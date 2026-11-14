"""Screed temperature carried from plan to plan, corrected by the return (RL29 fork)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import pendulum

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.slabestimate import (
    estimate_slabs,
    planned_value_at,
    slab_from_return_c,
)
from akkudoktoreos.optimization.genetic import configrequest as configrequest_module
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
)

START = pendulum.datetime(2026, 11, 14, 12, 0, tz="Europe/Berlin")
ZONES = {
    "enabled": True,
    "zones": [
        {"zone_id": "og", "share": 0.33},
        {"zone_id": "eg", "share": 0.67},
    ],
}


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def test_planned_value_interpolates_within_the_slot():
    rows = [24.0, 25.0, 26.0]  # states at the end of slots 0, 1, 2
    start = START.timestamp()
    assert planned_value_at(rows, start, 900, start + 300) == pytest.approx(24.0)
    assert planned_value_at(rows, start, 900, start + 900) == pytest.approx(24.0)
    assert planned_value_at(rows, start, 900, start + 1350) == pytest.approx(24.5)
    assert planned_value_at(rows, start, 900, start + 2700) == pytest.approx(26.0)


def test_planned_value_outside_the_plan_is_none():
    start = START.timestamp()
    assert planned_value_at([24.0], start, 900, start - 1) is None
    assert planned_value_at([24.0], start, 900, start + 901) is None
    assert planned_value_at([], start, 900, start) is None


def test_return_gives_the_screed_while_heating():
    # mean water (30 + 26) / 2 = 28, 2 kW into 1000 W/K -> screed 26
    assert slab_from_return_c(26.0, 2000.0, 1000.0, 3.0, flow_c=30.0) == pytest.approx(26.0)
    # without flow: return + spread / 2
    assert slab_from_return_c(26.0, 2000.0, 1000.0, 3.0) == pytest.approx(25.5)


def test_return_says_nothing_without_floor_heating():
    assert slab_from_return_c(26.0, 500.0, 1000.0, 3.0, min_heat_w=1000.0) is None
    assert slab_from_return_c(None, 3000.0, 1000.0, 3.0) is None
    assert slab_from_return_c(26.0, None, 1000.0, 3.0) is None


def test_estimate_corrects_house_and_zones_alike():
    house, zones = estimate_slabs(25.0, {"og": 24.0, "eg": 26.0}, 27.0, 0.5)
    assert house == pytest.approx(26.0)
    assert zones == pytest.approx({"og": 25.0, "eg": 27.0})


def test_estimate_without_carried_value_takes_the_return():
    assert estimate_slabs(None, None, 27.0, 0.5) == (pytest.approx(27.0), None)
    assert estimate_slabs(None, None, None, 0.5) == (None, None)


def test_estimate_stays_above_the_room_and_below_the_flow_limit():
    house, _ = estimate_slabs(18.0, None, None, 0.5, room_c=21.0, max_c=45.0)
    assert house == pytest.approx(20.5)
    house, _ = estimate_slabs(50.0, None, None, 0.5, room_c=21.0, max_c=45.0)
    assert house == pytest.approx(45.0)


# ----------------------------------------------------------------------
# Settings and run plan
# ----------------------------------------------------------------------


def device(**settings) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings.model_validate({"device_id": "hp1", **settings})


def test_enabled_estimate_asks_for_the_return_and_heat_measurements():
    keys = device(slab_estimate={"enabled": True}, calibration={"enabled": False}).measurement_keys
    for key in ("hp1-return-temperature", "hp1-flow-temperature", "hp1-heat-power"):
        assert key in keys
    assert "hp1-return-temperature" not in device().measurement_keys


def test_zone_screeds_reach_the_run_plan():
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=device(zones=ZONES),
        initial_room_c=21.0,
        initial_zone_slab_c={"og": 27.5, "eg": 25.0},
        initial_dhw_c=45.0,
    )
    starts = [START.add(hours=i) for i in range(4)]
    plan = build_run_plan(params, starts, 1.0, [5.0] * 4, [0.0] * 4, [0.0003] * 4)
    assert plan.zone_initial is not None
    assert plan.zone_initial.slabs == pytest.approx([27.5, 25.0])


# ----------------------------------------------------------------------
# Config request: carried from the previous plan, corrected by the return
# ----------------------------------------------------------------------


def previous_plan(monkeypatch: pytest.MonkeyPatch, start=START) -> None:
    schedule = {
        "start_datetime": start.isoformat(),
        "slot_seconds": 900,
        "slab_c": [26.0, 26.4, 26.8, 27.2],
        "zone_og_slab_c": [25.0, 25.2, 25.4, 25.6],
        "zone_eg_slab_c": [27.0, 27.4, 27.8, 28.2],
    }
    solution = SimpleNamespace(heat_pumps={"hp1": schedule})
    monkeypatch.setattr(
        configrequest_module,
        "get_ems",
        lambda: SimpleNamespace(genetic_solution=lambda: solution),
    )


def measurements(monkeypatch: pytest.MonkeyPatch, values: dict) -> None:
    async def latest(self, key, observation_time, max_age):
        return values.get(key)

    monkeypatch.setattr(ConfigOptimizationRequest, "_latest_measurement", latest)


def request() -> ConfigOptimizationRequest:
    return ConfigOptimizationRequest.model_construct()


@pytest.mark.asyncio
async def test_screed_is_carried_from_the_previous_plan(monkeypatch: pytest.MonkeyPatch):
    previous_plan(monkeypatch)
    measurements(monkeypatch, {})
    settings = device(zones=ZONES, slab_estimate={"enabled": True})
    slab, zones = await request()._slab_estimate(settings, START.add(minutes=30), 21.0)
    assert slab == pytest.approx(26.4)
    assert zones == pytest.approx({"og": 25.2, "eg": 27.4})


@pytest.mark.asyncio
async def test_return_corrects_the_carried_screed(monkeypatch: pytest.MonkeyPatch):
    previous_plan(monkeypatch)
    measurements(
        monkeypatch,
        {
            "hp1-return-temperature": 28.0,
            "hp1-flow-temperature": 32.0,
            "hp1-heat-power": 2000.0,
            "hp1-dhw-heat-power": 0.0,
        },
    )
    settings = device(
        zones=ZONES,
        building={"emitter_w_per_k": 1000.0},
        slab_estimate={"enabled": True, "return_gain": 0.5},
    )
    slab, zones = await request()._slab_estimate(settings, START.add(minutes=30), 21.0)
    # return-based: (32 + 28) / 2 - 2000 / 1000 = 28; carried 26.4 -> +0.8
    assert slab == pytest.approx(27.2)
    assert zones == pytest.approx({"og": 26.0, "eg": 28.2})


@pytest.mark.asyncio
async def test_hot_water_charge_does_not_correct(monkeypatch: pytest.MonkeyPatch):
    previous_plan(monkeypatch)
    measurements(
        monkeypatch,
        {"hp1-return-temperature": 40.0, "hp1-heat-power": 2800.0, "hp1-dhw-heat-power": 2800.0},
    )
    settings = device(slab_estimate={"enabled": True})
    slab, _ = await request()._slab_estimate(settings, START.add(minutes=30), 21.0)
    assert slab == pytest.approx(26.4)


@pytest.mark.asyncio
async def test_old_plan_is_not_carried(monkeypatch: pytest.MonkeyPatch):
    previous_plan(monkeypatch, start=START.subtract(hours=5))
    measurements(monkeypatch, {})
    settings = device(slab_estimate={"enabled": True, "max_age_hours": 3})
    assert await request()._slab_estimate(settings, START, 21.0) == (None, None)


@pytest.mark.asyncio
async def test_hot_water_flow_without_its_heat_measurement_does_not_correct(
    monkeypatch: pytest.MonkeyPatch,
):
    previous_plan(monkeypatch)
    # no <id>-dhw-heat-power: the flow above unit.max_flow_c gives the charge away
    measurements(
        monkeypatch,
        {"hp1-return-temperature": 44.0, "hp1-flow-temperature": 52.0, "hp1-heat-power": 2800.0},
    )
    settings = device(unit={"max_flow_c": 45.0}, slab_estimate={"enabled": True})
    slab, _ = await request()._slab_estimate(settings, START.add(minutes=30), 21.0)
    assert slab == pytest.approx(26.4)


@pytest.mark.asyncio
async def test_implausible_return_value_is_not_used(monkeypatch: pytest.MonkeyPatch):
    previous_plan(monkeypatch)
    measurements(
        monkeypatch,
        {"hp1-return-temperature": 40.0, "hp1-flow-temperature": 44.0, "hp1-heat-power": 1500.0},
    )
    settings = device(unit={"max_flow_c": 55.0}, slab_estimate={"enabled": True})
    slab, _ = await request()._slab_estimate(settings, START.add(minutes=30), 21.0)
    assert slab == pytest.approx(26.4)  # 40.5 degC is 14 K away from the carried 26.4
