"""Tests for unambiguous per-inverter service routing."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sunsynk import (
    _dashboard_enabled,
    _find_virtual_slot_scheduler,
    _migrate_virtual_slot_entity_unique_ids,
    async_setup,
    async_unload_entry,
)
from custom_components.sunsynk.const import DOMAIN
from custom_components.sunsynk.coordinator import SunsynkCoordinator
from tests.conftest import write_profile, inverter_info


def _coordinator() -> SunsynkCoordinator:
    coordinator = object.__new__(SunsynkCoordinator)
    coordinator.serials = ["SLAVE1", "MASTER1", "INV2"]
    coordinator.write_profiles = {"MASTER1": write_profile("MASTER1", ["MASTER1", "SLAVE1"]), "INV2": write_profile("INV2")}
    coordinator.data = {
        "SLAVE1": {"inverter": inverter_info("SLAVE1", parallel=True, master=False)},
        "MASTER1": {"inverter": inverter_info("MASTER1", parallel=True)},
        "INV2": {"inverter": inverter_info("INV2")},
    }
    return coordinator


def test_dashboard_is_opt_in_and_options_override_entry_data():
    assert not _dashboard_enabled(SimpleNamespace(data={}, options={}))
    assert _dashboard_enabled(
        SimpleNamespace(data={"create_dashboard": True}, options={})
    )
    assert not _dashboard_enabled(
        SimpleNamespace(
            context=SimpleNamespace(user_id=None),
            data={"create_dashboard": True},
            options={"create_dashboard": False},
        )
    )


@pytest.mark.asyncio
async def test_global_setup_does_not_mutate_lovelace_without_entry_opt_in():
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.services.async_register = MagicMock()

    with patch(
        "custom_components.sunsynk._async_register_lovelace_resource",
        new=AsyncMock(),
    ) as register_resource:
        assert await async_setup(hass, {}) is True

    register_resource.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_services_route_and_execute_on_minimal_ha():
    coordinator = _coordinator()
    coordinator.async_write_setting = AsyncMock()
    scheduler = MagicMock(async_set_slot=AsyncMock(), async_clear_slot=AsyncMock())
    handlers = {}
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "entry": coordinator,
            "entry_vslots": {"MASTER1": scheduler, "INV2": MagicMock()},
        }
    }
    hass.http.async_register_static_paths = AsyncMock()
    hass.async_run_hass_job.side_effect = lambda job, call: job.target(call)
    hass.services.async_register.side_effect = (
        lambda domain, service, handler, schema, *args, **kwargs: handlers.setdefault(service, handler)
    )

    await async_setup(hass, {})

    await handlers["force_charge"](
        SimpleNamespace(data={"serial": "SLAVE1", "current": 20}, context=SimpleNamespace(user_id=None))
    )
    await handlers["force_discharge"](
        SimpleNamespace(data={"serial": "INV2", "current": 30}, context=SimpleNamespace(user_id=None))
    )
    await handlers["set_work_mode"](
        SimpleNamespace(data={"serial": "INV2", "mode": 4}, context=SimpleNamespace(user_id=None))
    )
    await handlers["set_virtual_slot"](
        SimpleNamespace(
            context=SimpleNamespace(user_id=None),
            data={
                "serial": "SLAVE1",
                "slot_id": 1,
                "start": "22:00",
                "end": "06:00",
                "mode": "charge",
                "weekdays": ["mon", "sun"],
                "current": 20,
                "target_soc": 90,
                "sell_power": 0,
                "priority": 5,
                "enabled": True,
            }
        )
    )
    await handlers["clear_virtual_slot"](
        SimpleNamespace(data={"serial": "SLAVE1", "slot_id": 1}, context=SimpleNamespace(user_id=None))
    )

    coordinator.async_write_setting.assert_any_await(
        "MASTER1", "chargeCurrent", 20
    )
    coordinator.async_write_setting.assert_any_await(
        "INV2", "dischargeCurrent", 30
    )
    coordinator.async_write_setting.assert_any_await("INV2", "sysWorkMode", 4)
    scheduler.async_set_slot.assert_awaited_once()
    scheduler.async_clear_slot.assert_awaited_once_with(1)

    for service in ("force_charge", "force_discharge", "set_work_mode", "set_virtual_slot", "clear_virtual_slot"):
        with pytest.raises(ValueError, match="No Sunsynk inverter"):
            data = {"serial": "UNKNOWN", "current": 1, "mode": 1, "slot_id": 1}
            await handlers[service](SimpleNamespace(data=data, context=SimpleNamespace(user_id=None)))


def test_virtual_slot_service_routes_parallel_slave_to_master_scheduler():
    coordinator = _coordinator()
    master_scheduler = MagicMock()
    independent_scheduler = MagicMock()
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "entry": coordinator,
                "entry_vslots": {
                    "MASTER1": master_scheduler,
                    "INV2": independent_scheduler,
                },
            }
        }
    )

    assert _find_virtual_slot_scheduler(hass, "SLAVE1") is master_scheduler
    assert _find_virtual_slot_scheduler(hass, "MASTER1") is master_scheduler
    assert _find_virtual_slot_scheduler(hass, "INV2") is independent_scheduler


def test_virtual_slot_service_rejects_unknown_serial():
    coordinator = _coordinator()
    hass = SimpleNamespace(data={DOMAIN: {"entry": coordinator, "entry_vslots": {}}})

    assert _find_virtual_slot_scheduler(hass, "UNKNOWN") is None


def test_virtual_slot_entity_migration_preserves_existing_entity_ids():
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda platform, domain, unique_id: {
        "entry_vslots_enabled": "switch.virtual_slot_scheduler",
        "entry_vslots_state": "sensor.virtual_slot_scheduler",
    }.get(unique_id)

    with patch(
        "custom_components.sunsynk.er.async_get", return_value=registry
    ):
        _migrate_virtual_slot_entity_unique_ids(
            MagicMock(), "entry", "MASTER1"
        )

    assert registry.async_update_entity.call_count == 2
    registry.async_update_entity.assert_any_call(
        "switch.virtual_slot_scheduler",
        new_unique_id="MASTER1_vslots_enabled",
    )
    registry.async_update_entity.assert_any_call(
        "sensor.virtual_slot_scheduler",
        new_unique_id="MASTER1_vslots_state",
    )


def _record(events: list[str], name: str):
    def _side_effect(*args, **kwargs):
        events.append(name)

    return _side_effect


@pytest.mark.asyncio
async def test_unload_restores_before_closing_coordinator():
    events: list[str] = []
    entry = MagicMock(entry_id="entry")
    coordinator = MagicMock(
        async_close=AsyncMock(side_effect=_record(events, "coordinator_close"))
    )
    tariff = MagicMock(
        stop=MagicMock(side_effect=_record(events, "tariff_stop")),
        async_shutdown=AsyncMock(side_effect=_record(events, "tariff_restore")),
    )
    scheduler = MagicMock(
        stop=MagicMock(side_effect=_record(events, "scheduler_stop")),
        async_shutdown=AsyncMock(side_effect=_record(events, "scheduler_restore")),
    )
    config_entries = MagicMock(
        async_unload_platforms=AsyncMock(
            side_effect=lambda *args: events.append("platform_unload") or True
        )
    )
    hass = SimpleNamespace(
        config_entries=config_entries,
        data={
            DOMAIN: {
                "entry": coordinator,
                "entry_tariff": tariff,
                "entry_vslots": {"INV1": scheduler},
            }
        },
    )

    assert await async_unload_entry(hass, entry) is True

    assert events == [
        "tariff_stop",
        "scheduler_stop",
        "platform_unload",
        "tariff_restore",
        "scheduler_restore",
        "coordinator_close",
    ]
    assert hass.data[DOMAIN] == {}


@pytest.mark.asyncio
async def test_failed_platform_unload_restarts_listeners_without_restoring():
    entry = MagicMock(entry_id="entry")
    coordinator = MagicMock(async_close=AsyncMock())
    tariff = MagicMock(async_shutdown=AsyncMock())
    scheduler = MagicMock(async_shutdown=AsyncMock())
    hass = SimpleNamespace(
        config_entries=MagicMock(
            async_unload_platforms=AsyncMock(return_value=False)
        ),
        data={
            DOMAIN: {
                "entry": coordinator,
                "entry_tariff": tariff,
                "entry_vslots": {"INV1": scheduler},
            }
        },
    )

    assert await async_unload_entry(hass, entry) is False

    tariff.stop.assert_called_once()
    tariff.start.assert_called_once()
    scheduler.stop.assert_called_once()
    scheduler.start.assert_called_once()
    tariff.async_shutdown.assert_not_awaited()
    scheduler.async_shutdown.assert_not_awaited()
    coordinator.async_close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["force_charge", "force_discharge", "set_work_mode", "set_virtual_slot", "clear_virtual_slot"])
@pytest.mark.parametrize("serial", ["MASTER1", "SLAVE1"])
@pytest.mark.parametrize("user", [None, SimpleNamespace(is_admin=False)])
async def test_control_services_reject_unknown_and_non_admin_users(hass, service, serial, user):
    """Permission checks run before routing or changing local/device state."""
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    coordinator = _coordinator()
    coordinator.async_write_setting = AsyncMock()
    scheduler = MagicMock(async_set_slot=AsyncMock(), async_clear_slot=AsyncMock())
    hass.data[DOMAIN] = {"entry": coordinator, "entry_vslots": {"MASTER1": scheduler}}
    with patch.object(hass, "http", MagicMock(async_register_static_paths=AsyncMock())):
        await async_setup(hass, {})
    data = _service_data(service, serial)
    with patch.object(hass.auth, "async_get_user", new=AsyncMock(return_value=user)):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(DOMAIN, service, data, blocking=True, context=Context(user_id="restricted"))
    coordinator.async_write_setting.assert_not_awaited()
    scheduler.async_set_slot.assert_not_awaited()
    scheduler.async_clear_slot.assert_not_awaited()


def _service_data(service, serial):
    """Valid service data so schema rejection cannot mask authorization errors."""
    if service in ("force_charge", "force_discharge"):
        return {"serial": serial, "current": 20}
    if service == "set_work_mode":
        return {"serial": serial, "mode": 2}
    if service == "clear_virtual_slot":
        return {"serial": serial, "slot_id": 1}
    return {"serial": serial, "slot_id": 1, "start": "10:00", "end": "11:00", "mode": "charge", "current": 20}


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["force_charge", "force_discharge", "set_work_mode", "set_virtual_slot", "clear_virtual_slot"])
@pytest.mark.parametrize("user_id", [None, "administrator"])
async def test_control_services_allow_admin_and_system_automations(hass, service, user_id):
    """Allowed callers retain slave-to-master routing and local schedule editing."""
    from homeassistant.core import Context

    coordinator = _coordinator()
    coordinator.async_write_setting = AsyncMock()
    scheduler = MagicMock(async_set_slot=AsyncMock(), async_clear_slot=AsyncMock())
    hass.data[DOMAIN] = {"entry": coordinator, "entry_vslots": {"MASTER1": scheduler}}
    with patch.object(hass, "http", MagicMock(async_register_static_paths=AsyncMock())):
        await async_setup(hass, {})
    with patch.object(hass.auth, "async_get_user", new=AsyncMock(return_value=SimpleNamespace(is_admin=True))) as lookup:
        await hass.services.async_call(DOMAIN, service, _service_data(service, "SLAVE1"), blocking=True, context=Context(user_id=user_id))
    if user_id is None:
        lookup.assert_not_awaited()
    else:
        lookup.assert_awaited_once_with(user_id)
    if service == "set_virtual_slot":
        scheduler.async_set_slot.assert_awaited_once()
    elif service == "clear_virtual_slot":
        scheduler.async_clear_slot.assert_awaited_once_with(1)
    else:
        assert coordinator.async_write_setting.await_args.args[0] == "MASTER1"
