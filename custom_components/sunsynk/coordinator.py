"""DataUpdateCoordinator for Sunsynk integration."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.auth import SunsynkAuth, SunsynkAuthError
from .api.client import SunsynkApiError, SunsynkAuthenticationError, SunsynkClient
from .calibration import PerformanceRatioCalibrator
from .const import (
    BATTERY_SETTING_KEYS,
    DOMAIN,
    SLOT_SETTING_KEY_GROUPS,
    SOLAR_FORECAST_UPDATE_INTERVAL,
    SYSTEM_MODE_SETTING_KEYS,
)
from .write_policy import WritePolicy
from .write_validation import (
    SunsynkSettingValidationError,
    WriteProfile,
    validate_installation_settings,
    validate_plant_price,
    validate_setting_batch,
    validate_setting_payload,
    validate_setting_value,
)

_LOGGER = logging.getLogger(__name__)

# Some accounts (observed on a parallel/multi-inverter setup, #21) apparently
# relay a settings write to the physical inverter asynchronously — the write
# The write endpoint acknowledges before a command has necessarily propagated.
# Verify adaptively instead of blocking every per-inverter write queue for a
# fixed two seconds: most writes settle after the first short delay, while
# slower relays still get the same two-second propagation window.
_VERIFY_WRITE_RETRY_DELAYS = (0.25, 0.5, 1.25)


class SunsynkCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Manages data fetching for all inverters in one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        auth: SunsynkAuth,
        serials: list[str],
        refresh_interval: int,
        entry_id: str = "",
        *,
        write_policy: WritePolicy | None = None,
        write_profiles: dict[str, WriteProfile] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=refresh_interval),
        )
        self._auth = auth
        self.serials = serials
        self._entry_id = entry_id
        self.write_policy = write_policy or WritePolicy()
        self.write_profiles = write_profiles or {}
        self._session: aiohttp.ClientSession | None = None
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._pending_setting_writes: dict[str, dict[str, Any]] = {}
        self._pending_setting_waiters: dict[
            str, list[tuple[asyncio.Future[None], frozenset[str]]]
        ] = {}
        self._write_drain_scheduled: set[str] = set()
        self._write_drain_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def writes_pending(self) -> bool:
        """Include queued, dispatched and plant-level operations."""
        return bool(
            self._pending_setting_writes
            or self._write_drain_scheduled
            or self._write_drain_tasks
            or any(lock.locked() for lock in self._write_locks.values())
        )

    async def _async_get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    @property
    def write_target_serials(self) -> list[str]:
        """Deduplicate verified groups; unknown targets are never writable.

        Controllers use this list while polling still reads every serial.
        """
        targets = []
        for serial in self.serials:
            try:
                target = SunsynkCoordinator.resolve_write_target(self, serial)
            except UpdateFailed:
                continue
            if target not in targets:
                targets.append(target)
        return targets

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch data from all inverter endpoints."""
        session = await self._async_get_session()

        try:
            token = await self._auth.async_get_token(session)
        except SunsynkAuthError as err:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"auth_failed_{self._entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="auth_failed",
            )
            raise UpdateFailed(f"Authentication failed: {err}") from err

        ir.async_delete_issue(self.hass, DOMAIN, f"auth_failed_{self._entry_id}")

        client = SunsynkClient(
            self._auth._api_server, token, write_policy=self.write_policy
        )

        result: dict[str, dict[str, Any]] = {}
        inverter_failures: list[tuple[str, Exception]] = []
        for serial in self.serials:
            try:
                result[serial] = await client.async_fetch_all(session, serial)
                _LOGGER.debug("Data updated for inverter %s", serial)
                ir.async_delete_issue(self.hass, DOMAIN, f"inverter_offline_{serial}")
            except SunsynkAuthenticationError as err:
                self._auth.invalidate_token()
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"auth_failed_{self._entry_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="auth_failed",
                )
                raise UpdateFailed(f"Authentication failed: {err}") from err
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Failed to fetch data for inverter %s: %s", serial, err)
                inverter_failures.append((serial, err))
                # Retain non-safety-critical cached values for continuity, but
                # never carry a stale SOC into the tariff state machine.  An
                # empty battery payload deliberately triggers its fail-closed
                # path for this inverter.
                result[serial] = dict(self.data.get(serial, {})) if self.data else {}
                result[serial]["battery"] = {}
                result[serial]["inverter"] = {}
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"inverter_offline_{serial}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="inverter_offline",
                    translation_placeholders={"serial": serial},
                )
                continue

            plant_id = result[serial].get("inverter", {}).get("plant", {}).get("id")
            if plant_id:
                try:
                    result[serial]["plant"] = await client.async_get_plant_info(
                        session, str(plant_id)
                    )
                except SunsynkAuthenticationError as err:
                    self._auth.invalidate_token()
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        f"auth_failed_{self._entry_id}",
                        is_fixable=False,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="auth_failed",
                    )
                    raise UpdateFailed(f"Authentication failed: {err}") from err
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Could not fetch plant info for %s (plant %s): %s",
                        serial,
                        plant_id,
                        err,
                    )
                    result[serial]["plant"] = (
                        (self.data or {}).get(serial, {}).get("plant", {})
                    )
            else:
                result[serial]["plant"] = {}

        if self.serials and len(inverter_failures) == len(self.serials):
            serial, err = inverter_failures[0]
            raise UpdateFailed(
                f"All inverter updates failed; first failure for {serial}: {err}"
            ) from err

        return result

    def resolve_write_target(self, serial: str) -> str:
        """Resolve only explicitly configured groups with verified cached roles."""
        if (
            serial not in self.serials
            or getattr(self, "last_update_success", True) is False
        ):
            raise UpdateFailed("Writes require a successful topology poll")
        target = next(
            (
                target
                for target, profile in self.write_profiles.items()
                if serial in profile.members
            ),
            None,
        )
        if target is None:
            raise UpdateFailed(
                "Configure a write profile with verified membership and site limits before writing"
            )
        infos = {
            member: (self.data or {}).get(member, {}).get("inverter", {})
            for member in self.write_profiles[target].members
        }
        SunsynkCoordinator._validate_write_topology(self, target, infos)
        return target

    def _validate_write_topology(
        self, target: str, infos: dict[str, dict[str, Any]]
    ) -> int:
        """Verify roles, plant membership and rated power without a fallback target."""
        profile = self.write_profiles[target]
        plant_ids = set()
        rated_power = 0
        try:
            for member in profile.members:
                info = infos[member]
                if info.get("sn", member) != member:
                    raise SunsynkSettingValidationError("Unexpected inverter serial")
                parallel = validate_setting_value("batteryOn", info.get("parallel"))
                if len(profile.members) == 1:
                    if parallel:
                        raise SunsynkSettingValidationError(
                            "Parallel membership is incomplete"
                        )
                elif not parallel or validate_setting_value(
                    "energyMode", info.get("equipMode")
                ) != (1 if member == target else 0):
                    raise SunsynkSettingValidationError(
                        "Parallel master/slave roles do not match the configured group"
                    )
                plant_id = info.get("plant", {}).get("id")
                if plant_id is None or isinstance(plant_id, bool) or not str(plant_id):
                    raise SunsynkSettingValidationError("Plant identity is missing")
                plant_ids.add(str(plant_id))
                power = validate_setting_value("pvMaxLimit", info.get("ratePower"))
                if power == 0:
                    raise SunsynkSettingValidationError("Rated power is unknown")
                if member == target or profile.power_scope == "group":
                    rated_power += power
            if len(plant_ids) != 1:
                raise SunsynkSettingValidationError(
                    "Members belong to different plants"
                )
        except (
            KeyError,
            TypeError,
            AttributeError,
            SunsynkSettingValidationError,
        ) as err:
            raise UpdateFailed(f"Unverified write topology: {err}") from err
        return rated_power

    async def _async_read_write_topology(
        self, client: SunsynkClient, session: aiohttp.ClientSession, target: str
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Read every declared member before authorizing the physical target."""
        infos = {
            member: await client.async_get_inverter_info(session, member)
            for member in self.write_profiles[target].members
        }
        return infos, SunsynkCoordinator._validate_write_topology(self, target, infos)

    def scheduled_power_limit(self, serial: str, *, exporting: bool = False) -> int:
        """Use confirmed register scope and site limits; never invent rated power."""
        target = SunsynkCoordinator.resolve_write_target(self, serial)
        profile = self.write_profiles[target]
        infos = {
            member: (self.data or {}).get(member, {}).get("inverter", {})
            for member in profile.members
        }
        rated = SunsynkCoordinator._validate_write_topology(self, target, infos)
        return min(
            rated,
            profile.max_power_w,
            profile.max_export_power_w if exporting else profile.max_power_w,
        )

    def _resolve_parallel_write_target(self, serial: str, setting_key: str) -> str:
        """Backward-compatible internal wrapper for write routing."""
        return SunsynkCoordinator.resolve_write_target(self, serial)

    def _ensure_write_state(self) -> None:
        """Initialise write coordination lazily for lightweight test doubles."""
        if not hasattr(self, "_write_locks"):
            self._write_locks = {}
        if not hasattr(self, "_pending_setting_writes"):
            self._pending_setting_writes = {}
        if not hasattr(self, "_pending_setting_waiters"):
            self._pending_setting_waiters = {}
        if not hasattr(self, "_write_drain_scheduled"):
            self._write_drain_scheduled = set()
        if not hasattr(self, "_write_drain_tasks"):
            self._write_drain_tasks = {}

    def _write_lock_for(self, serial: str) -> asyncio.Lock:
        SunsynkCoordinator._ensure_write_state(self)
        return self._write_locks.setdefault(serial, asyncio.Lock())

    @staticmethod
    def _allowed_setting_group(setting_key: str) -> frozenset[str]:
        """Return the API payload group that owns one setting."""
        if setting_key in BATTERY_SETTING_KEYS:
            return BATTERY_SETTING_KEYS
        slot_keys = next(
            (keys for keys in SLOT_SETTING_KEY_GROUPS.values() if setting_key in keys),
            None,
        )
        if slot_keys is not None:
            return slot_keys
        if setting_key in SYSTEM_MODE_SETTING_KEYS:
            return SYSTEM_MODE_SETTING_KEYS
        return frozenset([setting_key])

    async def async_write_setting(
        self, serial: str, setting_key: str, value: Any
    ) -> None:
        """Queue one setting write through the shared coalescing pipeline."""
        await SunsynkCoordinator.async_write_settings(
            self, serial, {setting_key: value}
        )

    async def async_write_settings(self, serial: str, settings: dict[str, Any]) -> None:
        """Coalesce and serialize setting writes for one physical inverter.

        Calls made during the same event-loop turn are merged. Settings from
        the same API group (notably all fields of one timer slot) are emitted
        in one payload. A per-target lock prevents services, entities, tariff
        evaluation and virtual-slot ticks from racing each other.
        """
        if not settings:
            return
        self.write_policy.ensure_writable()
        # Validate the complete caller-supplied batch before mutating queue
        # state. A bad field must not allow valid siblings to leak to the API.
        settings = validate_setting_batch(settings)
        serial = SunsynkCoordinator.resolve_write_target(self, serial)
        SunsynkCoordinator._ensure_write_state(self)

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        pending = self._pending_setting_writes.setdefault(serial, {})
        pending.update(settings)
        self._pending_setting_waiters.setdefault(serial, []).append(
            (waiter, frozenset(settings))
        )

        if serial not in self._write_drain_scheduled:
            self._write_drain_scheduled.add(serial)
            loop.call_soon(
                SunsynkCoordinator._launch_write_drain,
                self,
                serial,
            )

        await waiter

    def _launch_write_drain(self, serial: str) -> None:
        """Launch a queued drain after same-turn callers have coalesced."""
        task = asyncio.get_running_loop().create_task(
            SunsynkCoordinator._async_drain_write_queue(self, serial)
        )
        self._write_drain_tasks[serial] = task

    async def _async_drain_write_queue(self, serial: str) -> None:
        """Drain all queued batches for one target under its write lock."""
        current_waiters: list[tuple[asyncio.Future[None], frozenset[str]]] = []
        try:
            while updates := self._pending_setting_writes.pop(serial, None):
                current_waiters = self._pending_setting_waiters.pop(serial, [])
                async with SunsynkCoordinator._write_lock_for(self, serial):
                    failures = await SunsynkCoordinator._async_execute_setting_batch(
                        self, serial, updates
                    )

                for waiter, keys in current_waiters:
                    if waiter.done():
                        continue
                    error = next(
                        (failures[key] for key in keys if key in failures),
                        None,
                    )
                    if error is None:
                        waiter.set_result(None)
                    else:
                        waiter.set_exception(error)
                current_waiters = []
        except Exception as err:
            # A coordinator-internal failure must never strand callers.
            pending_waiters = self._pending_setting_waiters.pop(serial, [])
            for waiter, _keys in [*current_waiters, *pending_waiters]:
                if not waiter.done():
                    waiter.set_exception(err)
            _LOGGER.exception("Setting write queue failed for inverter %s", serial)
        finally:
            self._write_drain_tasks.pop(serial, None)
            self._write_drain_scheduled.discard(serial)
            if self._pending_setting_writes.get(serial):
                self._write_drain_scheduled.add(serial)
                asyncio.get_running_loop().call_soon(
                    SunsynkCoordinator._launch_write_drain,
                    self,
                    serial,
                )

    async def _async_execute_setting_batch(
        self, serial: str, updates: dict[str, Any]
    ) -> dict[str, Exception]:
        """Write one coalesced batch and return failures keyed by setting."""
        self.write_policy.ensure_writable()
        session = await self._async_get_session()
        try:
            token = await self._auth.async_get_token(session)
        except SunsynkAuthError as err:
            error = UpdateFailed(f"Authentication failed: {err}")
            return {key: error for key in updates}

        self.write_policy.ensure_writable()
        client = SunsynkClient(
            self._auth._api_server, token, write_policy=self.write_policy
        )
        profile = self.write_profiles[serial]
        try:
            # Re-read topology at dispatch: a cached role never authorizes a POST.
            _infos, rated_power = await SunsynkCoordinator._async_read_write_topology(
                self, client, session, serial
            )
            baseline = dict(await client.async_get_settings(session, serial))
            updates = validate_installation_settings(
                updates, baseline, profile, rated_power
            )
        except SunsynkAuthenticationError as err:
            self._auth.invalidate_token()
            return {
                key: UpdateFailed(f"Authentication failed: {err}") for key in updates
            }
        except (SunsynkApiError, SunsynkSettingValidationError, UpdateFailed) as err:
            return {
                key: UpdateFailed(f"Write preflight failed: {err}") for key in updates
            }

        grouped_updates: dict[frozenset[str], dict[str, Any]] = {}
        for key, value in updates.items():
            group = SunsynkCoordinator._allowed_setting_group(key)
            grouped_updates.setdefault(group, {})[key] = value

        for group, changes in grouped_updates.items():
            try:
                fresh = dict(await client.async_get_settings(session, serial))
                # Without cloud compare-and-swap, reject detectable external edits.
                watched = group - {"sn"}
                if any(
                    not SunsynkCoordinator._values_match(
                        baseline.get(key), fresh.get(key)
                    )
                    for key in watched
                    if key in baseline or key in fresh
                ):
                    raise UpdateFailed(
                        "Settings changed during write preparation; refresh and retry"
                    )
                changes = validate_installation_settings(
                    changes, fresh, profile, rated_power
                )
                # Timer companions are required by the API; battery/system edits are minimal.
                payload = dict(changes)
                if group in SLOT_SETTING_KEY_GROUPS.values():
                    required = {key for key in group if not key.endswith("Volt")}
                    if not required <= fresh.keys():
                        raise UpdateFailed(
                            "A complete timer slot is required before writing"
                        )
                    payload = {
                        key: value for key, value in fresh.items() if key in group
                    }
                    payload.update(changes)
                payload = validate_setting_payload(payload, fresh, profile, rated_power)
                # Revalidate roles again after reads, immediately before dispatch.
                (
                    _infos,
                    latest_rated,
                ) = await SunsynkCoordinator._async_read_write_topology(
                    self, client, session, serial
                )
                payload = validate_setting_payload(
                    payload, fresh, profile, latest_rated
                )
                await client.async_write_settings(
                    session, serial, {**payload, "sn": serial}
                )
                expected = {
                    key: value for key, value in fresh.items() if key in watched
                }
                expected.update(changes)
                await SunsynkCoordinator._async_verify_writes(
                    self, client, session, serial, expected
                )
                baseline.update(changes)
            except SunsynkAuthenticationError as err:
                self._auth.invalidate_token()
                return {
                    key: UpdateFailed(f"Authentication failed: {err}")
                    for key in updates
                }
            except (
                SunsynkApiError,
                SunsynkSettingValidationError,
                UpdateFailed,
            ) as err:
                # Earlier groups may already be applied; never imply batch atomicity.
                return {
                    key: UpdateFailed(
                        f"Write failed; settings may be partially applied: {err}"
                    )
                    for key in updates
                }
        await self.async_request_refresh()
        return {}

    async def _async_verify_write(
        self,
        client: SunsynkClient,
        session: aiohttp.ClientSession,
        serial: str,
        setting_key: str,
        value: Any,
    ) -> None:
        """Backward-compatible single-setting verification wrapper."""
        await SunsynkCoordinator._async_verify_writes(
            self, client, session, serial, {setting_key: value}
        )

    async def _async_verify_writes(
        self,
        client: SunsynkClient,
        session: aiohttp.ClientSession,
        serial: str,
        expected: dict[str, Any],
    ) -> None:
        """Fail-safe: verify a whole batch with adaptive delayed API reads.

        The write endpoint returns success even when the inverter silently
        ignores a value (out-of-range, conflicting with another setting,
        dongle briefly offline), so a 200 response alone doesn't prove the
        change actually took. Re-reading once confirms every field in the
        coalesced batch and raises a Repair for each mismatch. Verification
        starts after a short delay and retries only while propagation is still
        pending, avoiding a fixed two-second delay on every successful write.
        """
        fresh_settings: dict[str, Any] = {}
        for delay in _VERIFY_WRITE_RETRY_DELAYS:
            await asyncio.sleep(delay)
            try:
                fresh_settings = await client.async_get_settings(session, serial)
            except SunsynkAuthenticationError as err:
                self._auth.invalidate_token()
                raise UpdateFailed(f"Authentication failed: {err}") from err
            except SunsynkApiError as err:
                raise UpdateFailed(
                    f"Could not verify writes of {', '.join(expected)} for "
                    f"{serial}: {err}"
                ) from err

            if all(
                SunsynkCoordinator._values_match(value, fresh_settings.get(key))
                for key, value in expected.items()
            ):
                break

        # Replace any assumptions with the authoritative read-back before
        # notifying consumers. This also rolls the cache back when the device
        # silently ignored one or more values.
        if self.data is not None and serial in self.data:
            self.data[serial].setdefault("settings", {}).update(fresh_settings)

        mismatches: list[str] = []
        for setting_key, value in expected.items():
            issue_id = f"setting_write_mismatch_{serial}_{setting_key}"
            actual = fresh_settings.get(setting_key)
            if SunsynkCoordinator._values_match(value, actual):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue

            _LOGGER.warning(
                "Write verification failed for %s on inverter %s: sent %r, "
                "inverter reports %r",
                setting_key,
                serial,
                value,
                actual,
            )
            mismatches.append(f"{setting_key}: expected {value!r}, actual {actual!r}")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="setting_write_mismatch",
                translation_placeholders={
                    "serial": serial,
                    "setting_key": setting_key,
                    "expected": str(value),
                    "actual": str(actual),
                },
            )

        if mismatches:
            raise UpdateFailed(
                f"Inverter {serial} rejected setting write(s): " + "; ".join(mismatches)
            )

    _TRUE_STRINGS = frozenset({"true", "1"})
    _FALSE_STRINGS = frozenset({"false", "0"})
    _TIME_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")

    @classmethod
    def _values_match(cls, sent: Any, actual: Any) -> bool:
        """Type-tolerant comparison between what we sent and what the API echoes back.

        The API returns booleans as "true"/"false" strings (we send 1/0)
        and numbers as strings with inconsistent formatting (e.g. "90.0"
        for an int we sent as 90), so a naive `!=` would false-positive on
        a successful write.
        """
        if actual is None:
            return sent is None

        sent_str = str(sent).strip().lower()
        actual_str = str(actual).strip().lower()
        if sent_str == actual_str:
            return True
        if sent_str in cls._TRUE_STRINGS and actual_str in cls._TRUE_STRINGS:
            return True
        if sent_str in cls._FALSE_STRINGS and actual_str in cls._FALSE_STRINGS:
            return True

        sent_time = cls._TIME_RE.fullmatch(sent_str)
        actual_time = cls._TIME_RE.fullmatch(actual_str)
        if sent_time is not None and actual_time is not None:
            sent_minutes = int(sent_time[1]) * 60 + int(sent_time[2])
            actual_minutes = int(actual_time[1]) * 60 + int(actual_time[2])
            return sent_minutes == actual_minutes

        try:
            return float(sent_str) == float(actual_str)
        except (TypeError, ValueError):
            return False

    async def async_write_plant_price(self, serial: str, price: float) -> None:
        """Serialize a plant-price write with every inverter setting write."""
        self.write_policy.ensure_writable()
        price = validate_plant_price(price)
        serial = SunsynkCoordinator.resolve_write_target(self, serial)
        async with SunsynkCoordinator._write_lock_for(self, serial):
            await SunsynkCoordinator._async_write_plant_price_locked(
                self, serial, price
            )

    async def _async_write_plant_price_locked(self, serial: str, price: float) -> None:
        """Set a manual constant electricity price for the inverter's plant.

        This is a plant-level (not inverter-level) setting. For safety it
        only edits an already-existing single Constant Price entry. A
        Time-of-Use, live-price or multi-entry configuration is rejected
        rather than destructively replaced. Currency and investment figures
        are read fresh and passed through unchanged.
        """
        session = await self._async_get_session()

        try:
            token = await self._auth.async_get_token(session)
        except SunsynkAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err

        client = SunsynkClient(
            self._auth._api_server, token, write_policy=self.write_policy
        )

        try:
            infos, _rated = await SunsynkCoordinator._async_read_write_topology(
                self, client, session, serial
            )
        except SunsynkAuthenticationError as err:
            self._auth.invalidate_token()
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except SunsynkApiError as err:
            raise UpdateFailed(
                f"Cannot read inverter info for {serial}: {err}"
            ) from err
        plant_id = infos[serial]["plant"]["id"]

        try:
            plant = await client.async_get_plant_info(session, str(plant_id))
        except SunsynkAuthenticationError as err:
            self._auth.invalidate_token()
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except SunsynkApiError as err:
            raise UpdateFailed(f"Cannot read plant info for {plant_id}: {err}") from err

        charges = plant.get("charges")
        only_charge = (
            charges[0] if isinstance(charges, list) and len(charges) == 1 else None
        )
        if not isinstance(only_charge, dict) or str(only_charge.get("type")) != "1":
            raise UpdateFailed(
                "Plant price can only be changed when the Sunsynk plant already "
                "uses exactly one Constant Price entry. The existing Time-of-Use, "
                "live-price or multi-entry tariff was left unchanged."
            )

        plant_response_id = plant.get("id")
        currency_id = (plant.get("currency") or {}).get("id")
        if (
            plant_response_id is None
            or str(plant_response_id) != str(plant_id)
            or currency_id is None
            or "invest" not in plant
        ):
            raise UpdateFailed(
                f"Plant {plant_id} pricing metadata is incomplete; no changes were made"
            )

        updated_charge = dict(only_charge)
        updated_charge["price"] = price

        payload = {
            "id": plant_response_id,
            "currency": currency_id,
            "invest": plant.get("invest"),
            "charges": [updated_charge],
        }

        try:
            await client.async_set_plant_income(session, str(plant_id), payload)
        except SunsynkAuthenticationError as err:
            self._auth.invalidate_token()
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except SunsynkApiError as err:
            raise UpdateFailed(
                f"Failed to write plant price for {plant_id}: {err}"
            ) from err

        await self.async_request_refresh()

    async def async_close(self) -> None:
        """Flush active write drains, then close the aiohttp session."""
        SunsynkCoordinator._ensure_write_state(self)
        await asyncio.sleep(0)
        tasks = list(self._write_drain_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()


class SolarForecastCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches solar irradiance and weather forecast from Open-Meteo (no API key)."""

    _URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(
        self,
        hass: HomeAssistant,
        latitude: float,
        longitude: float,
        panel_kwp: float,
        performance_ratio: float,
        calibrator: PerformanceRatioCalibrator | None = None,
        actual_energy_fn: Callable[[], float | None] | None = None,
    ) -> None:
        if not (
            -90 <= latitude <= 90
            and -180 <= longitude <= 180
            and panel_kwp > 0
            and 0 < performance_ratio <= 1
        ):
            raise ValueError("Invalid solar forecast configuration")
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_forecast",
            update_interval=timedelta(minutes=SOLAR_FORECAST_UPDATE_INTERVAL),
        )
        self._latitude = latitude
        self._longitude = longitude
        self._panel_kwp = panel_kwp
        self._performance_ratio = performance_ratio
        self._calibrator = calibrator
        self._actual_energy_fn = actual_energy_fn
        self._session: aiohttp.ClientSession | None = None

    async def _async_get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _async_update_data(self) -> dict[str, Any]:
        session = await self._async_get_session()
        params = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "hourly": "shortwave_radiation,direct_normal_irradiance,cloud_cover,precipitation",
            "forecast_days": 2,
            "timezone": "auto",
        }
        try:
            async with session.get(
                self._URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                raw = await resp.json()
        except Exception as err:
            raise UpdateFailed(f"Open-Meteo request failed: {err}") from err

        return await self._parse(raw)

    async def _parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        hourly = raw.get("hourly", {})
        times: list[str] = hourly.get("time", [])
        ghi: list[Any] = hourly.get("shortwave_radiation", [])
        dni: list[Any] = hourly.get("direct_normal_irradiance", [])
        cloud: list[Any] = hourly.get("cloud_cover", [])
        precip: list[Any] = hourly.get("precipitation", [])

        now = dt_util.now()
        offset = raw.get("utc_offset_seconds")
        if isinstance(offset, (int, float)) and -86400 < offset < 86400:
            # Open-Meteo's hourly timestamps are naive wall-clock values in
            # the requested location. Compare them against that location's
            # date/hour rather than Home Assistant's potentially different
            # timezone.
            now = now.astimezone(timezone(timedelta(seconds=offset)))
        today = now.date()
        tomorrow = today + timedelta(days=1)

        performance_ratio = (
            self._calibrator.get_ratio(now.month)
            if self._calibrator is not None
            else self._performance_ratio
        )

        today_raw_kwh = 0.0  # ratio=1 irradiance model, used to calibrate the ratio
        today_kwh = 0.0
        tomorrow_kwh = 0.0
        for i, ts in enumerate(times):
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                continue
            g = float(ghi[i]) if i < len(ghi) and ghi[i] is not None else 0.0
            raw_contribution = g / 1000.0 * self._panel_kwp
            if dt.date() == today:
                today_raw_kwh += raw_contribution
                today_kwh += raw_contribution * performance_ratio
            elif dt.date() == tomorrow:
                tomorrow_kwh += raw_contribution * performance_ratio

        if self._calibrator is not None and self._actual_energy_fn is not None:
            await self._calibrator.async_update(
                today, today_raw_kwh, self._actual_energy_fn()
            )

        current_hour_str = now.strftime("%Y-%m-%dT%H:00")
        idx: int | None = None
        for i, ts in enumerate(times):
            if ts == current_hour_str:
                idx = i
                break

        def _val(lst: list[Any], i: int | None) -> float | None:
            if i is None or i >= len(lst) or lst[i] is None:
                return None
            return round(float(lst[i]), 1)

        return {
            "today_kwh": round(today_kwh, 2),
            "tomorrow_kwh": round(tomorrow_kwh, 2),
            "cloud_cover": _val(cloud, idx),
            "precipitation": _val(precip, idx),
            "ghi": _val(ghi, idx),
            "dni": _val(dni, idx),
            "performance_ratio": round(performance_ratio, 3),
        }

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
