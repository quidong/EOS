"""Multi-zone heating in the GENETIC plan (eos-plus fork, phase 3).

Two zones like the reference house: ``og`` (wet screed, set back at night and on
working-day mornings) and ``eg`` (dry screed, ground floor plus basement).
"""

from __future__ import annotations

import random

import pendulum
import pytest_asyncio
import pytest
from pydantic import ValidationError

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import HeatingMode
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    HeatPumpGeneLayout,
    build_run_plan,
    plan_result,
)

TZ = "Europe/Berlin"
MONDAY = pendulum.datetime(2026, 11, 2, 0, 0, tz=TZ)

ZONES = {
    "enabled": True,
    "zones": [
        {
            "zone_id": "og",
            "share": 0.4,
            "building": {
                "slab_capacity_wh_per_k": 2500,
                "room_capacity_wh_per_k": 4000,
                "loss_w_per_k": 80,
                "slab_room_w_per_k": 600,
                "emitter_w_per_k": 400,
            },
            "setback_windows": {
                "windows": [{"start_time": "22:00", "duration": "2 hours", "value": None}]
            },
            "setback_room_c": 19.0,
        },
        {
            "zone_id": "eg",
            "share": 0.6,
            "building": {
                "slab_capacity_wh_per_k": 700,
                "room_capacity_wh_per_k": 6000,
                "loss_w_per_k": 100,
                "slab_room_w_per_k": 900,
                "emitter_w_per_k": 600,
            },
        },
    ],
    "couplings": [{"zone_a": "og", "zone_b": "eg", "w_per_k": 150}],
}


def device(zones=ZONES, **settings) -> HeatPumpCommonSettings:
    settings.setdefault("optimization", {})
    settings["optimization"].setdefault("heating_modes", [0, 1, 2, 3, 4])
    return HeatPumpCommonSettings.model_validate({"device_id": "hp1", "zones": zones, **settings})


def zone_plan(start=MONDAY.add(hours=12), hours=24, settings=None, **kwargs):
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=settings or device(),
        initial_room_c=kwargs.pop("room", 20.6),
        initial_dhw_c=kwargs.pop("dhw", 45.0),
        **kwargs,
    )
    starts = [start.add(hours=i) for i in range(hours)]
    return build_run_plan(params, starts, 1.0, [2.0] * hours, [0.0] * hours, [0.0003] * hours)


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def test_zone_settings_validation():
    with pytest.raises(ValidationError):  # one zone is no multi-zone setup
        device(zones={"enabled": True, "zones": [{"zone_id": "og"}]})
    with pytest.raises(ValidationError):
        device(zones={"enabled": True, "zones": [{"zone_id": "og"}, {"zone_id": "og"}]})
    with pytest.raises(ValidationError):
        device(
            zones={
                "enabled": True,
                "zones": [{"zone_id": "og"}, {"zone_id": "eg"}],
                "couplings": [{"zone_a": "og", "zone_b": "ug"}],
            }
        )
    with pytest.raises(ValidationError):  # the id becomes part of measurement keys
        device(zones={"enabled": True, "zones": [{"zone_id": "OG 1"}, {"zone_id": "eg"}]})


def test_zone_measurement_keys():
    keys = device(room_limits={"enabled": True}).measurement_keys
    for zone in ("og", "eg"):
        assert f"hp1-{zone}-room-temperature" in keys
        assert f"hp1-{zone}-room-min-margin" in keys
        assert f"hp1-{zone}-room-max-margin" in keys
    assert "hp1-og-room-temperature" not in device(zones={"enabled": False}).measurement_keys


def test_a_zone_without_own_model_is_the_house_times_its_share():
    settings = device(
        zones={
            "enabled": True,
            "zones": [{"zone_id": "og", "share": 0.4}, {"zone_id": "eg", "share": 0.6}],
        }
    )
    og = settings.zones.zones[0].to_parameters(settings.building)
    house = settings.building.to_parameters()
    assert og.loss_w_per_k == pytest.approx(house.loss_w_per_k * 0.4)
    assert og.slab_capacity_wh_per_k == pytest.approx(house.slab_capacity_wh_per_k * 0.4)


# ----------------------------------------------------------------------
# Gene layout
# ----------------------------------------------------------------------


def test_gene_layout_has_one_heating_block_per_zone():
    plan = zone_plan()
    assert plan.n_zones == 2 and plan.zone_ids == ("og", "eg")
    assert plan.n_genes == 3 * plan.n_periods
    assert len(plan.upper_bounds()) == plan.n_genes
    genes = plan.default_genes()
    assert len(genes) == plan.n_genes and plan.valid(genes)
    zones = plan.zone_modes(genes)
    assert all(m == HeatingMode.NORMAL for z in zones for m in z)
    # a single-zone gene vector (e.g. from the MILP projection) applies to every zone
    single = [plan.heating_index(HeatingMode.FORCED)] * plan.n_periods + [0] * plan.n_periods
    expanded = plan.expand_zone_genes(single)
    assert len(expanded) == plan.n_genes
    assert all(m == HeatingMode.FORCED for z in plan.zone_modes(expanded) for m in z)


def test_heat_pump_mode_follows_the_zone_modes():
    plan = zone_plan()
    n = plan.n_periods
    og = [plan.heating_index(HeatingMode.FORCED)] * n
    eg = [plan.heating_index(HeatingMode.REDUCED)] * n
    heating, _ = plan.modes(og + eg + [0] * n)
    assert all(m == HeatingMode.FORCED for m in heating)
    both_reduced = eg + eg + [0] * n
    assert all(m == HeatingMode.REDUCED for m in plan.modes(both_reduced)[0])


def test_mutation_and_shift_keep_every_block_valid():
    plan = zone_plan()
    rng = random.Random(4)  # noqa: S311 - reproducible test
    genes = plan.default_genes()
    touched = set()
    for _ in range(200):
        before = list(genes)
        assert plan.mutate(genes, rng)
        assert plan.valid(genes)
        touched |= {i // plan.n_periods for i, (a, b) in enumerate(zip(before, genes)) if a != b}
    assert touched == {0, 1, 2}  # both zones and hot water are searched
    layout = HeatPumpGeneLayout([plan])
    n = plan.n_periods
    marked = [1] * n + [2] * n + [0] * n
    marked[0], marked[n], marked[2 * n] = 3, 4, 1
    shifted = layout.shift(marked, 1)
    assert shifted[0] == 1 and shifted[n] == 2 and shifted[2 * n] == 0


# ----------------------------------------------------------------------
# Simulation in the plan
# ----------------------------------------------------------------------


def test_forcing_one_zone_while_the_other_regulates():
    plan = zone_plan(zone_room_c={"og": 19.3, "eg": 21.6})
    n = plan.n_periods
    og = [plan.heating_index(HeatingMode.FORCED)] * 4 + [plan.heating_index(1)] * (n - 4)
    eg = [plan.heating_index(HeatingMode.NORMAL)] * n
    result = plan_result(plan, og + eg + [0] * n)
    assert result["heating_mode"][:4] == ["FORCED"] * 4
    assert result["zone_og_heating_mode"][:4] == ["FORCED"] * 4
    assert result["zone_eg_heating_mode"][:4] == ["NORMAL"] * 4
    og_heat = sum(result["zone_og_heat_wh"][:4])
    eg_heat = sum(result["zone_eg_heat_wh"][:4])
    assert og_heat > eg_heat
    assert result["zone_og_room_c"][3] > 19.3
    assert result["zone_eg_room_c"][3] < 21.8


def test_measured_zone_rooms_set_the_initial_state():
    plan = zone_plan(zone_room_c={"og": 19.4})  # eg missing -> house mean
    assert plan.zone_initial.rooms == pytest.approx([19.4, 20.6])


def test_setback_window_lowers_only_its_zone():
    plan = zone_plan(start=MONDAY.add(hours=20), hours=6)
    slot_22 = 2  # 22:00
    og, eg = plan.zone_inputs[slot_22]
    assert og.setpoint_c == pytest.approx(19.0)
    assert og.comfort_min_c == pytest.approx(18.5)
    assert eg.comfort_min_c == pytest.approx(plan.inputs[slot_22].comfort_min_c)
    assert plan.zone_inputs[0][0].setpoint_c == pytest.approx(21.0)  # 20:00, no setback


def test_setback_with_the_measured_temperature():
    plan = zone_plan(start=MONDAY.add(hours=22), hours=2, setback_room_c=18.0)
    assert plan.zone_inputs[0][0].setpoint_c == pytest.approx(18.0)


def test_zone_margins_raise_only_their_zone():
    settings = device(room_limits={"enabled": True, "horizon_hours": 3})
    plan = zone_plan(
        settings=settings,
        zone_room_c={"og": 20.3, "eg": 21.0},
        zone_min_margin_k={"og": -0.4, "eg": 1.0},
    )
    og, eg = plan.zone_inputs[0]
    assert og.comfort_min_c == pytest.approx(20.7)  # 20.3 + 0.4, above the band 20.5
    assert eg.comfort_min_c == pytest.approx(plan.inputs[0].comfort_min_c)
    assert plan.zone_inputs[3][0].comfort_min_c == pytest.approx(plan.inputs[3].comfort_min_c)
    # without zones the house margins still apply as before
    plain = zone_plan(
        settings=device(zones={"enabled": False}, room_limits={"enabled": True}),
        room_min_margin_k=-0.4,
    )
    assert plain.inputs[0].comfort_min_c > plan.inputs[0].comfort_min_c


def test_zone_changes_count_as_mode_changes():
    settings = device(optimization={"mode_change_penalty_amt": 0.05})
    plan = zone_plan(settings=settings)
    n = plan.n_periods
    normal = plan.heating_index(HeatingMode.NORMAL)
    reduced = plan.heating_index(HeatingMode.REDUCED)
    steady = plan.evaluate([normal] * n + [normal] * n + [0] * n)
    one_zone = plan.evaluate([reduced] + [normal] * (n - 1) + [normal] * n + [0] * n)
    assert steady.mode_changes == 0
    assert one_zone.mode_changes == 1


def test_comfort_is_judged_per_zone():
    """A cold zone costs comfort even when the house mean is fine."""
    plan = zone_plan(zone_room_c={"og": 19.0, "eg": 22.0}, room=20.8)
    n = plan.n_periods
    normal = plan.heating_index(HeatingMode.NORMAL)
    outcome = plan.evaluate([normal] * (2 * n) + [0] * n)
    assert outcome.comfort_kh > 0


@pytest.mark.asyncio
async def test_optimization_plans_and_reports_every_zone(config_eos):
    from akkudoktoreos.core.coreabc import get_ems
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
    from akkudoktoreos.utils.datetimeutil import to_datetime

    hours = 24
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {
                    "horizon_hours": hours,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                }
            },
        }
    )
    start = to_datetime("2026-11-02T00:00:00", in_timezone=TZ)
    get_ems(init=True).set_start_datetime(start)
    params = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * hours,
                "gesamtlast": [300.0] * hours,
                "strompreis_euro_pro_wh": [0.0003] * hours,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * hours,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": [2.0] * hours,
            "pv_battery": None,
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "hp1",
                    "settings": device().model_dump(),
                    "initial_room_c": 20.6,
                    "initial_dhw_c": 45.0,
                    "zone_room_c": {"og": 19.5, "eg": 21.4},
                }
            ],
        }
    )
    solution = GeneticOptimization(fixed_seed=3).optimize_ems(params, ngen=3, individuals=10)
    schedule = solution.heat_pumps["hp1"]
    for zone in ("og", "eg"):
        assert len(schedule[f"zone_{zone}_heating_mode"]) >= hours
        assert len(schedule[f"zone_{zone}_room_c"]) >= hours
    frame = await solution.optimization_solution()
    columns = set(frame.solution.columns) if hasattr(frame.solution, "columns") else set()
    if not columns:
        columns = set(frame.solution.to_dataframe().columns)
    assert {"hp1_zone_og_heating_mode", "hp1_zone_eg_room_temperature_c"} <= columns
    # the heat pump mode is the aggregate of the zone modes in every slot
    agg = schedule["heating_mode"]
    og, eg = schedule["zone_og_heating_mode"], schedule["zone_eg_heating_mode"]
    for a, o, e in zip(agg, og, eg):
        if "FORCED" in (o, e):
            assert a == "FORCED"
        if o == e == "REDUCED":
            assert a == "REDUCED"


@pytest_asyncio.fixture
async def isolated_measurement(config_eos):
    from akkudoktoreos.core.coreabc import get_measurement

    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_request_reads_the_zone_measurements(config_eos, isolated_measurement):
    from akkudoktoreos.core.coreabc import get_ems
    from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest

    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 8,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                },
            },
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "max_home_appliances": 0,
                "home_appliances": {},
                "max_heat_pumps": 1,
                "heat_pumps": {"hp1": {"zones": ZONES, "room_limits": {"enabled": True}}},
            },
        }
    )
    now = pendulum.datetime(2026, 11, 20, 12, 31, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    m = isolated_measurement
    await m.update_value(now, "hp1-room-temperature", 20.5)
    await m.update_value(now, "hp1-dhw-temperature", 45.0)
    await m.update_value(now, "hp1-og-room-temperature", 19.4)
    await m.update_value(now, "hp1-og-room-min-margin", -0.1)
    await m.update_value(now.subtract(hours=2), "hp1-eg-room-temperature", 21.5)  # stale
    request = ConfigOptimizationRequest.model_validate(
        {
            "forecasts": {
                "pv_forecast_wh": [0.0] * 48,
                "total_load": [300.0] * 48,
                "electricity_price_per_wh": [0.0003] * 48,
                "feed_in_tariff_per_wh": [0.00007] * 48,
                "temperature_forecast": [4.0] * 48,
                "ghi_forecast": [0.0] * 48,
            }
        }
    )
    state = (await request.resolve()).heat_pumps[0]
    assert state.zone_room_c == {"og": 19.4}  # the stale eg value is left out
    assert state.zone_min_margin_k == {"og": -0.1}
