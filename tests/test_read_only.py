"""Read-only safety coverage across configuration, controllers and transport."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sunsynk import async_setup, async_unload_entry
from custom_components.sunsynk.api.client import SunsynkClient
from custom_components.sunsynk.config_flow import _prepare_read_only
from custom_components.sunsynk.const import DOMAIN
from custom_components.sunsynk.coordinator import SunsynkCoordinator
from custom_components.sunsynk.write_policy import SunsynkReadOnlyError, WritePolicy
from tests.conftest import (
    FakeResponse,
    fake_session,
    inverter_info,
    write_profile,
    safe_settings,
)
from tests.test_config_flow import _options_flow, _user_input
from tests.test_tariff import _make_manager, _price_state
from tests.test_virtual_slots import _make_scheduler


def _coordinator(mode="read_only"):
    """Use the real write pipeline with no network or HA polling side effects."""
    coordinator = object.__new__(SunsynkCoordinator)
    coordinator.hass = MagicMock()
    coordinator._entry_id = "entry"
    coordinator.write_policy = WritePolicy(mode)
    coordinator.serials = ["TEST123"]
    coordinator.write_profiles = {"TEST123": write_profile()}
    coordinator.data = {
        "TEST123": {
            "inverter": inverter_info(),
            "settings": {"chargeCurrent": 50},
            "battery": {"soc": 50},
        }
    }
    coordinator._auth = MagicMock(_api_server="api.sunsynk.net")
    coordinator._auth.async_get_token = AsyncMock(return_value="token")
    coordinator._async_get_session = AsyncMock(return_value=MagicMock())
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_close = AsyncMock()
    SunsynkCoordinator._ensure_write_state(coordinator)
    return coordinator


@pytest.mark.parametrize("mode", ["read_only", None, "", "invalid", True])
def test_policy_fails_closed(mode):
    policy = WritePolicy(mode)
    assert policy.mode == "read_only"
    with pytest.raises(SunsynkReadOnlyError, match="Select Read/write"):
        policy.ensure_writable()


def test_policy_explicit_opt_in_and_revocation():
    policy = WritePolicy("read_write")
    assert policy.mode == "read_write"
    policy.ensure_writable()
    policy.revoke()
    with pytest.raises(SunsynkReadOnlyError):
        policy.ensure_writable()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["settings", "price", "post"])
async def test_api_default_blocks_every_write(operation):
    client = SunsynkClient("api.sunsynk.net", "token")
    session = fake_session()
    with pytest.raises(SunsynkReadOnlyError):
        if operation == "settings":
            await client.async_write_settings(session, "TEST123", {"chargeCurrent": 80})
        elif operation == "price":
            await client.async_set_plant_income(session, "plant", {"price": 0.1})
        else:
            await client._post(session, "https://api.sunsynk.net/write", {})
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_old_api_client_observes_revoked_shared_policy():
    policy = WritePolicy("read_write")
    client = SunsynkClient("api.sunsynk.net", "token", write_policy=policy)
    policy.revoke()
    session = fake_session()
    with pytest.raises(SunsynkReadOnlyError):
        await client.async_write_settings(session, "TEST123", {})
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_api_reads_work_without_write_access():
    client = SunsynkClient("api.sunsynk.net", "token")
    session = fake_session(get=FakeResponse({"msg": "Success", "data": {"soc": 50}}))
    assert await client.async_get_settings(session, "TEST123") == {"soc": 50}
    session.post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["single", "batch", "price", "dispatch"])
async def test_coordinator_rejects_before_auth_queue_or_cache_changes(operation):
    coordinator = _coordinator()
    with pytest.raises(SunsynkReadOnlyError):
        if operation == "single":
            await coordinator.async_write_setting("TEST123", "chargeCurrent", 80)
        elif operation == "batch":
            await coordinator.async_write_settings("TEST123", {"chargeCurrent": 80})
        elif operation == "price":
            await coordinator.async_write_plant_price("TEST123", 0.2)
        else:
            await coordinator._async_execute_setting_batch(
                "TEST123", {"chargeCurrent": 80}
            )
    coordinator._auth.async_get_token.assert_not_awaited()
    coordinator._async_get_session.assert_not_awaited()
    assert not coordinator.writes_pending
    assert coordinator.data["TEST123"]["settings"]["chargeCurrent"] == 50


@pytest.mark.asyncio
async def test_queued_write_cannot_dispatch_after_revocation():
    coordinator = _coordinator("read_write")
    task = asyncio.create_task(
        coordinator.async_write_setting("TEST123", "chargeCurrent", 80)
    )
    await asyncio.sleep(0)
    assert coordinator.writes_pending
    coordinator.write_policy.revoke()
    with pytest.raises(SunsynkReadOnlyError):
        await task
    coordinator._async_get_session.assert_not_awaited()
    assert not coordinator.writes_pending


@pytest.mark.asyncio
async def test_revocation_during_auth_is_checked_at_actual_post():
    coordinator = _coordinator("read_write")
    session = fake_session(
        get=FakeResponse({"msg": "Success", "data": inverter_info()})
    )
    coordinator._async_get_session.return_value = session

    async def revoke_during_auth(_session):
        coordinator.write_policy.revoke()
        return "token"

    coordinator._auth.async_get_token.side_effect = revoke_during_auth
    with pytest.raises(SunsynkReadOnlyError):
        await coordinator.async_write_setting("TEST123", "chargeCurrent", 80)
    session.post.assert_not_called()
    assert coordinator.data["TEST123"]["settings"]["chargeCurrent"] == 50


@pytest.mark.parametrize("state", ["queue", "scheduled", "task", "lock"])
def test_pending_writes_cover_every_stage(state):
    coordinator = _coordinator("read_write")
    if state == "queue":
        coordinator._pending_setting_writes["TEST123"] = {"chargeCurrent": 80}
    elif state == "scheduled":
        coordinator._write_drain_scheduled.add("TEST123")
    elif state == "task":
        coordinator._write_drain_tasks["TEST123"] = MagicMock()
    else:
        coordinator._write_locks["TEST123"] = MagicMock(locked=lambda: True)
    assert coordinator.writes_pending


def _mode_context(coordinator, controller=None):
    return SimpleNamespace(
        data={
            DOMAIN: {
                "entry": coordinator,
                "entry_tariff": controller,
                "entry_vslots": {},
            }
        }
    )


def test_readonly_transition_with_no_running_entry():
    assert _prepare_read_only(SimpleNamespace(data={}), "entry")


@pytest.mark.parametrize("enabled,pending", [(True, False), (False, True)])
@pytest.mark.parametrize("kind", ["tariff", "scheduler"])
def test_mode_transition_rejects_active_or_unrestored_controller(
    enabled, pending, kind
):
    coordinator = _coordinator("read_write")
    controller = SimpleNamespace(is_enabled=enabled, restoration_pending=pending)
    hass = _mode_context(coordinator, controller if kind == "tariff" else None)
    if kind == "scheduler":
        hass.data[DOMAIN]["entry_vslots"] = {"TEST123": controller}
    assert not _prepare_read_only(hass, "entry")
    assert coordinator.write_policy.mode == "read_write"


def test_mode_transition_rejects_pending_write_then_revokes_idle_policy():
    coordinator = _coordinator("read_write")
    hass = _mode_context(coordinator)
    coordinator._write_drain_scheduled.add("TEST123")
    assert not _prepare_read_only(hass, "entry")
    coordinator._write_drain_scheduled.clear()
    assert _prepare_read_only(hass, "entry")
    assert coordinator.write_policy.mode == "read_only"


@pytest.mark.asyncio
async def test_options_reject_transition_without_saving_then_allow_cleanup():
    flow = _options_flow()
    coordinator = _coordinator("read_write")
    controller = SimpleNamespace(is_enabled=True, restoration_pending=False)
    flow.hass.data = _mode_context(coordinator, controller).data
    result = await flow.async_step_init(
        _user_input(password="", access_mode="read_only")
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "control_active"
    flow.hass.config_entries.async_update_entry.assert_not_called()
    assert coordinator.write_policy.mode == "read_write"
    controller.is_enabled = False
    result = await flow.async_step_init(
        _user_input(password="", access_mode="read_only")
    )
    assert result["type"] == "create_entry"
    assert result["data"]["access_mode"] == "read_only"
    assert coordinator.write_policy.mode == "read_only"


@pytest.mark.asyncio
async def test_options_opt_in_does_not_enable_old_controllers():
    flow = _options_flow()
    result = await flow.async_step_init(
        _user_input(password="", access_mode="read_write")
    )
    assert result["data"]["access_mode"] == "read_write"


@pytest.mark.parametrize("kind", ["tariff", "scheduler"])
def test_controllers_cannot_enable_in_readonly(kind, mock_hass, mock_coordinator):
    mock_coordinator.write_policy = WritePolicy()
    controller = (
        _make_manager(mock_hass, mock_coordinator)
        if kind == "tariff"
        else _make_scheduler(mock_hass, mock_coordinator)
    )
    with pytest.raises(SunsynkReadOnlyError):
        controller.set_enabled(True)
    assert not controller.is_enabled
    mock_hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_tariff_failed_restore_retains_ownership_then_retry_clears(
    mock_hass, mock_coordinator
):
    manager = _make_manager(mock_hass, mock_coordinator)
    manager._charging_active_serials.add("TEST123")
    mock_coordinator.async_write_settings.side_effect = RuntimeError("offline")
    assert not await manager.async_shutdown()
    assert manager.restoration_pending
    mock_coordinator.async_write_settings.side_effect = None
    assert await manager.async_shutdown()
    assert not manager.restoration_pending


@pytest.mark.asyncio
async def test_missing_tariff_restore_current_keeps_pending_state(
    mock_hass, mock_coordinator
):
    manager = _make_manager(mock_hass, mock_coordinator, normal_charge=None)
    manager._charging_active_serials.add("TEST123")
    assert not await manager.async_shutdown()
    assert manager.restoration_pending
    mock_coordinator.async_write_settings.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "price,key", [("0.05", "chargeCurrent"), ("0.40", "dischargeCurrent")]
)
async def test_tariff_ambiguous_override_is_tracked(
    price, key, mock_hass, mock_coordinator
):
    mock_hass.states.get.return_value = _price_state(price)
    mock_coordinator.async_write_setting.side_effect = RuntimeError(
        "verification failed"
    )
    manager = _make_manager(mock_hass, mock_coordinator)
    manager._enabled = True
    with pytest.raises(RuntimeError, match="verification failed"):
        await manager._evaluate()
    assert manager.restoration_pending
    mock_coordinator.async_write_setting.assert_awaited_once_with("TEST123", key, 100)


@pytest.mark.asyncio
async def test_scheduler_failed_current_restoration_is_retained_for_retry(
    mock_hass, mock_coordinator
):
    scheduler = _make_scheduler(mock_hass, mock_coordinator, normal_charge_current=50)
    scheduler._owned_current_keys.add("chargeCurrent")
    mock_coordinator.async_write_settings.side_effect = RuntimeError("offline")
    assert not await scheduler.async_shutdown()
    assert scheduler.restoration_pending
    mock_coordinator.async_write_settings.side_effect = None
    assert await scheduler.async_shutdown()
    assert not scheduler.restoration_pending


@pytest.mark.asyncio
async def test_missing_scheduler_current_restoration_blocks_mode_change(
    mock_hass, mock_coordinator
):
    scheduler = _make_scheduler(mock_hass, mock_coordinator)
    scheduler._owned_current_keys.add("chargeCurrent")
    assert not await scheduler.async_shutdown()
    assert scheduler.restoration_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tariff", "scheduler"])
async def test_pending_shutdown_task_blocks_mode_change(
    kind, mock_hass, mock_coordinator
):
    mock_hass.services.async_call = AsyncMock()
    mock_hass.async_create_task.side_effect = asyncio.create_task
    controller = (
        _make_manager(mock_hass, mock_coordinator)
        if kind == "tariff"
        else _make_scheduler(mock_hass, mock_coordinator)
    )
    if kind == "tariff":
        controller._charging_active_serials.add("TEST123")
    controller.set_enabled(False)
    assert controller.restoration_pending
    await controller._restore_task
    assert not controller.restoration_pending


@pytest.mark.asyncio
async def test_manual_services_reject_without_post(mock_hass):
    coordinator = _coordinator()
    mock_hass.data = _mode_context(coordinator).data
    handlers = {}
    mock_hass.services.async_register.side_effect = (
        lambda domain, service, handler, schema: handlers.setdefault(service, handler)
    )
    mock_hass.http.async_register_static_paths = AsyncMock()
    await async_setup(mock_hass, {})
    for service, fields in [
        ("force_charge", {"current": 80}),
        ("force_discharge", {"current": 80}),
        ("set_work_mode", {"mode": 1}),
    ]:
        with pytest.raises(SunsynkReadOnlyError):
            await handlers[service](
                SimpleNamespace(data={"serial": "TEST123", **fields})
            )
    coordinator._async_get_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_unload_cannot_restore_to_cloud(mock_hass):
    coordinator = _coordinator()
    manager = _make_manager(mock_hass, coordinator)
    manager._charging_active_serials.add("TEST123")
    scheduler = _make_scheduler(mock_hass, coordinator, normal_charge_current=50)
    scheduler._original_settings = {"time1on": 0}
    mock_hass.data = {
        DOMAIN: {
            "entry": coordinator,
            "entry_tariff": manager,
            "entry_vslots": {"TEST123": scheduler},
        }
    }
    mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    assert await async_unload_entry(mock_hass, SimpleNamespace(entry_id="entry"))
    coordinator._async_get_session.assert_not_awaited()
    assert manager.restoration_pending
    assert scheduler.restoration_pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["number", "switch_on", "switch_off", "text", "plant_price"]
)
async def test_entity_actions_use_guard_without_changing_values(kind):
    from custom_components.sunsynk.number import (
        WRITABLE_NUMBERS,
        PlantEnergyPriceNumberEntity,
        SunsynkNumberEntity,
    )
    from custom_components.sunsynk.switch import WRITABLE_SWITCHES, SunsynkSwitchEntity
    from custom_components.sunsynk.text import WRITABLE_TEXTS, SunsynkTextEntity

    coordinator = _coordinator()
    coordinator.async_contexts = lambda: set()
    device = MagicMock()
    if kind == "number":
        entity = SunsynkNumberEntity(
            coordinator,
            "TEST123",
            next(d for d in WRITABLE_NUMBERS if d.setting_key == "chargeCurrent"),
            device,
        )
        action = entity.async_set_native_value(80)
    elif kind.startswith("switch"):
        entity = SunsynkSwitchEntity(
            coordinator, "TEST123", WRITABLE_SWITCHES[0], device
        )
        action = (
            entity.async_turn_on() if kind == "switch_on" else entity.async_turn_off()
        )
    elif kind == "text":
        entity = SunsynkTextEntity(coordinator, "TEST123", WRITABLE_TEXTS[0], device)
        action = entity.async_set_value("10:00")
    else:
        entity = PlantEnergyPriceNumberEntity(coordinator, "TEST123", device)
        action = entity.async_set_native_value(0.2)
    with pytest.raises(SunsynkReadOnlyError):
        await action
    coordinator._async_get_session.assert_not_awaited()
    assert coordinator.data["TEST123"]["settings"] == {"chargeCurrent": 50}


@pytest.mark.asyncio
async def test_readonly_local_schedule_storage_remains_available(
    mock_hass, mock_coordinator
):
    from custom_components.sunsynk.virtual_slots import VirtualSlot

    mock_coordinator.write_policy = WritePolicy()
    scheduler = _make_scheduler(mock_hass, mock_coordinator)
    scheduler._store.async_save = AsyncMock()
    await scheduler.async_set_slot(
        VirtualSlot(slot_id=1, start="10:00", end="11:00", mode="charge")
    )
    scheduler._store.async_save.assert_awaited_once()
    mock_coordinator.async_write_settings.assert_not_awaited()
    mock_coordinator.async_write_setting.assert_not_awaited()
    assert not scheduler.is_enabled


@pytest.mark.asyncio
async def test_readwrite_pipeline_posts_and_verifies_with_shared_policy():
    coordinator = _coordinator("read_write")
    session = fake_session(
        post=FakeResponse({"msg": "Success"}),
        get=FakeResponse({"msg": "Success", "data": {"chargeCurrent": 80}}),
    )

    def read(url, **kwargs):
        return FakeResponse(
            {
                "msg": "Success",
                "data": inverter_info()
                if "read" not in url
                else safe_settings(chargeCurrent=80 if session.post.called else 50),
            }
        )

    session.get.side_effect = read
    coordinator._async_get_session.return_value = session
    with patch("custom_components.sunsynk.coordinator.asyncio.sleep", AsyncMock()):
        await coordinator.async_write_setting("TEST123", "chargeCurrent", 80)
    session.post.assert_called_once()
    assert coordinator.data["TEST123"]["settings"]["chargeCurrent"] == 80
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_ha_setup_form_defaults_to_readonly(
    hass, enable_custom_integrations
):
    from homeassistant.config_entries import SOURCE_USER

    with (
        patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()),
        patch("custom_components.sunsynk.async_setup", AsyncMock(return_value=True)),
        patch(
            "custom_components.sunsynk.async_setup_entry", AsyncMock(return_value=True)
        ),
        patch(
            "custom_components.sunsynk.config_flow._async_validate_credentials",
            AsyncMock(),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] == "form"
        values = result["data_schema"](_user_input())
        assert values["access_mode"] == "read_only"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _user_input()
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["data"]["access_mode"] == "read_only"


@pytest.mark.asyncio
async def test_scheduler_retries_failed_slot_disable(mock_hass, mock_coordinator):
    scheduler = _make_scheduler(mock_hass, mock_coordinator)
    mock_coordinator.async_write_settings.side_effect = RuntimeError("offline")
    assert not await scheduler._async_shutdown(force_disable=True)
    assert scheduler.restoration_pending
    mock_coordinator.async_write_settings.side_effect = None
    assert await scheduler.async_shutdown()
    mock_coordinator.async_write_settings.assert_awaited_with(
        "TEST123", {"time1on": 0, "time6on": 0}
    )
    assert not scheduler.restoration_pending


@pytest.mark.asyncio
async def test_current_restore_retry_does_not_disable_restored_timer(
    mock_hass, mock_coordinator
):
    scheduler = _make_scheduler(mock_hass, mock_coordinator, normal_charge_current=50)
    scheduler._original_settings = {"time1on": 1}
    scheduler._owned_current_keys.add("chargeCurrent")
    mock_coordinator.async_write_settings.side_effect = [None, RuntimeError("offline")]
    assert not await scheduler.async_shutdown()
    mock_coordinator.async_write_settings.reset_mock(side_effect=True)
    assert await scheduler.async_shutdown()
    mock_coordinator.async_write_settings.assert_awaited_once_with(
        "TEST123", {"chargeCurrent": 50}
    )
    assert not scheduler.restoration_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "invalid", "read_only", "read_write"])
async def test_setup_resolves_mode_and_shares_policy(mode):
    from custom_components.sunsynk import async_setup_entry
    from tests.test_integration_lifecycle import _entry, _hass

    entry = _entry()
    entry.options.pop("panel_kwp")
    if mode is not None:
        entry.options["access_mode"] = mode
    coordinator = MagicMock(
        async_config_entry_first_refresh=AsyncMock(),
        data={},
        write_target_serials=["TEST123"],
    )
    with (
        patch(
            "custom_components.sunsynk.SunsynkCoordinator", return_value=coordinator
        ) as factory,
        patch(
            "custom_components.sunsynk.VirtualSlotScheduler",
            return_value=MagicMock(async_load=AsyncMock()),
        ),
        patch(
            "custom_components.sunsynk.Store",
            return_value=MagicMock(async_load=AsyncMock(return_value=None)),
        ),
        patch("custom_components.sunsynk._migrate_virtual_slot_entity_unique_ids"),
        patch("custom_components.sunsynk.TariffChargingManager"),
    ):
        assert await async_setup_entry(_hass(), entry)
    policy = factory.call_args.kwargs["write_policy"]
    assert policy.mode == ("read_write" if mode == "read_write" else "read_only")


@pytest.mark.asyncio
async def test_tariff_cannot_evaluate_after_policy_revocation(
    mock_hass, mock_coordinator
):
    manager = _make_manager(mock_hass, mock_coordinator)
    manager._enabled = True
    mock_coordinator.write_policy.revoke()
    with pytest.raises(SunsynkReadOnlyError):
        await manager._evaluate()
    mock_coordinator.async_write_setting.assert_not_awaited()
    assert not manager._charging_active_serials
    assert not manager._discharging_active_serials


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["read_only", "read_write"])
async def test_reload_listener_revokes_saved_readonly_before_unload(mode):
    from custom_components.sunsynk import _async_update_listener

    coordinator = _coordinator("read_write")
    hass = _mode_context(coordinator)

    async def reload(entry_id):
        assert entry_id == "entry"
        assert coordinator.write_policy.mode == mode

    hass.config_entries = SimpleNamespace(async_reload=AsyncMock(side_effect=reload))
    entry = SimpleNamespace(entry_id="entry", options={"access_mode": mode}, data={})
    await _async_update_listener(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitoring_only_setup_keeps_local_schedulers():
    from custom_components.sunsynk import async_setup_entry
    from tests.test_integration_lifecycle import _entry, _hass

    entry = _entry()
    entry.options.pop("panel_kwp")
    coordinator = MagicMock(
        async_config_entry_first_refresh=AsyncMock(),
        data={},
        serials=["INV1", "INV2"],
        write_target_serials=[],
        write_profiles={},
    )
    hass = _hass()
    scheduler = MagicMock(async_load=AsyncMock())
    with (
        patch("custom_components.sunsynk.SunsynkCoordinator", return_value=coordinator),
        patch(
            "custom_components.sunsynk.VirtualSlotScheduler", return_value=scheduler
        ) as factory,
        patch(
            "custom_components.sunsynk.Store",
            return_value=MagicMock(async_load=AsyncMock(return_value=None)),
        ),
        patch("custom_components.sunsynk.TariffChargingManager"),
        patch("custom_components.sunsynk._migrate_virtual_slot_entity_unique_ids"),
    ):
        assert await async_setup_entry(hass, entry)
    assert {call.kwargs["serial"] for call in factory.call_args_list} == {
        "INV1",
        "INV2",
    }


@pytest.mark.asyncio
async def test_profile_changes_are_rejected_during_active_control():
    flow = _options_flow()
    coordinator = _coordinator("read_write")
    flow.hass.data = _mode_context(
        coordinator, SimpleNamespace(is_enabled=True, restoration_pending=False)
    ).data
    result = await flow.async_step_init(
        _user_input(password="", access_mode="read_write")
    )
    assert result["errors"]["base"] == "control_active"
    assert coordinator.write_policy.mode == "read_write"
