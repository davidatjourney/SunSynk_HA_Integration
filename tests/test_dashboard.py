"""Tests for build_dashboard (custom_components/sunsynk/dashboard.py).

Focuses on the actual logic in this otherwise-declarative module: entity
ID resolution/fallback, None-filtering, and the has_forecast/has_tariff/
has_vslots gating that adds or removes whole optional sections. Exact
card contents are exercised only where they encode a real conditional —
not asserted wholesale, since that would just be re-typing the file.
"""

from __future__ import annotations

from custom_components.sunsynk.dashboard import build_dashboard


def _views_by_title(config: dict) -> dict[str, dict]:
    return {v["title"]: v for v in config["views"]}


def _cards_by_title(view: dict) -> dict[str, dict]:
    return {c.get("title"): c for c in view["cards"] if isinstance(c, dict)}


def _overview_stack_cards(config: dict) -> list[dict]:
    """The Overview view wraps everything in one top-level vertical-stack —
    unwrap it to get at the individual cards (power flow, quick stats,
    forecast, tariff summary, today's energy)."""
    overview = _views_by_title(config)["Overview"]
    return overview["cards"][0]["cards"]


class TestEntityResolution:
    def test_required_entity_uses_registry_lookup_when_available(self):
        config = build_dashboard("myprefix", eid=lambda key: f"sensor.resolved_{key}")
        views = _views_by_title(config)
        diag = _cards_by_title(views["Diagnostics"])
        grid_card = diag["Grid Details"]
        assert grid_card["entities"][0] == "sensor.resolved_grid_pac"

    def test_required_entity_falls_back_to_prefix_when_no_eid_fn(self):
        config = build_dashboard("myprefix")
        views = _views_by_title(config)
        diag = _cards_by_title(views["Diagnostics"])
        grid_card = diag["Grid Details"]
        assert grid_card["entities"][0] == "sensor.myprefix_grid_power"

    def test_required_entity_falls_back_when_eid_fn_returns_none(self):
        config = build_dashboard("myprefix", eid=lambda key: None)
        views = _views_by_title(config)
        diag = _cards_by_title(views["Diagnostics"])
        grid_card = diag["Grid Details"]
        assert grid_card["entities"][0] == "sensor.myprefix_grid_power"

    def test_switch_number_text_domains_are_correct(self):
        config = build_dashboard("myprefix")
        views = _views_by_title(config)
        settings = _cards_by_title(views["Settings"])
        system_mode_entities = settings["System Mode"]["entities"]
        # sw()/nm() build "<domain>.<prefix>_<key>" when unresolved
        assert any(
            str(e).startswith("switch.myprefix_")
            for e in system_mode_entities
            if isinstance(e, str)
        )
        assert any(
            str(e).startswith("number.myprefix_")
            for e in system_mode_entities
            if isinstance(e, str)
        )


class TestPowerFlowCardEntities:
    def test_pv1_power_falls_back_to_combined_solar_power_without_mppt_data(self):
        config = build_dashboard(
            "myprefix"
        )  # no eid -> e_opt returns fallback strings too
        flow_card = config["views"][0]["cards"][0]["cards"][0]
        assert flow_card["type"] == "custom:sunsynk-power-flow-card"
        # With no `eid`, e_opt() falls back to a real (non-None) string, so
        # pv1_power is never None here — the `or solar_power` branch only
        # fires once entity-registry lookups are wired in and MPPT truly
        # doesn't exist. Covered separately in test_none_values below.
        assert (
            flow_card["entities"]["pv1_power_186"] == "sensor.myprefix_pv_mppt_1_power"
        )

    def test_pv1_power_falls_back_to_solar_power_when_mppt_entity_missing(self):
        def eid(key: str) -> str | None:
            if key in ("pv_mppt0_ppv", "pv_mppt1_ppv"):
                return None
            return f"sensor.resolved_{key}"

        config = build_dashboard("myprefix", eid=eid)
        flow_card = config["views"][0]["cards"][0]["cards"][0]
        assert flow_card["entities"]["pv1_power_186"] == "sensor.resolved_pv_pac"
        assert "pv2_power_187" not in flow_card["entities"]

    def test_none_valued_entities_are_dropped_from_power_flow_card(self):
        def eid(key: str) -> str | None:
            if key == "pv_mppt1_ppv":
                return None
            return f"sensor.resolved_{key}"

        config = build_dashboard("myprefix", eid=eid)
        flow_card = config["views"][0]["cards"][0]["cards"][0]
        assert "pv2_power_187" not in flow_card["entities"]
        assert "pv1_power_186" in flow_card["entities"]

    def test_mppts_count_is_2_when_second_string_present(self):
        config = build_dashboard("myprefix", eid=lambda key: f"sensor.resolved_{key}")
        flow_card = config["views"][0]["cards"][0]["cards"][0]
        assert flow_card["solar"]["mppts"] == 2

    def test_mppts_count_is_1_when_second_string_absent(self):
        def eid(key: str) -> str | None:
            if key == "pv_mppt1_ppv":
                return None
            return f"sensor.resolved_{key}"

        config = build_dashboard("myprefix", eid=eid)
        flow_card = config["views"][0]["cards"][0]["cards"][0]
        assert flow_card["solar"]["mppts"] == 1

    def test_essential_and_nonessential_power_are_literal_none_markers(self):
        config = build_dashboard("myprefix")
        flow_card = config["views"][0]["cards"][0]["cards"][0]
        assert flow_card["entities"]["essential_power"] == "none"
        assert flow_card["entities"]["nonessential_power"] == "none"


class TestForecastGating:
    def test_forecast_card_absent_when_not_configured(self):
        config = build_dashboard("myprefix")  # no forecast_eid
        card_titles = [c.get("title") for c in _overview_stack_cards(config)]
        assert "Weather Conditions (Open-Meteo)" not in card_titles

    def test_forecast_card_present_when_configured(self):
        config = build_dashboard(
            "myprefix", forecast_eid=lambda key: f"sensor.resolved_{key}"
        )
        card_titles = [c.get("title") for c in _overview_stack_cards(config)]
        assert "Weather Conditions (Open-Meteo)" in card_titles


class TestTariffGating:
    def test_tariff_view_shows_placeholder_when_not_configured(self):
        config = build_dashboard("myprefix")
        tariff_view = _views_by_title(config)["Tariff"]
        assert len(tariff_view["cards"]) == 1
        assert tariff_view["cards"][0]["type"] == "markdown"

    def test_tariff_view_shows_real_cards_when_configured(self):
        config = build_dashboard(
            "myprefix", tariff_eid=lambda key: f"sensor.resolved_{key}"
        )
        tariff_view = _views_by_title(config)["Tariff"]
        titles = [c.get("title") for c in tariff_view["cards"]]
        assert "Tariff Manager" in titles
        assert "Cheap-rate Charging" in titles
        assert "Peak-rate Discharging" in titles

    def test_overview_tariff_summary_card_absent_when_not_configured(self):
        config = build_dashboard("myprefix")
        card_titles = [c.get("title") for c in _overview_stack_cards(config)]
        assert "Tariff Manager" not in card_titles

    def test_overview_tariff_summary_card_present_when_configured(self):
        config = build_dashboard(
            "myprefix", tariff_eid=lambda key: f"sensor.resolved_{key}"
        )
        card_titles = [c.get("title") for c in _overview_stack_cards(config)]
        assert "Tariff Manager" in card_titles


class TestVirtualSlotsGating:
    def test_virtual_slots_view_shows_placeholder_when_not_configured(self):
        config = build_dashboard("myprefix")
        vs_view = _views_by_title(config)["Virtual Slots"]
        assert len(vs_view["cards"]) == 1
        assert vs_view["cards"][0]["type"] == "markdown"

    def test_virtual_slots_view_shows_real_cards_when_configured(self):
        config = build_dashboard(
            "myprefix", vslot_eid=lambda key: f"sensor.resolved_{key}"
        )
        vs_view = _views_by_title(config)["Virtual Slots"]
        titles = [c.get("title") for c in vs_view["cards"]]
        assert "Virtual Slot Scheduler" in titles
        assert "Configured Virtual Slots" in titles


class TestStructure:
    def test_top_level_view_titles_and_order(self):
        config = build_dashboard("myprefix")
        titles = [v["title"] for v in config["views"]]
        assert titles == [
            "Overview",
            "Charts",
            "Settings",
            "Tariff",
            "Virtual Slots",
            "Diagnostics",
        ]

    def test_overview_is_a_panel_view(self):
        config = build_dashboard("myprefix")
        assert config["views"][0]["type"] == "panel"

    def test_diagnostics_cards_present(self):
        config = build_dashboard("myprefix")
        diag = _cards_by_title(_views_by_title(config)["Diagnostics"])
        assert set(diag.keys()) >= {
            "Inverter Info",
            "Grid Details",
            "Battery Details",
            "Plant Pricing",
            "All-Time Totals",
        }

    def test_returns_a_fresh_dict_on_each_call(self):
        """Guards against accidental module-level mutable state being shared
        and mutated across config entries (each with a different prefix)."""
        config_a = build_dashboard("prefix_a")
        config_b = build_dashboard("prefix_b")
        assert config_a is not config_b
        assert config_a["views"] is not config_b["views"]
        grid_a = _cards_by_title(_views_by_title(config_a)["Diagnostics"])[
            "Grid Details"
        ]
        grid_b = _cards_by_title(_views_by_title(config_b)["Diagnostics"])[
            "Grid Details"
        ]
        assert grid_a["entities"][0] == "sensor.prefix_a_grid_power"
        assert grid_b["entities"][0] == "sensor.prefix_b_grid_power"


class TestCombinedDashboard:
    def _inverter(self, serial):
        keys = [
            "pv_pac",
            "battery_soc",
            "battery_power",
            "grid_pac",
            "inverter_pac",
            "load_total_power",
            "pv_etoday",
            "pv_etotal",
            "battery_voltage",
            "battery_current",
            "battery_temp",
            "grid_fac",
            "load_daily_used",
            "battery_etoday_charge",
            "battery_etoday_discharge",
            "grid_etoday_from",
            "grid_etoday_to",
        ]
        entities = {key: f"sensor.{serial}_{key}" for key in keys}
        return f"Solar ({serial})", entities, {}

    def test_responsive_overview_and_detail_tabs(self):
        from custom_components.sunsynk.dashboard import _build_combined_dashboard

        inverters = [self._inverter(sn) for sn in ("SN1", "SN2", "SN3")]
        system = self._inverter("combined")[1]
        config = _build_combined_dashboard(inverters, system, 20)
        views = _views_by_title(config)
        assert list(views) == ["Overview", "Solar", "Battery", "Grid", "Charts"]
        overview = views["Overview"]
        assert overview["type"] == "sections"
        assert overview["max_columns"] == 2
        left, right = overview["sections"]
        heading, flow = left["cards"]
        assert all(label in heading["content"] for label, _, _ in inverters)
        assert flow["type"] == "custom:sunsynk-power-flow-card"
        assert flow["card_width"] == "100%"
        assert flow["battery"]["power_only"] is True
        assert flow["battery"]["show_remaining_energy"] is False
        assert (
            "power_only"
            not in build_dashboard("individual")["views"][0]["cards"][0]["cards"][0][
                "battery"
            ]
        )
        assert flow["entities"]["essential_power"] == system["load_total_power"]
        assert flow["entities"]["pv_total"] == system["pv_pac"]
        assert flow["solar"]["mppts"] == 1
        assert all(
            value in system.values() or value == "none"
            for value in flow["entities"].values()
        )
        chart = right["cards"][0]
        assert chart["type"] == "custom:sunsynk-overview-chart"
        assert chart["battery_bank_capacity_kwh"] == 20
        assert chart["entities"] == {
            key: system[key] for key in ("pv_pac", "load_total_power", "battery_soc")
        }
        for title in ("Solar", "Battery", "Grid"):
            rows = views[title]["cards"][0]["entities"]
            assert rows
            assert all(row["entity"] in system.values() for row in rows)
        assert [row["entity"] for row in views["Battery"]["cards"][0]["entities"]] == [
            system[key]
            for key in (
                "battery_soc",
                "battery_power",
                "battery_voltage",
                "battery_current",
                "battery_temp",
                "battery_etoday_charge",
                "battery_etoday_discharge",
            )
        ]
        graphs = views["Charts"]["cards"]
        for graph, key in zip(
            graphs,
            (
                "pv_pac",
                "battery_power",
                "grid_pac",
                "load_total_power",
                "battery_soc",
                "pv_etotal",
            ),
            strict=True,
        ):
            assert graph["entities"][-1] == {
                "entity": system[key],
                "name": "Combined system",
            }
            assert len(graph["entities"]) == 4
        assert graphs[-1]["stat_types"] == ["change"]
        assert graphs[-1]["days_to_show"] == 30

    def test_missing_system_entities_show_explanation_without_guessed_ids(self):
        from custom_components.sunsynk.dashboard import _build_combined_dashboard

        config = _build_combined_dashboard(
            [
                ("Missing (SN1)", {}, {}),
                ("Partial (SN2)", {"battery_soc": "sensor.registered_soc"}, {}),
            ]
        )
        views = _views_by_title(config)
        left, right = views["Overview"]["sections"]
        assert all(card["type"] == "markdown" for card in left["cards"])
        assert "one parallel installation" in left["cards"][1]["content"]
        assert right["cards"][0]["entities"] == {}
        for title in ("Solar", "Battery", "Grid"):
            assert views[title]["cards"][0]["type"] == "markdown"
        assert views["Charts"]["cards"][4]["entities"] == [
            {"entity": "sensor.registered_soc", "name": "Partial (SN2)"}
        ]

    def test_optional_detail_readings_are_filtered(self):
        from custom_components.sunsynk.dashboard import _build_combined_dashboard

        config = _build_combined_dashboard([], {"battery_soc": "sensor.real_soc"})
        views = _views_by_title(config)
        assert views["Battery"]["cards"][0]["entities"] == [
            {"entity": "sensor.real_soc", "name": "SOC"}
        ]
        assert (
            views["Overview"]["sections"][1]["cards"][0]["battery_bank_capacity_kwh"]
            is None
        )

    def test_flow_disables_daily_sections_without_registered_readings(self):
        from custom_components.sunsynk.dashboard import _build_combined_dashboard

        keys = ("pv_pac", "battery_soc", "battery_power", "grid_pac", "inverter_pac")
        config = _build_combined_dashboard(
            [], {key: f"sensor.actual_{key}" for key in keys}
        )
        flow = config["views"][0]["sections"][0]["cards"][1]
        assert flow["solar"]["show_daily"] is False
        assert flow["battery"]["show_daily"] is False
        assert flow["grid"]["show_daily_buy"] is False
        assert flow["grid"]["show_daily_sell"] is False
        assert flow["load"]["show_daily"] is False
