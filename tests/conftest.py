"""Shared pytest fixtures for Sunsynk tests."""

from __future__ import annotations

from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.sunsynk.write_policy import WritePolicy


class FakeResponse:
    """Stands in for an aiohttp response used as `async with session.get(...) as resp`."""

    def __init__(self, json_data: Any, status: int = 200) -> None:
        self._json_data = json_data
        self.status = status

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=self.status,
            )

    async def json(self) -> Any:
        return self._json_data


def fake_session(**method_return_values: Any) -> MagicMock:
    """A MagicMock aiohttp.ClientSession whose .get/.post return FakeResponse objects.

    Pass get=<FakeResponse-or-list-of-them> and/or post=<...>. A list is
    consumed one call at a time (via side_effect); a single value is
    returned for every call.
    """
    session = MagicMock()
    for method, value in method_return_values.items():
        mock_method = MagicMock()
        if isinstance(value, list):
            mock_method.side_effect = value
        else:
            mock_method.return_value = value
        setattr(session, method, mock_method)
    return session


@pytest.fixture
def mock_hass():
    """Return a minimal mock HomeAssistant instance."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.services = MagicMock()
    return hass


@pytest.fixture
def mock_coordinator(mock_hass):
    """Return a mock SunsynkCoordinator."""
    coordinator = MagicMock()
    coordinator.write_policy = WritePolicy("read_write")
    coordinator.serials = ["TEST123"]
    # Mirrors real SunsynkCoordinator.write_target_serials — for a plain
    # (non-parallel) single-inverter fixture this is identical to `serials`.
    # A test exercising parallel-group dedup overrides this directly.
    coordinator.write_target_serials = ["TEST123"]
    coordinator.data = {
        "TEST123": {
            "battery": {"soc": 50, "power": 0},
            "settings": safe_settings(),
            "inverter": inverter_info(),
        }
    }
    coordinator.write_profiles = {"TEST123": write_profile()}

    def scheduled_limit(serial, *, exporting=False):
        from custom_components.sunsynk.coordinator import SunsynkCoordinator

        info = coordinator.data[serial].setdefault("inverter", inverter_info(serial))
        # Tests that override only power retain the unrelated topology metadata.
        info.update(
            {
                key: value
                for key, value in inverter_info(serial).items()
                if key not in info
            }
        )
        return SunsynkCoordinator.scheduled_power_limit(
            coordinator, serial, exporting=exporting
        )

    coordinator.scheduled_power_limit.side_effect = scheduled_limit
    from custom_components.sunsynk.coordinator import SunsynkCoordinator

    coordinator.resolve_write_target.side_effect = lambda serial: (
        SunsynkCoordinator.resolve_write_target(coordinator, serial)
    )
    coordinator.async_write_setting = AsyncMock()
    coordinator.async_write_settings = AsyncMock()
    return coordinator


def write_profile(target="TEST123", members=None, **limits):
    """Explicit test installation; never use these limits on real hardware."""
    from custom_components.sunsynk.write_validation import WriteProfile

    values = dict(
        members=tuple(members or [target]),
        current_scope="per_inverter",
        power_scope="per_inverter",
        max_charge_current_a=300,
        max_discharge_current_a=300,
        max_power_w=30000,
        max_export_power_w=30000,
        min_soc_percent=1,
    )
    values.update(limits)
    return WriteProfile(**values)


def inverter_info(
    serial="TEST123", *, parallel=False, master=True, plant=7, power=30000
):
    return {
        "sn": serial,
        "parallel": parallel,
        "equipMode": int(master),
        "plant": {"id": plant},
        "ratePower": power,
    }


def safe_settings(**changes):
    """A complete independent device state for write-pipeline tests."""
    result = dict(
        chargeCurrent=50,
        dischargeCurrent=50,
        sdBatteryCurrent=50,
        batteryMaxCurrentCharge=300,
        batteryMaxCurrentDischarge=300,
        batteryShutdownCap=10,
        batteryLowCap=20,
        batteryRestartCap=30,
        solarSell=0,
        solarMaxSellPower=6000,
        pvMaxLimit=30000,
    )
    for n in range(1, 7):
        result.update(
            {
                f"sellTime{n}": f"{(n - 1) * 4:02}:00",
                f"time{n}on": 0,
                f"sellTime{n}on": 0,
                f"cap{n}": 50,
                f"sellTime{n}Pac": 0,
            }
        )
    result.update(changes)
    return result
