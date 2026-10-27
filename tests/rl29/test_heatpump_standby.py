"""Heat pump standby, restart hot water charge and presence-dependent gains (RL29 fork).

Measured on the WBB 12 (KNX power meter): about 10 W in the standby operating mode,
30-35 W ready between runs. Every restart from standby charges the hot water tank
(cannot be switched off). Nobody at home means less heat from people and devices.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pendulum
import pytest

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.building import BuildingParameters
from akkudoktoreos.devices.thermal.calibration import (
    CalibrationOptions,
    HourlySample,
    _daily_aggregates,
)
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel
from akkudoktoreos.devices.thermal.dhw import DhwTankParameters
from akkudoktoreos.devices.thermal.heatpump import (
    DhwMode,
    HeatingMode,
    HeatPumpParameters,
    HeatPumpSystem,
    SlotInputs,
    ThermalState,
)
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
)

T0 = pendulum.datetime(2026, 11, 3, 10, 0, tz="Europe/Berlin")


def system(off_w=10.0, standby_w=32.0, gains=300.0):
    return HeatPumpSystem(
        building=BuildingParameters(
            slab_capacity_wh_per_k=3000,
            room_capacity_wh_per_k=8000,
            loss_w_per_k=150,
            slab_room_w_per_k=600,
            emitter_w_per_k=850,
            internal_gains_w=gains,
        ),
        tank=DhwTankParameters(volume_l=190, loss_w_per_k=1.5),
        cop=CopModel(WBB12_RATING_POINTS),
        heat_pump=HeatPumpParameters(standby_w=standby_w, off_w=off_w),
        slot_hours=0.25,
    )


def away_slot(**kwargs):
    values = dict(
        outdoor_c=5.0,
        comfort_min_c=16.0,
        setpoint_c=18.0,
        comfort_max_c=22.0,
        dhw_off=True,
        standby_possible=True,
        standby_exit_dhw_c=41.0,
    )
    values.update(kwargs)
    return SlotInputs(**values)


def test_standby_draws_off_power_and_no_heat():
    sys_ = system()
    state = ThermalState(slab_c=19.0, room_c=18.5, dhw_c=29.0, standby=True)
    result = sys_.step(state, away_slot(), HeatingMode.REDUCED, DhwMode.HOLD)
    assert result.state.standby
    assert result.heat_space_wh == 0.0 and result.heat_dhw_hp_wh == 0.0
    assert result.hp_electric_wh == pytest.approx(10.0 * 0.25)
    # Ready but not in standby (NORMAL above its target): standby_w
    ready = sys_.step(
        ThermalState(slab_c=19.0, room_c=18.5, dhw_c=41.0),
        away_slot(setpoint_c=17.0),
        HeatingMode.NORMAL,
        DhwMode.HOLD,
    )
    assert not ready.state.standby
    assert ready.hp_electric_wh >= 32.0 * 0.25 - 1e-9


def test_restart_from_standby_charges_hot_water_to_the_target():
    sys_ = system()
    state = ThermalState(slab_c=19.0, room_c=18.5, dhw_c=28.5, standby=True)
    energy = 0.0
    temps = []
    for _ in range(8):  # two hours of NORMAL
        result = sys_.step(state, away_slot(), HeatingMode.NORMAL, DhwMode.HOLD)
        state = result.state
        energy += result.hp_electric_wh
        temps.append(state.dhw_c)
        assert result.dhw_violation_kh == 0.0
    assert max(temps) == pytest.approx(41.0, abs=0.6)  # charged up although hot water is off
    assert not state.standby
    assert energy > 600  # ~2.4 kWh heat at a COP around 3: clearly more than idling
    # Without the restart rule (standby_exit_dhw_c None) nothing is charged
    state = ThermalState(slab_c=19.0, room_c=18.5, dhw_c=28.5, standby=True)
    quiet = sys_.step(state, away_slot(standby_exit_dhw_c=None), HeatingMode.NORMAL, DhwMode.HOLD)
    assert quiet.heat_dhw_hp_wh == 0.0


def test_room_at_the_floor_ends_standby():
    sys_ = system()
    state = ThermalState(slab_c=16.5, room_c=16.1, dhw_c=30.0, standby=True)
    result = sys_.step(state, away_slot(), HeatingMode.REDUCED, DhwMode.HOLD)
    assert not result.state.standby
    assert result.heat_dhw_hp_wh > 0  # the restart charges hot water first
    space = 0.0
    state = result.state
    for _ in range(6):
        result = sys_.step(state, away_slot(), HeatingMode.REDUCED, DhwMode.HOLD)
        state = result.state
        space += result.heat_space_wh
    assert space > 0  # then the controller heats at the floor


def test_away_gains_need_more_heat():
    sys_ = system(gains=400.0)
    state = ThermalState(slab_c=22.0, room_c=20.0, dhw_c=45.0)
    home = SlotInputs(outdoor_c=0.0, comfort_min_c=19.5, setpoint_c=20.0)
    away = SlotInputs(outdoor_c=0.0, comfort_min_c=19.5, setpoint_c=20.0, internal_gains_factor=0.3)
    assert sys_.slot_gains_w(away) == pytest.approx(120.0)
    h = sys_.step(state, home, HeatingMode.NORMAL, DhwMode.HOLD)
    a = sys_.step(state, away, HeatingMode.NORMAL, DhwMode.HOLD)
    assert a.heat_space_wh > h.heat_space_wh


def holiday_plan(**away):
    device = HeatPumpCommonSettings(
        device_id="wbb12",
        unit={"standby_w": 32.0, "off_w": 10.0},
        building={"internal_gains_w": 300.0},
        away={"standby_exit_dhw_c": 41.0, "internal_gains_share": 0.4, **away},
    )
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=device,
        initial_room_c=18.8,
        initial_dhw_c=29.0,
        away=True,
        away_until=T0.add(hours=40),
        away_room_c=18.0,
        away_min_c=16.0,
    )
    starts = [T0.add(hours=i) for i in range(48)]
    return build_run_plan(params, starts, 1.0, [8.0] * 48, [0.0] * 48, [0.0003] * 48)


def test_holiday_plan_prefers_standby_over_normal():
    run = holiday_plan()
    first = run.inputs[0]
    assert first.standby_possible and first.standby_exit_dhw_c == 41.0
    assert first.internal_gains_factor == 0.4
    assert not run.inputs[45].standby_possible  # at home again
    assert run.initial.standby
    n = run.n_periods
    reduced = run.evaluate(run.genes_for_modes([HeatingMode.REDUCED] * n, [DhwMode.HOLD] * n))
    normal = run.evaluate(run.genes_for_modes([HeatingMode.NORMAL] * n, [DhwMode.HOLD] * n))
    # 10 hours in standby: ~10 W; NORMAL restarts the heat pump and charges hot water
    assert sum(reduced.electric_wh[:10]) == pytest.approx(100.0, abs=1.0)
    assert sum(normal.electric_wh[:10]) > 600.0


def test_holiday_days_are_left_out_of_the_building_fit():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    samples = []
    for h in range(24 * 4):
        stamp = start + timedelta(hours=h)
        samples.append(
            HourlySample(
                stamp,
                outdoor_c=5.0,
                room_c=20.0,
                heat_w=2000.0,
                away=1.0 if h >= 48 else 0.0,
            )
        )
    days = _daily_aggregates(samples, CalibrationOptions(), timezone.utc)
    assert len(days) == 2


def test_milp_keeps_the_heat_pump_in_standby_while_away(config_eos):
    from akkudoktoreos.optimization.milp.milpoptimization import MilpOptimization

    from .test_milp_optimization import configure, parameters

    configure(config_eos, hours=12, polish_generations=0, hp_search_seconds=3)
    settings = HeatPumpCommonSettings(
        device_id="wbb12",
        unit={"standby_w": 32.0, "off_w": 10.0},
        away={"standby_exit_dhw_c": 41.0},
    )
    hp = {
        "device_id": "wbb12",
        "settings": settings,
        "initial_room_c": 18.8,
        "initial_dhw_c": 29.0,
        "away": True,
        "away_until": "2026-11-05T00:00:00+01:00",
        "away_room_c": 18.0,
        "away_min_c": 16.0,
    }
    params = parameters(hours=12, battery=False, heat_pumps=[hp], temperature_forecast=[8.0] * 12)
    optimizer = MilpOptimization(fixed_seed=1)
    solution = optimizer.optimize_ems(params, ngen=0, individuals=20)
    plan = optimizer._hp_layout().plans[0]
    heating, dhw = plan.modes(optimizer._heat_pump_genes(list(solution.start_solution)))
    assert all(mode == HeatingMode.REDUCED for mode in heating)
    assert all(option.mode == DhwMode.HOLD for option in dhw)


def test_without_restart_charge_heating_out_of_standby_costs_only_ready_power():
    """HA lowers the hot water setpoints while away: leaving standby charges nothing."""
    run = holiday_plan(standby_exit_dhw_c=None)
    assert run.inputs[0].standby_possible and run.inputs[0].standby_exit_dhw_c is None
    n = run.n_periods
    reduced = run.evaluate(run.genes_for_modes([HeatingMode.REDUCED] * n, [DhwMode.HOLD] * n))
    normal = run.evaluate(run.genes_for_modes([HeatingMode.NORMAL] * n, [DhwMode.HOLD] * n))
    # No hot water: NORMAL above its target only idles at the ready power (32 W)
    assert sum(normal.electric_wh[:5]) == pytest.approx(5 * 32.0, abs=1.0)
    assert sum(reduced.electric_wh[:5]) == pytest.approx(5 * 10.0, abs=1.0)
