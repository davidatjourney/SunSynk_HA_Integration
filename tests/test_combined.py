"""Combined installation monitoring must not double-count shared batteries."""

from unittest.mock import MagicMock

import pytest

from custom_components.sunsynk.combined import combined_prefix, shared_master
from custom_components.sunsynk.const import ALL_STATIC_SENSORS
from custom_components.sunsynk.sensor import CombinedSystemSensor


def context():
    data = {
        serial: {
            "inverter": {"plant": {"id": 10}},
            "settings": {"parallel": "1", "equipMode": role},
            "battery": {
                "power": power,
                "soc": 83,
                "voltage": 52,
                "capacity": 400,
                "temp": temp,
            },
            "pv": {"pac": pv, "etoday": energy},
            "grid": {"pac": grid},
            "load": {"totalPower": load},
        }
        for serial, role, power, temp, pv, energy, grid, load in [
            ("SLAVE", "0", 317, -100, 1000, 3.5, -20, 241),
            ("MASTER", "1", 308, 22.4, 2000, 13.2, 10, 250),
        ]
    }
    data["SLAVE"]["battery"].update(soc=50, voltage=48)
    coordinator = MagicMock(
        serials=["SLAVE", "MASTER"], data=data, last_update_success=True
    )
    return coordinator


def sensor(coordinator, key):
    desc = next(d for d in ALL_STATIC_SENSORS if d.key == key)
    return CombinedSystemSensor(coordinator, "entry_combined_", desc, {})


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("pv_pac", 3000),
        ("pv_etoday", 16.7),
        ("battery_power", 625),
        ("grid_pac", -10),
        ("load_total_power", 491),
        ("battery_soc", 83),
        ("battery_voltage", 52),
        ("battery_capacity", 400),
        ("battery_temp", 22.4),
    ],
)
def test_combined_readings_sum_power_but_take_shared_battery_once(key, expected):
    c = context()
    entity = sensor(c, key)
    assert entity.native_value == expected
    assert entity.unique_id == f"entry_combined_{key}"


@pytest.mark.parametrize("bad", [None, "--", True, "nan", "inf"])
def test_any_missing_or_invalid_component_makes_total_unknown(bad):
    c = context()
    c.data["SLAVE"]["pv"]["pac"] = bad
    assert sensor(c, "pv_pac").native_value is None


def test_zero_is_valid_and_offline_member_is_not_a_partial_total():
    c = context()
    c.data["SLAVE"]["pv"]["pac"] = 0
    assert sensor(c, "pv_pac").native_value == 2000
    c.data["SLAVE"]["inverter"] = {}
    assert sensor(c, "pv_pac").native_value is None


def test_invalid_master_temperature_is_unknown():
    c = context()
    c.data["MASTER"]["battery"]["temp"] = -100
    assert sensor(c, "battery_temp").native_value is None


def test_membership_identity_survives_reorder_but_not_member_changes():
    assert combined_prefix("entry", ["a", "b"]) == combined_prefix("entry", ["b", "a"])
    assert combined_prefix("entry", ["a", "b"]) != combined_prefix("entry", ["a", "c"])


@pytest.mark.parametrize(
    "case",
    [
        "single",
        "empty",
        "no_info",
        "no_plant",
        "bool_plant",
        "not_parallel",
        "unknown_role",
        "two_masters",
        "no_master",
        "different_plants",
    ],
)
def test_unverified_or_multiple_installations_cannot_produce_system_totals(case):
    c = context()
    if case == "single":
        c.serials = ["MASTER"]
    elif case == "empty":
        c.data = {}
    elif case == "no_info":
        c.data["SLAVE"]["inverter"] = {}
    elif case == "no_plant":
        c.data["SLAVE"]["inverter"]["plant"] = {}
    elif case == "bool_plant":
        c.data["SLAVE"]["inverter"]["plant"]["id"] = True
    elif case == "not_parallel":
        c.data["SLAVE"]["settings"]["parallel"] = 0
    elif case == "unknown_role":
        c.data["SLAVE"]["settings"]["equipMode"] = 2
    elif case == "two_masters":
        c.data["SLAVE"]["settings"]["equipMode"] = 1
    elif case == "no_master":
        c.data["MASTER"]["settings"]["equipMode"] = 0
    else:
        c.data["SLAVE"]["inverter"]["plant"]["id"] = 11
    assert shared_master(c.serials, c.data) is None


def test_master_can_be_identified_from_inverter_metadata():
    c = context()
    for payload in c.data.values():
        payload["inverter"].update(payload.pop("settings"))
    assert shared_master(c.serials, c.data) == "MASTER"


@pytest.mark.asyncio
async def test_sensor_platform_registers_system_entities_on_separate_device():
    from types import SimpleNamespace
    from custom_components.sunsynk.const import DOMAIN
    from custom_components.sunsynk.sensor import (
        async_setup_entry,
        COMBINED_SUM_KEYS,
        COMBINED_MASTER_KEYS,
    )

    c = context()
    entry = SimpleNamespace(entry_id="entry", async_on_unload=MagicMock())
    hass = SimpleNamespace(data={DOMAIN: {"entry": c}})
    add = MagicMock()
    await async_setup_entry(hass, entry, add)
    entities = [entity for call in add.call_args_list for entity in call.args[0]]
    combined = [
        entity for entity in entities if isinstance(entity, CombinedSystemSensor)
    ]
    assert {
        entity.entity_description.key for entity in combined
    } == COMBINED_SUM_KEYS | COMBINED_MASTER_KEYS
    assert len({entity.unique_id for entity in combined}) == len(combined)
    assert all(
        entity.device_info["name"] == "Combined Solar System" for entity in combined
    )
    assert (
        next(
            entity
            for entity in combined
            if entity.entity_description.key == "battery_power"
        ).native_value
        == 625
    )
