"""Tests for the central inverter-write validation boundary."""

from __future__ import annotations

import math

import pytest

from custom_components.sunsynk.write_validation import (
    MAX_CURRENT_A,
    MAX_PLANT_PRICE,
    MAX_POWER_W,
    SunsynkSettingValidationError,
    validate_plant_price,
    validate_setting_batch,
    validate_setting_value,
)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("chargeCurrent", 0, 0),
        ("dischargeCurrent", str(MAX_CURRENT_A), MAX_CURRENT_A),
        ("cap1", 100.0, 100),
        ("sellTime6Pac", MAX_POWER_W, MAX_POWER_W),
        ("sysWorkMode", 4, 4),
        ("time1on", "true", 1),
        ("sellTime1on", False, 0),
        ("sellTime1", "23:59", "23:59"),
    ],
)
def test_valid_values_are_normalized(key, value, expected):
    assert validate_setting_value(key, value) == expected


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("chargeCurrent", -1),
        ("chargeCurrent", MAX_CURRENT_A + 1),
        ("chargeCurrent", 1.5),
        ("chargeCurrent", math.nan),
        ("chargeCurrent", math.inf),
        ("cap1", 101),
        ("sellTime1Pac", MAX_POWER_W + 1),
        ("sysWorkMode", 5),
        ("time1on", 2),
        ("sellTime1", "24:00"),
        ("sellTime1", "8:00"),
        ("notARealSetting", 1),
    ],
)
def test_invalid_values_are_rejected(key, value):
    with pytest.raises(SunsynkSettingValidationError):
        validate_setting_value(key, value)


@pytest.mark.parametrize("value", [True, object()])
def test_integer_validation_rejects_boolean_and_non_numeric(value):
    with pytest.raises(SunsynkSettingValidationError):
        validate_setting_value("chargeCurrent", value)


@pytest.mark.parametrize(("value", "expected"), [("no", 0), ("off", 0)])
def test_boolean_string_false_variants(value, expected):
    assert validate_setting_value("time1on", value) == expected


def test_batch_validation_does_not_mutate_input_on_failure():
    original = {"cap1": 80, "sellTime1Pac": MAX_POWER_W + 1}

    with pytest.raises(SunsynkSettingValidationError):
        validate_setting_batch(original)

    assert original == {"cap1": 80, "sellTime1Pac": MAX_POWER_W + 1}


@pytest.mark.parametrize("value", [0, "0.25", MAX_PLANT_PRICE])
def test_valid_plant_prices(value):
    assert validate_plant_price(value) == float(value)


@pytest.mark.parametrize(
    "value", [-0.01, MAX_PLANT_PRICE + 0.01, math.nan, math.inf, True, "bad"]
)
def test_invalid_plant_prices(value):
    with pytest.raises(SunsynkSettingValidationError):
        validate_plant_price(value)


# Installation limits extend the existing validators; generic bounds remain syntax guards.
from dataclasses import asdict
import json

from custom_components.sunsynk.write_validation import (
    parse_write_profiles,
    validate_installation_settings,
    validate_setting_payload,
)
from tests.conftest import write_profile, safe_settings


def _profile_json(**updates):
    profile = asdict(
        write_profile(
            max_charge_current_a=80,
            max_discharge_current_a=90,
            max_power_w=8000,
            max_export_power_w=3000,
            min_soc_percent=20,
        )
    )
    profile["members"] = ["TEST123"]
    profile.update(updates)
    return {"TEST123": profile}


def test_profiles_parse_json_and_confirm_scope():
    profiles = parse_write_profiles(
        json.dumps(_profile_json(current_scope="group", power_scope="group")),
        ["TEST123"],
    )
    assert profiles["TEST123"].max_charge_current_a == 80
    assert profiles["TEST123"].current_scope == "group"
    assert parse_write_profiles("", ["TEST123"]) == {}


@pytest.mark.parametrize(
    "value",
    [
        "bad-json",
        [],
        {"TEST123": {}},
        _profile_json(unknown=True),
        _profile_json(members=[]),
        _profile_json(members=["OTHER"]),
        _profile_json(members=["TEST123", "TEST123"]),
        _profile_json(current_scope="unknown"),
        _profile_json(max_charge_current_a=0),
        _profile_json(max_export_power_w=9000),
        _profile_json(battery_voltage_min_v=40),
        _profile_json(battery_voltage_min_v=True, battery_voltage_max_v=60),
        _profile_json(battery_voltage_min_v=60, battery_voltage_max_v=40),
        _profile_json(battery_voltage_min_v=40, battery_voltage_max_v=float("inf")),
    ],
)
def test_invalid_profiles_fail_closed(value):
    with pytest.raises(SunsynkSettingValidationError):
        parse_write_profiles(value, ["TEST123"])


def test_overlapping_groups_are_rejected():
    profiles = _profile_json()
    profiles["OTHER"] = {**profiles["TEST123"], "members": ["OTHER", "TEST123"]}
    with pytest.raises(SunsynkSettingValidationError, match="overlapping"):
        parse_write_profiles(profiles, ["TEST123", "OTHER"])


@pytest.mark.parametrize(
    "updates",
    [
        {"chargeCurrent": 81},
        {"dischargeCurrent": 91},
        {"sdBatteryCurrent": 81},
        {"batteryMaxCurrentCharge": 301},
        {"batteryMaxCurrentCharge": 40},
        {"sellTime1Pac": 8001},
        {"solarMaxSellPower": 3001},
        {"pvMaxLimit": 8001},
        {"zeroExportPower": 3001},
        {"cap1": 19, "time1on": 1},
        {"batteryShutdownCap": 31},
        {"batteryRestartCap": 10},
        {"batteryLowCap": 5},
        {"solarSell": 1},
        {"sellTime1on": 1, "sellTime1Pac": 3001},
        {"sellTime1": "08:00"},
        {"time1on": 1, "sellTime1": "04:00"},
    ],
)
def test_installation_limits_and_cross_field_constraints(updates):
    profile = parse_write_profiles(_profile_json(), ["TEST123"])["TEST123"]
    with pytest.raises(SunsynkSettingValidationError):
        validate_installation_settings(updates, safe_settings(), profile, 8000)


def test_live_battery_maximum_is_an_additional_limit():
    profile = parse_write_profiles(_profile_json(), ["TEST123"])["TEST123"]
    with pytest.raises(SunsynkSettingValidationError, match="configured battery"):
        validate_installation_settings(
            {"chargeCurrent": 60},
            safe_settings(batteryMaxCurrentCharge=50),
            profile,
            8000,
        )
    assert validate_installation_settings(
        {"chargeCurrent": 50}, safe_settings(), profile, 8000
    ) == {"chargeCurrent": 50}


def test_circular_timer_order_and_disabled_zero_duration_slots():
    profile = write_profile()
    settings = safe_settings(
        sellTime1="22:00",
        sellTime2="02:00",
        sellTime3="06:00",
        sellTime4="10:00",
        sellTime5="14:00",
        sellTime6="18:00",
    )
    assert validate_installation_settings({"time1on": 1}, settings, profile, 8000)
    assert validate_installation_settings(
        {"sellTime2": "06:00"}, settings, profile, 8000
    )
    with pytest.raises(SunsynkSettingValidationError):
        validate_installation_settings(
            {"time2on": 1, "sellTime2": "06:00"}, settings, profile, 8000
        )
    with pytest.raises(SunsynkSettingValidationError):
        validate_installation_settings({"sellTime1": "22:00"}, {}, profile, 8000)


def test_voltage_companions_need_approved_finite_bounds():
    profile = parse_write_profiles(
        _profile_json(battery_voltage_min_v=40, battery_voltage_max_v=60), ["TEST123"]
    )["TEST123"]
    assert validate_setting_payload(
        {"sellTime1Volt": "52"}, safe_settings(), profile, 8000
    ) == {"sellTime1Volt": 52.0}
    for value in (999, float("nan"), True, "bad"):
        with pytest.raises(SunsynkSettingValidationError):
            validate_setting_payload(
                {"sellTime1Volt": value}, safe_settings(), profile, 8000
            )
    with pytest.raises(SunsynkSettingValidationError):
        validate_setting_payload(
            {"sellTime1Volt": 52}, safe_settings(), write_profile(), 8000
        )
    with pytest.raises(SunsynkSettingValidationError):
        validate_setting_payload(
            {"absorptionVolt": 999}, safe_settings(), profile, 8000
        )


def test_mode_changes_validate_existing_limits_before_activation():
    profile = write_profile(max_power_w=8000, max_export_power_w=3000)
    with pytest.raises(SunsynkSettingValidationError, match="power limit"):
        validate_installation_settings(
            {"sysWorkMode": 1}, safe_settings(pvMaxLimit=9000), profile, 8000
        )
    assert validate_installation_settings(
        {"sysWorkMode": 1},
        safe_settings(pvMaxLimit=8000, solarMaxSellPower=3000),
        profile,
        8000,
    )
