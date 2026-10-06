"""Central validation for every value the integration writes to an inverter."""

from __future__ import annotations

import dataclasses
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Final

MAX_CURRENT_A: Final = 300
MAX_POWER_W: Final = 30_000
MAX_PLANT_PRICE: Final = 10.0

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_CURRENT_KEYS = frozenset(
    {
        "batteryMaxCurrentCharge",
        "batteryMaxCurrentDischarge",
        "chargeCurrent",
        "dischargeCurrent",
        "sdBatteryCurrent",
    }
)
_PERCENTAGE_KEYS = frozenset(
    {
        "batteryShutdownCap",
        "batteryRestartCap",
        "batteryLowCap",
        "generatorStartCap",
        "genOnCap",
        "genOffCap",
        *(f"cap{slot}" for slot in range(1, 7)),
    }
)
_POWER_KEYS = frozenset(
    {
        "zeroExportPower",
        "solarMaxSellPower",
        "pvMaxLimit",
        *(f"sellTime{slot}Pac" for slot in range(1, 7)),
    }
)
_ENUM_RANGES: dict[str, tuple[int, int]] = {
    "battMode": (0, 2),
    "sysWorkMode": (0, 4),
    "energyMode": (0, 1),
}
_BOOLEAN_KEYS = frozenset(
    {
        "solarSell",
        "batteryOn",
        "mondayOn",
        "tuesdayOn",
        "wednesdayOn",
        "thursdayOn",
        "fridayOn",
        "saturdayOn",
        "sundayOn",
        "genChargeOn",
        "gridAlwaysOn",
        "peakAndVallery",
        "sdChargeOn",
        "allowRemoteControl",
        *(f"time{slot}on" for slot in range(1, 7)),
        *(f"sellTime{slot}on" for slot in range(1, 7)),
        *(f"genTime{slot}on" for slot in range(1, 7)),
    }
)
_TIME_KEYS = frozenset(f"sellTime{slot}" for slot in range(1, 7))

WRITABLE_SETTING_KEYS: Final = frozenset().union(
    _CURRENT_KEYS,
    _PERCENTAGE_KEYS,
    _POWER_KEYS,
    _ENUM_RANGES,
    _BOOLEAN_KEYS,
    _TIME_KEYS,
)


class SunsynkSettingValidationError(ValueError):
    """Raised before an unsafe or unsupported setting reaches the API."""


def _integer_in_range(key: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise SunsynkSettingValidationError(f"{key} must be an integer, not a boolean")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise SunsynkSettingValidationError(f"{key} must be an integer") from err
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise SunsynkSettingValidationError(f"{key} must be a finite integer")
    result = int(numeric)
    if not minimum <= result <= maximum:
        raise SunsynkSettingValidationError(
            f"{key} must be between {minimum} and {maximum}; got {value!r}"
        )
    return result


def _boolean(key: str, value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return 1
        if lowered in {"0", "false", "no", "off"}:
            return 0
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value)
    raise SunsynkSettingValidationError(f"{key} must be a boolean or 0/1")


def validate_setting_value(key: str, value: Any) -> Any:
    """Validate and normalize one supported setting value."""
    if key in _CURRENT_KEYS:
        return _integer_in_range(key, value, 0, MAX_CURRENT_A)
    if key in _PERCENTAGE_KEYS:
        return _integer_in_range(key, value, 0, 100)
    if key in _POWER_KEYS:
        return _integer_in_range(key, value, 0, MAX_POWER_W)
    if key in _ENUM_RANGES:
        return _integer_in_range(key, value, *_ENUM_RANGES[key])
    if key in _BOOLEAN_KEYS:
        return _boolean(key, value)
    if key in _TIME_KEYS:
        if not isinstance(value, str) or _TIME_PATTERN.fullmatch(value) is None:
            raise SunsynkSettingValidationError(f"{key} must use 24-hour HH:MM format")
        return value
    raise SunsynkSettingValidationError(f"Unsupported writable setting: {key}")


def validate_setting_batch(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized copy, raising before the caller can enqueue the batch."""
    return {key: validate_setting_value(key, value) for key, value in settings.items()}


def validate_plant_price(value: Any) -> float:
    """Validate the constant plant energy price written to the plant API."""
    if isinstance(value, bool):
        raise SunsynkSettingValidationError("Plant price must be numeric")
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise SunsynkSettingValidationError("Plant price must be numeric") from err
    if not math.isfinite(price) or not 0 <= price <= MAX_PLANT_PRICE:
        raise SunsynkSettingValidationError(
            f"Plant price must be between 0 and {MAX_PLANT_PRICE}; got {value!r}"
        )
    return price


@dataclass(frozen=True)
class WriteProfile:
    """Installer-approved limits in the units and scope of the cloud registers."""

    members: tuple[str, ...]
    current_scope: str
    power_scope: str
    max_charge_current_a: int
    max_discharge_current_a: int
    max_power_w: int
    max_export_power_w: int
    min_soc_percent: int
    battery_voltage_min_v: float | None = None
    battery_voltage_max_v: float | None = None


def parse_write_profiles(value: Any, serials: list[str]) -> dict[str, WriteProfile]:
    """Reject incomplete limits and overlapping groups instead of guessing."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except json.JSONDecodeError as err:
            raise SunsynkSettingValidationError(
                "Write profiles must be valid JSON"
            ) from err
    if not isinstance(value, dict):
        raise SunsynkSettingValidationError("Write profiles must be an object")
    profiles: dict[str, WriteProfile] = {}
    assigned: set[str] = set()
    fields = {field.name for field in dataclasses.fields(WriteProfile)}
    required = {
        field.name
        for field in dataclasses.fields(WriteProfile)
        if field.default is dataclasses.MISSING
    }
    for target, profile in value.items():
        if (
            not isinstance(profile, dict)
            or not required <= profile.keys()
            or not profile.keys() <= fields
        ):
            raise SunsynkSettingValidationError(
                f"Incomplete or unknown profile fields for {target}"
            )
        members = profile["members"]
        if (
            not isinstance(members, list)
            or not members
            or any(
                not isinstance(member, str) or member not in serials
                for member in members
            )
            or target not in members
            or len(set(members)) != len(members)
            or assigned.intersection(members)
        ):
            raise SunsynkSettingValidationError(
                f"Invalid or overlapping members for {target}"
            )
        if any(
            profile[key] not in ("per_inverter", "group")
            for key in ("current_scope", "power_scope")
        ):
            raise SunsynkSettingValidationError(
                "Current and power scope must be explicitly confirmed"
            )
        limits = {
            key: _integer_in_range(
                key, profile[key], 0 if key == "max_export_power_w" else 1, maximum
            )
            for key, maximum in (
                ("max_charge_current_a", MAX_CURRENT_A),
                ("max_discharge_current_a", MAX_CURRENT_A),
                ("max_power_w", MAX_POWER_W),
                ("max_export_power_w", MAX_POWER_W),
                ("min_soc_percent", 100),
            )
        }
        if limits["max_export_power_w"] > limits["max_power_w"]:
            raise SunsynkSettingValidationError("Export limit exceeds the power limit")
        voltages = {
            key: profile.get(key)
            for key in ("battery_voltage_min_v", "battery_voltage_max_v")
        }
        if any(value is not None for value in voltages.values()):
            try:
                if any(isinstance(value, bool) for value in voltages.values()):
                    raise ValueError
                voltages = {key: float(value) for key, value in voltages.items()}
                if (
                    not all(math.isfinite(value) for value in voltages.values())
                    or not 0
                    < voltages["battery_voltage_min_v"]
                    < voltages["battery_voltage_max_v"]
                ):
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as err:
                raise SunsynkSettingValidationError(
                    "Provide both approved battery voltage bounds"
                ) from err
        profiles[target] = WriteProfile(
            members=tuple(members),
            current_scope=profile["current_scope"],
            power_scope=profile["power_scope"],
            **limits,
            **voltages,
        )
        assigned.update(members)
    return profiles


def validate_installation_settings(
    updates: dict[str, Any],
    settings: dict[str, Any],
    profile: WriteProfile,
    rated_power_w: int,
) -> dict[str, Any]:
    """Validate requested values against fresh device state and site limits."""
    updates = validate_setting_batch(updates)
    effective = {**settings, **updates}
    charge_keys = {"batteryMaxCurrentCharge", "chargeCurrent", "sdBatteryCurrent"}
    discharge_keys = {"batteryMaxCurrentDischarge", "dischargeCurrent"}
    for key, value in updates.items():
        if key in charge_keys | discharge_keys:
            charging = key in charge_keys
            limit = (
                profile.max_charge_current_a
                if charging
                else profile.max_discharge_current_a
            )
            maximum_key = (
                "batteryMaxCurrentCharge" if charging else "batteryMaxCurrentDischarge"
            )
            # The device's configured maximum is an additional limit, never a BMS rating.
            maximum = validate_setting_value(maximum_key, effective.get(maximum_key))
            if value > min(limit, maximum):
                raise SunsynkSettingValidationError(
                    f"{key} exceeds the installation or configured battery limit"
                )
        elif key in _POWER_KEYS:
            limit = min(profile.max_power_w, rated_power_w)
            if key in {"zeroExportPower", "solarMaxSellPower"}:
                limit = min(limit, profile.max_export_power_w)
            elif key.startswith("sellTime"):
                index = key.removeprefix("sellTime").removesuffix("Pac")
                if validate_setting_value(
                    f"sellTime{index}on", effective.get(f"sellTime{index}on")
                ):
                    limit = min(limit, profile.max_export_power_w)
            if value > limit:
                raise SunsynkSettingValidationError(
                    f"{key} exceeds the installation power limit"
                )
        if (
            key in _PERCENTAGE_KEYS
            and key.startswith("cap")
            and validate_setting_value(
                f"time{key.removeprefix('cap')}on",
                effective.get(f"time{key.removeprefix('cap')}on"),
            )
            and value
            < max(
                profile.min_soc_percent,
                validate_setting_value("batteryLowCap", effective.get("batteryLowCap")),
            )
        ):
            raise SunsynkSettingValidationError(
                f"{key} is below the installation SOC reserve"
            )

    if updates.keys() & {"batteryMaxCurrentCharge", "batteryMaxCurrentDischarge"}:
        for key in ("chargeCurrent", "dischargeCurrent", "sdBatteryCurrent"):
            if key in effective:
                validate_installation_settings(
                    {key: effective[key]}, effective, profile, rated_power_w
                )

    if updates.keys() & {"batteryShutdownCap", "batteryRestartCap", "batteryLowCap"}:
        shutdown, restart, low = (
            validate_setting_value(key, effective.get(key))
            for key in ("batteryShutdownCap", "batteryRestartCap", "batteryLowCap")
        )
        if shutdown >= restart or shutdown > low:
            raise SunsynkSettingValidationError(
                "Shutdown SOC must be below restart SOC and no greater than low SOC"
            )

    # Enabling export must respect an existing power cap too.
    export_caps = {
        "solarSell": "solarMaxSellPower",
        **{f"sellTime{n}on": f"sellTime{n}Pac" for n in range(1, 7)},
    }
    for key, cap in export_caps.items():
        if updates.get(key) == 1:
            power = validate_setting_value(cap, effective.get(cap))
            if power > min(
                profile.max_export_power_w, profile.max_power_w, rated_power_w
            ):
                raise SunsynkSettingValidationError(
                    "Existing export power exceeds the installation limit"
                )

    if updates.keys() & {"sysWorkMode", "energyMode", "batteryOn", "peakAndVallery"}:
        guard_keys = {
            "chargeCurrent",
            "dischargeCurrent",
            "batteryMaxCurrentCharge",
            "batteryMaxCurrentDischarge",
            "solarMaxSellPower",
            "pvMaxLimit",
        }
        validate_installation_settings(
            {key: effective.get(key) for key in guard_keys},
            effective,
            profile,
            rated_power_w,
        )

    for n in range(1, 7):
        if updates.get(f"time{n}on") == 1:
            companions = {
                key: effective.get(key) for key in (f"cap{n}", f"sellTime{n}Pac")
            }
            validate_installation_settings(
                companions, effective, profile, rated_power_w
            )

    if any(key in _TIME_KEYS or re.fullmatch(r"time[1-6]on", key) for key in updates):
        times = [
            validate_setting_value(f"sellTime{n}", effective.get(f"sellTime{n}"))
            for n in range(1, 7)
        ]
        enabled = [
            validate_setting_value(f"time{n}on", effective.get(f"time{n}on"))
            for n in range(1, 7)
        ]
        if sum(times[n] > times[(n + 1) % 6] for n in range(6)) > 1:
            raise SunsynkSettingValidationError(
                "The six timer slots must follow circular time order"
            )
        if any(enabled[n] and times[n] == times[(n + 1) % 6] for n in range(6)):
            raise SunsynkSettingValidationError(
                "An enabled timer slot must have a nonzero duration"
            )
    return updates


def validate_setting_payload(
    payload: dict[str, Any],
    settings: dict[str, Any],
    profile: WriteProfile,
    rated_power_w: int,
) -> dict[str, Any]:
    """Validate every transmitted companion field without widening the edit API."""
    supported = {
        key: value for key, value in payload.items() if key in WRITABLE_SETTING_KEYS
    }
    result = validate_installation_settings(supported, settings, profile, rated_power_w)
    for key in payload.keys() - supported.keys():
        if not re.fullmatch(r"sellTime[1-6]Volt", key):
            raise SunsynkSettingValidationError(f"Unsupported companion field {key}")
        try:
            value = float(payload[key])
            if (
                isinstance(payload[key], bool)
                or not math.isfinite(value)
                or profile.battery_voltage_min_v is None
                or profile.battery_voltage_max_v is None
                or not profile.battery_voltage_min_v
                <= value
                <= profile.battery_voltage_max_v
            ):
                raise ValueError
        except (TypeError, ValueError, OverflowError) as err:
            raise SunsynkSettingValidationError(
                f"{key} requires approved battery voltage bounds"
            ) from err
        result[key] = value
    return result
