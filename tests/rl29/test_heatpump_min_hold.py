"""Minimum hold time between changes of the executed heating level (RL29 fork).

On 2026-11-05 the Home Assistant automation enforced a 55 minute hold between SG Ready
changes that EOS did not know: EOS planned SG 1 at 08:32 (held until 09:00), then SG 2
at 09:00:30 - the heat pump ran SG 1 for 2.5 minutes - and FORCED at 09:32, which was
never executed. The hold now lives in EOS:

- a change of the executed level (SG Ready state per mode) waits ``min_hold_minutes``
  after the last change; a return to NORMAL is always allowed and starts a new hold;
- at the start of the plan the hold counts from the change the controller reports;
- modes of the same level (RAISED/PREHEAT, which zone is forced) change freely.

Self-contained on purpose (CI imports test modules one by one).
"""

from __future__ import annotations

from types import SimpleNamespace

import pendulum
import pytest
import pytest_asyncio
from pydantic import ValidationError

from akkudoktoreos.devices.settings.heatpumpsettings import (
    DEFAULT_SG_READY_OF_MODE,
    HeatPumpCommonSettings,
)
from akkudoktoreos.devices.thermal.heatpump import HeatingMode
from akkudoktoreos.optimization.genetic import configrequest
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
    plan_result,
)

TZ = "Europe/Berlin"
START = pendulum.datetime(2026, 11, 5, 8, 0, tz=TZ)
R, N, RA, P, F = (
    HeatingMode.REDUCED,
    HeatingMode.NORMAL,
    HeatingMode.RAISED,
    HeatingMode.PREHEAT,
    HeatingMode.FORCED,
)

ZONES = {
    "enabled": True,
    "zones": [
        {
            "zone_id": "og",
            "share": 0.4,
            "building": {
                "slab_capacity_wh_per_k": 2200,
                "room_capacity_wh_per_k": 3300,
                "loss_w_per_k": 80,
                "slab_room_w_per_k": 500,
                "emitter_w_per_k": 330,
            },
        },
        {
            "zone_id": "eg",
            "share": 0.6,
            "building": {
                "slab_capacity_wh_per_k": 1300,
                "room_capacity_wh_per_k": 6700,
                "loss_w_per_k": 60,
                "slab_room_w_per_k": 1000,
                "emitter_w_per_k": 670,
            },
        },
    ],
    "couplings": [{"zone_a": "og", "zone_b": "eg", "w_per_k": 100}],
}


def device(hold: float = 55.0, zones=None, **settings) -> HeatPumpCommonSettings:
    opt = settings.pop("optimization", {})
    opt.setdefault("heating_modes", [0, 1, 2, 3, 4])
    opt.setdefault("min_hold_minutes", hold)
    data = {"device_id": "hp1", "optimization": opt, **settings}
    if zones is not None:
        data["zones"] = zones
    return HeatPumpCommonSettings.model_validate(data)


def plan(start=START, hours=6, settings=None, **kwargs):
    params = GeneticHeatPumpParameters(
        device_id="hp1",
        settings=settings or device(),
        initial_room_c=21.0,
        initial_dhw_c=45.0,
        **kwargs,
    )
    n = hours * 4
    starts = [start.add(minutes=15 * i) for i in range(n)]
    return build_run_plan(params, starts, 0.25, [5.0] * n, [0.0] * n, [0.0003] * n)


def held(p, periods):
    """Executed heat pump modes per slot for heating modes per gene period."""
    agg, _ = p.held_slot_modes(list(periods), [])
    return agg


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def test_defaults_off_and_level_mapping():
    d = HeatPumpCommonSettings.model_validate({"device_id": "hp1"})
    assert d.optimization.min_hold_minutes == 0
    assert d.optimization.sg_ready_levels() == (1, 2, 3, 3, 4, 2)
    assert "hp1-sg-ready-actual" not in d.measurement_keys
    with_hold = device()
    assert {"hp1-sg-ready-actual", "hp1-sg-ready-changed-ts"} <= set(with_hold.measurement_keys)


def test_level_mapping_is_merged_and_validated():
    d = device(optimization={"sg_ready_of_mode": {"raised": 2}})
    assert d.optimization.sg_ready_of_mode == {**DEFAULT_SG_READY_OF_MODE, "RAISED": 2}
    with pytest.raises(ValidationError):
        device(optimization={"sg_ready_of_mode": {"BOOST": 3}})
    with pytest.raises(ValidationError):
        device(optimization={"sg_ready_of_mode": {"FORCED": 12}})
    with pytest.raises(ValidationError):
        device(hold=-1)


# ----------------------------------------------------------------------
# Hold logic
# ----------------------------------------------------------------------


def test_without_hold_the_planned_modes_pass_unchanged():
    p = plan(settings=device(hold=0))
    assert p.hold_seconds == 0
    periods = [R, N, F, N, R, R]
    assert held(p, periods) == [m for m in periods for _ in range(4)]


def test_change_waits_for_the_hold_after_the_reported_change():
    # 08:00 plan, SG 1 since 07:40 (20 min): leaving REDUCED for RAISED waits until 08:35.
    p = plan(sg_ready_actual=1, sg_ready_changed_at=START.subtract(minutes=20),
             active_heating_mode=0)
    modes = held(p, [RA, RA, N, N, N, N])
    assert modes[:3] == [R, R, R]  # 08:00-08:30 held (20+30 = 50 min < 55)
    assert modes[3] == RA  # 08:45: 65 min after the change
    assert modes[4] == RA


def test_return_to_normal_is_immediate_and_starts_a_new_hold():
    # SG 4 since 07:55; the plan wants NORMAL at once and FORCED again from 09:00.
    p = plan(sg_ready_actual=4, sg_ready_changed_at=START.subtract(minutes=5),
             active_heating_mode=4)
    modes = held(p, [N, F, F, N, N, N])
    assert modes[0] == N  # return to NORMAL at 08:00 (a change at t = 0)
    # FORCED at 09:00 is 60 min later -> allowed (>= 55 min)
    assert modes[4] == F
    p2 = plan(start=START.add(minutes=30), sg_ready_actual=4,
              sg_ready_changed_at=START.add(minutes=25), active_heating_mode=4)
    # periods start on full hours: first period 08:30-09:00 (2 slots)
    modes2 = held(p2, [N, F, F, N, N, N])
    assert modes2[:2] == [N, N]
    # FORCED from 09:00 would be 30 min after the return to NORMAL at 08:30 -> waits
    assert modes2[2:4] == [N, N]
    assert modes2[4] == F  # 09:30 = 60 min after 08:30


def test_reported_hold_blocks_forced_like_on_2026_11_05():
    # 09:32: SG 2 since 09:02, EOS wants FORCED -> waits until 09:57 (first slot 10:00).
    start = pendulum.datetime(2026, 11, 5, 9, 30, tz=TZ)
    p = plan(start=start, sg_ready_actual=2, sg_ready_changed_at=start.subtract(minutes=28),
             active_heating_mode=1)
    modes = held(p, [F, F, F, N, N, N])
    assert modes[:2] == [N, N]  # 09:30, 09:45
    assert modes[2] == F  # 10:00 (58 min after 09:02)


def test_same_level_changes_are_free():
    p = plan(sg_ready_actual=3, sg_ready_changed_at=START.subtract(minutes=1),
             active_heating_mode=2)
    modes = held(p, [P, RA, P, N, N, N])
    assert modes[:4] == [P] * 4 and modes[4:8] == [RA] * 4


def test_unknown_level_takes_the_first_planned_level():
    p = plan()  # no measurement, no previous plan
    assert p.hold_initial_level is None
    modes = held(p, [F, R, F, R, R, R])
    assert modes[:4] == [F] * 4
    assert modes[4] == R  # first change: nothing known before, allowed at once
    assert modes[8] == F  # hourly periods: 60 min after the change -> allowed


def test_level_from_previous_plan_without_measurement():
    p = plan(active_heating_mode=0, sg_ready_changed_at=START.subtract(minutes=10))
    assert p.hold_initial_level == 1
    modes = held(p, [F, F, N, N, N, N])
    assert modes[:3] == [R, R, R]
    assert modes[3] == F  # 08:45 = 55 min


def test_held_mode_when_the_previous_mode_has_another_level():
    # The controller reports SG 4 but the previous plan had NORMAL: FORCED is held.
    p = plan(sg_ready_actual=4, sg_ready_changed_at=START.subtract(minutes=10),
             active_heating_mode=1)
    modes = held(p, [RA, RA, N, N, N, N])
    assert modes[:3] == [F, F, F]


def test_hold_applies_in_the_simulation_and_the_plan_rows():
    p = plan(sg_ready_actual=1, sg_ready_changed_at=START.subtract(minutes=5),
             active_heating_mode=0)
    n = p.n_periods
    genes = p.genes_for_modes([F] * n, [0] * n)
    result = plan_result(p, genes)
    rows = result["heating_mode"]
    assert rows[:4] == ["REDUCED"] * 4  # 08:00-08:45 held (50 min < 55)
    assert rows[4] == "FORCED"
    # held slots run REDUCED: less energy than FORCED from the start
    unheld = plan(settings=device(hold=0))
    free = plan_result(unheld, unheld.genes_for_modes([F] * n, [0] * n))
    assert sum(result["electric_wh"][:4]) < sum(free["electric_wh"][:4])


# ----------------------------------------------------------------------
# Zones
# ----------------------------------------------------------------------


def test_zone_modes_stay_while_the_level_is_held():
    s = device(zones=ZONES, comfort={"fixed_room_setpoints": True})
    p = plan(settings=s, sg_ready_actual=4, sg_ready_changed_at=START.subtract(minutes=10),
             active_heating_mode=4, active_zone_modes={"og": 3, "eg": 4})
    assert p.hold_initial_modes == (3, 4)
    n = p.n_periods
    zones = [[RA] * n, [RA] * n]  # plan wants RAISED (SG 3) everywhere
    agg = [RA] * n
    out, zone_out = p.held_slot_modes(agg, zones)
    assert out[:3] == [F, F, F]
    assert [zone_out[0][0], zone_out[1][0]] == [P, F]  # the forcing pattern stays
    assert out[3] == RA and zone_out[0][3] == RA  # 08:45 = 55 min


def test_zone_pattern_changes_freely_within_the_level():
    s = device(zones=ZONES, comfort={"fixed_room_setpoints": True})
    p = plan(settings=s, sg_ready_actual=4, sg_ready_changed_at=START.subtract(minutes=1),
             active_heating_mode=4, active_zone_modes={"og": 4, "eg": 3})
    n = p.n_periods
    zones = [[P] * n, [F] * n]  # still SG 4, other zone forced
    out, zone_out = p.held_slot_modes([F] * n, zones)
    assert out[0] == F and zone_out[0][0] == P and zone_out[1][0] == F


def test_zone_plan_rows_follow_the_hold():
    s = device(zones=ZONES, comfort={"fixed_room_setpoints": True})
    p = plan(settings=s, sg_ready_actual=2, sg_ready_changed_at=START.subtract(minutes=50),
             active_heating_mode=1, active_zone_modes={"og": 1, "eg": 1})
    n = p.n_periods
    genes = [p.heating_index(F)] * n + [p.heating_index(N)] * n + [0] * n  # og FORCED
    rows = plan_result(p, genes)
    assert rows["zone_og_heating_mode"][0] == "NORMAL"  # 08:00 (50 min) held
    assert rows["zone_og_heating_mode"][1] == "FORCED"  # 08:15 (65 min)
    assert rows["heating_mode"][1] == "FORCED"


# ----------------------------------------------------------------------
# Measurements and previous plan (configrequest)
# ----------------------------------------------------------------------


def test_active_zone_modes_from_the_previous_plan(monkeypatch):
    start = START
    schedule = {
        "start_datetime": start.isoformat(),
        "slot_seconds": 900,
        "zone_og_heating_mode": ["PREHEAT", "PREHEAT", "NORMAL"],
        "zone_eg_heating_mode": ["FORCED", "FORCED", "NORMAL"],
    }
    solution = SimpleNamespace(heat_pumps={"hp1": schedule})
    monkeypatch.setattr(
        configrequest, "get_ems", lambda: SimpleNamespace(genetic_solution=lambda: solution)
    )
    got = ConfigOptimizationRequest._active_zone_modes("hp1", start.add(minutes=20), ["og", "eg"])
    assert got == {"og": 3, "eg": 4}
    assert (
        ConfigOptimizationRequest._active_zone_modes("hp1", start.add(hours=2), ["og", "eg"])
        is None
    )
    assert ConfigOptimizationRequest._active_zone_modes("hp1", start, ["og", "xx"]) is None


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
async def test_request_reads_level_and_change_time(config_eos, isolated_measurement):
    from akkudoktoreos.core.coreabc import get_ems

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
                "heat_pumps": {"hp1": {"optimization": {"min_hold_minutes": 55}}},
            },
        }
    )
    now = pendulum.datetime(2026, 11, 20, 9, 7, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    await isolated_measurement.update_value(now, "hp1-room-temperature", 20.5)
    await isolated_measurement.update_value(now, "hp1-dhw-temperature", 45.0)

    def request() -> ConfigOptimizationRequest:
        return ConfigOptimizationRequest.model_validate(
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

    async def params():
        heat_pumps = (await request().resolve()).heat_pumps
        assert heat_pumps is not None
        return heat_pumps[0]

    first = await params()
    assert first.sg_ready_actual is None and first.sg_ready_changed_at is None
    changed = now.subtract(minutes=20)
    await isolated_measurement.update_value(now, "hp1-sg-ready-actual", 3.0)
    await isolated_measurement.update_value(now, "hp1-sg-ready-changed-ts", changed.timestamp())
    got = await params()
    assert got.sg_ready_actual == 3
    assert got.sg_ready_changed_at.timestamp() == pytest.approx(changed.timestamp())
    # a change time in the future (clock skew > 1 min) is ignored
    await isolated_measurement.update_value(
        now, "hp1-sg-ready-changed-ts", now.add(hours=1).timestamp()
    )
    assert (await params()).sg_ready_changed_at is None
