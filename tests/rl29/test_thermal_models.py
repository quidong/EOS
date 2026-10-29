"""Thermal device models: COP, building, hot water tank, heat pump system (RL29 fork)."""

from __future__ import annotations

import math

import numpy as np
import pendulum
import pytest
from pydantic import ValidationError

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.building import BuildingModel, BuildingParameters
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel, CopPoint
from akkudoktoreos.devices.thermal.dhw import DhwTank, DhwTankParameters
from akkudoktoreos.devices.thermal.heatpump import (
    DhwControlParameters,
    DhwMode,
    HeatingMode,
    HeatPumpSystem,
    SlotInputs,
    ThermalState,
)

BUILDING = BuildingParameters(
    slab_capacity_wh_per_k=3450,
    room_capacity_wh_per_k=10000,
    loss_w_per_k=180,
    slab_room_w_per_k=1500,
    emitter_w_per_k=1000,
    solar_aperture_m2=2.0,
    internal_gains_w=300,
)
TANK = DhwTankParameters(volume_l=190, loss_w_per_k=1.5, ambient_c=18)


def system(rod_w: float = 3000.0, slot_hours: float = 0.25) -> HeatPumpSystem:
    return HeatPumpSystem(
        BUILDING,
        TANK,
        CopModel(WBB12_RATING_POINTS),
        dhw=DhwControlParameters(rod_power_w=rod_w),
        slot_hours=slot_hours,
    )


def run(sim, state, inputs, modes, slots):
    results = []
    for i in range(slots):
        heating, dhw = modes(i) if callable(modes) else modes
        result = sim.step(state, inputs(i) if callable(inputs) else inputs, heating, dhw)
        state = result.state
        results.append(result)
    return state, results


# ---------------------------------------------------------------------------
# COP
# ---------------------------------------------------------------------------


class TestCop:
    def test_fit_reproduces_rating_points(self):
        model = CopModel(WBB12_RATING_POINTS)
        assert max(abs(r) for r in model.residuals()) < 0.05

    def test_monotonic(self):
        model = CopModel(WBB12_RATING_POINTS)
        assert model.cop(10, 35) > model.cop(0, 35) > model.cop(-10, 35)
        assert model.cop(0, 30) > model.cop(0, 40) > model.cop(0, 50)

    def test_scale_and_bounds(self):
        model = CopModel(WBB12_RATING_POINTS, scale=0.8)
        assert model.cop(2, 35) == pytest.approx(0.8 * CopModel(WBB12_RATING_POINTS).cop(2, 35))
        assert model.cop(30, 25) <= 8.0  # lift clamp keeps it finite
        assert CopModel([CopPoint(7, 35, 1.2)], scale=0.5).cop(-20, 60) == 1.0

    def test_single_point(self):
        model = CopModel([CopPoint(2, 35, 4.0)])
        assert model.cop(2, 35) == pytest.approx(4.0)

    @pytest.mark.parametrize("kwargs", [{"points": []}, {"scale": 0}, {"cop_min": 2, "cop_max": 1}])
    def test_invalid(self, kwargs):
        args = {"points": WBB12_RATING_POINTS, **kwargs}
        with pytest.raises(ValueError):
            CopModel(**args)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


class TestBuilding:
    def test_matches_fine_euler_integration(self):
        model = BuildingModel(BUILDING, 0.25)
        s, r = 25.0, 20.0
        es, er = s, r
        p = BUILDING
        for _ in range(8):
            s, r = model.step(s, r, 3000.0, 0.0, 500.0)
            for _ in range(900):  # 1 s Euler steps
                dt = 1 / 3600
                ds = (3000.0 - p.slab_room_w_per_k * (es - er)) / p.slab_capacity_wh_per_k
                dr = (
                    p.slab_room_w_per_k * (es - er) - p.loss_w_per_k * (er - 0.0) + 500.0
                ) / p.room_capacity_wh_per_k
                es, er = es + ds * dt, er + dr * dt
        assert s == pytest.approx(es, abs=0.01)
        assert r == pytest.approx(er, abs=0.01)

    def test_steady_state_holds(self):
        model = BuildingModel(BUILDING, 0.25)
        gains = model.gains_w(0.0)
        slab = model.steady_slab_c(21.0, 5.0, gains)
        heat = model.steady_heat_w(21.0, 5.0, gains)
        s, r = model.step(slab, 21.0, heat, 5.0, gains)
        assert s == pytest.approx(slab, abs=1e-9)
        assert r == pytest.approx(21.0, abs=1e-9)

    def test_heat_for_room_target_is_exact(self):
        model = BuildingModel(BUILDING, 0.25)
        q = model.heat_for_room_target(24.0, 20.0, 0.0, 300.0, 20.2)
        _, r = model.step(24.0, 20.0, q, 0.0, 300.0)
        assert r == pytest.approx(20.2)

    def test_energy_balance(self):
        """Stored heat change = input - losses (trapezoid over fine steps)."""
        model = BuildingModel(BUILDING, 1 / 3600)
        s, r = 26.0, 20.0
        start = model.stored_heat_wh(s, r, 0.0)
        heat_in = 0.0
        loss = 0.0
        for _ in range(3600):
            loss += BUILDING.loss_w_per_k * (r - 2.0) / 3600
            s, r = model.step(s, r, 2000.0, 2.0, 0.0)
            heat_in += 2000.0 / 3600
        end = model.stored_heat_wh(s, r, 0.0)
        assert end - start == pytest.approx(heat_in - loss, rel=1e-3)

    def test_invalid_parameters(self):
        with pytest.raises(ValueError):
            BuildingModel(BuildingParameters(0, 1, 1, 1, 1), 0.25)
        with pytest.raises(ValueError):
            BuildingModel(BUILDING, 0)


# ---------------------------------------------------------------------------
# Hot water tank
# ---------------------------------------------------------------------------


class TestDhwTank:
    def test_capacity(self):
        assert DhwTankParameters(190, 1.5).capacity_wh_per_k == pytest.approx(220.97)

    def test_exponential_decay(self):
        tank = DhwTank(TANK, 24.0)
        t = tank.step(60.0, 0.0, 0.0)
        expected = 18 + 42 * math.exp(-1.5 * 24 / (190 * 1.163))
        assert t == pytest.approx(expected)

    def test_hotter_tank_loses_more(self):
        tank = DhwTank(TANK, 24.0)
        loss_45 = 45 - tank.step(45.0, 0.0, 0.0)
        loss_70 = 70 - tank.step(70.0, 0.0, 0.0)
        assert loss_70 / loss_45 == pytest.approx((70 - 18) / (45 - 18), rel=1e-6)

    def test_heat_to_reach_is_exact(self):
        tank = DhwTank(TANK, 0.25)
        q = tank.heat_to_reach(42.0, 45.0, 100.0)
        assert tank.step(42.0, q, 100.0) == pytest.approx(45.0)

    def test_no_loss_tank(self):
        tank = DhwTank(DhwTankParameters(100, 0.0), 1.0)
        assert tank.step(40.0, 116.3, 0.0) == pytest.approx(41.0)


# ---------------------------------------------------------------------------
# Heat pump system
# ---------------------------------------------------------------------------

COLD = SlotInputs(outdoor_c=0.0, comfort_min_c=20.5, setpoint_c=21.0, comfort_max_c=22.0)


class TestHeatPumpSystem:
    def test_normal_mode_holds_setpoint(self):
        sim = system()
        state = ThermalState(slab_c=23.0, room_c=21.0, dhw_c=45.0)
        state, results = run(sim, state, COLD, (HeatingMode.NORMAL, DhwMode.HOLD), 96)
        assert state.room_c == pytest.approx(21.0, abs=0.05)
        heat = sum(r.heat_space_wh for r in results)
        # Daily heat roughly equals steady losses at 0 degC minus internal gains.
        assert heat == pytest.approx((180 * 21 - 300) * 24, rel=0.1)
        assert all(r.comfort_violation_kh == 0 for r in results)

    def test_preheat_stores_heat_and_reduced_coasts(self):
        sim = system()
        start = ThermalState(23.0, 21.0, 45.0)
        charged, pre = run(sim, start, COLD, (HeatingMode.PREHEAT, DhwMode.HOLD), 16)
        assert charged.slab_c > start.slab_c + 1.5
        assert charged.room_c > 21.0
        # Coasting afterwards needs less heat than normal operation over the next 8 h.
        _, coast = run(sim, charged, COLD, (HeatingMode.REDUCED, DhwMode.HOLD), 32)
        _, normal = run(sim, start, COLD, (HeatingMode.NORMAL, DhwMode.HOLD), 32)
        assert sum(r.heat_space_wh for r in coast) < sum(r.heat_space_wh for r in normal)
        # Pre-heating runs at a higher flow temperature, i.e. a lower COP.
        assert pre[0].flow_c > normal[0].flow_c
        assert pre[0].cop_space < normal[0].cop_space

    def test_comfort_repair_in_reduced_mode(self):
        sim = system()
        state = ThermalState(slab_c=20.6, room_c=20.55, dhw_c=45.0)
        state, results = run(sim, state, COLD, (HeatingMode.REDUCED, DhwMode.HOLD), 48)
        assert state.room_c >= 20.5 - 0.02
        assert sum(r.forced_space_wh for r in results) == pytest.approx(
            0.0
        )  # REDUCED targets the minimum
        assert sum(r.heat_space_wh for r in results) > 0

    def test_never_above_comfort_max(self):
        sim = system()
        state = ThermalState(30.0, 22.5, 45.0)
        result = sim.step(state, COLD, HeatingMode.PREHEAT, DhwMode.HOLD)
        assert result.heat_space_wh == 0.0

    def test_power_limit_caps_electric_power(self):
        sim = system()
        state = ThermalState(21.0, 20.0, 35.0)
        limited = SlotInputs(**{**COLD.__dict__, "max_electric_w": 1500.0})
        result = sim.step(state, limited, HeatingMode.PREHEAT, DhwMode.ROD)
        assert result.electric_wh <= 1500.0 * 0.25 + 1e-6
        assert "power limit" in result.notes
        # The rod is cut before space heating.
        assert result.rod_electric_wh == pytest.approx(0.0)

    def test_heat_pump_dhw_charges_to_setpoint_only(self):
        sim = system()
        state = ThermalState(23.0, 21.0, 41.0)
        state, results = run(sim, state, COLD, (HeatingMode.NORMAL, DhwMode.HEAT_PUMP), 12)
        peak = max(r.state.dhw_c for r in results)
        assert peak == pytest.approx(45.0, abs=0.3)
        assert all(r.rod_electric_wh == 0 for r in results)
        assert results[0].cop_dhw > 1.5

    def test_rod_charges_to_70(self):
        sim = system()
        state = ThermalState(23.0, 21.0, 45.0)
        state, results = run(sim, state, COLD, (HeatingMode.NORMAL, DhwMode.ROD), 48)
        assert state.dhw_c == pytest.approx(70.0, abs=0.3)
        rod_kwh = sum(r.rod_electric_wh for r in results) / 1000
        # 25 K x 0.221 kWh/K plus standby losses and tapping-free hours.
        assert 5.5 < rod_kwh < 6.5
        assert all(r.rod_electric_wh <= 3000 * 0.25 + 1e-9 for r in results)

    def test_hold_mode_reheats_below_switch_on(self):
        sim = system()
        state = ThermalState(23.0, 21.0, 39.0)
        result = sim.step(state, COLD, HeatingMode.NORMAL, DhwMode.HOLD)
        assert result.heat_dhw_hp_wh > 0
        assert result.forced_dhw_wh > 0
        # The charge continues until the setpoint (hysteresis memory).
        assert result.state.hp_dhw_charging
        assert result.state.hp_dhw_forced

    def test_forced_charge_counts_in_every_slot(self):
        """The controller's own reheat is reported as forced until it ends,
        not only in its first slot; a charge the plan started is never forced.
        """
        sim = system()
        state, results = run(
            sim, ThermalState(23.0, 21.0, 39.0), COLD, (HeatingMode.NORMAL, DhwMode.HOLD), 12
        )
        charging = [r for r in results if r.heat_dhw_hp_wh > 0]
        assert len(charging) >= 2
        assert all(r.forced_dhw_wh == pytest.approx(r.heat_dhw_hp_wh) for r in charging)
        assert sum(r.forced_dhw_wh for r in results) == pytest.approx(
            sum(r.heat_dhw_hp_wh for r in results)
        )
        assert not state.hp_dhw_charging and not state.hp_dhw_forced
        # started by the plan (HEAT_PUMP), continued under HOLD: not forced
        first = sim.step(
            ThermalState(23.0, 21.0, 40.2), COLD, HeatingMode.NORMAL, DhwMode.HEAT_PUMP
        )
        assert first.heat_dhw_hp_wh > 0 and first.forced_dhw_wh == 0
        assert not first.state.hp_dhw_forced
        second = sim.step(first.state, COLD, HeatingMode.NORMAL, DhwMode.HOLD)
        assert second.forced_dhw_wh == 0

    def test_dhw_charging_reduces_space_heating_capacity(self):
        sim = system()
        state = ThermalState(20.0, 20.0, 41.0)
        both = sim.step(state, COLD, HeatingMode.PREHEAT, DhwMode.HEAT_PUMP)
        heat_only = sim.step(state, COLD, HeatingMode.PREHEAT, DhwMode.HOLD)
        assert both.heat_space_wh < heat_only.heat_space_wh

    def test_tapping_cools_tank(self):
        sim = system(rod_w=0)
        state = ThermalState(23.0, 21.0, 50.0)
        tap = SlotInputs(**{**COLD.__dict__, "draw_wh": 500.0})
        result = sim.step(state, tap, HeatingMode.NORMAL, DhwMode.HOLD)
        assert result.state.dhw_c == pytest.approx(50.0 - 500 / (190 * 1.163), abs=0.1)

    def test_standby_power_always_counted(self):
        sim = system()
        state = ThermalState(30.0, 22.5, 45.0)
        result = sim.step(state, COLD, HeatingMode.REDUCED, DhwMode.HOLD)
        assert result.hp_electric_wh == pytest.approx(22.0 * 0.25)

    def test_energy_balance_of_electric_and_heat(self):
        sim = system()
        state = ThermalState(22.0, 20.8, 42.0)
        _, results = run(sim, state, COLD, (HeatingMode.RAISED, DhwMode.HEAT_PUMP), 8)
        for r in results:
            heat_hp = r.heat_space_wh + r.heat_dhw_hp_wh
            expected = (
                (r.heat_space_wh / r.cop_space if r.heat_space_wh else 0)
                + (r.heat_dhw_hp_wh / r.cop_dhw if r.heat_dhw_hp_wh else 0)
                + 22.0 * 0.25
            )
            assert r.hp_electric_wh == pytest.approx(expected)
            assert heat_hp >= 0


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_defaults_build_a_system(self):
        settings = HeatPumpCommonSettings(device_id="wbb12")
        sim = settings.to_system(0.25)
        assert isinstance(sim, HeatPumpSystem)
        assert sim.dhw.setpoint_c == 45.0
        assert settings.measurement_key_dhw_temperature == "wbb12-dhw-temperature"
        assert len(settings.measurement_keys) == 10  # 5 state + 4 holiday-mode keys + lock

    def test_measured_setpoint_overrides_config(self):
        sim = HeatPumpCommonSettings(device_id="x").to_system(0.25, dhw_setpoint_c=48.0)
        assert sim.dhw.setpoint_c == 48.0

    def test_comfort_minimum_by_time_of_day(self):
        settings = HeatPumpCommonSettings(device_id="x")
        tz = "Europe/Berlin"
        assert settings.comfort.minimum_at(pendulum.datetime(2026, 11, 3, 3, tz=tz)) == 19.5
        assert settings.comfort.minimum_at(pendulum.datetime(2026, 11, 3, 12, tz=tz)) == 20.5
        assert settings.comfort.minimum_at(pendulum.datetime(2026, 11, 3, 23, tz=tz)) == 19.5

    def test_slot_inputs(self):
        settings = HeatPumpCommonSettings.model_validate(
            {
                "device_id": "x",
                "power_limit_w": {
                    "windows": [{"start_time": "19:00", "duration": "2 hours", "value": 4200}]
                },
            }
        )
        tz = "Europe/Berlin"
        starts = [pendulum.datetime(2026, 11, 3, 18, tz=tz).add(minutes=15 * i) for i in range(16)]
        temps = [5.0, None] + [None] * 14
        inputs = settings.slot_inputs(starts, 0.25, temps)
        assert len(inputs) == 16
        assert inputs[1].outdoor_c == 5.0  # carried forward
        assert inputs[0].max_electric_w is None
        assert inputs[4].max_electric_w == 4200.0  # 19:00
        assert inputs[12].max_electric_w is None  # 21:00
        assert inputs[0].draw_wh == pytest.approx(350 * 0.25)

    def test_invalid_draw_profile(self):
        with pytest.raises(ValidationError):
            HeatPumpCommonSettings.model_validate(
                {"device_id": "x", "dhw": {"draw_profile_wh": [1.0] * 23}}
            )

    def test_registered_in_device_config(self, config_eos):
        config_eos.merge_settings_from_dict(
            {"devices": {"heat_pumps": {"wbb12": {"dhw": {"rod_power_w": 3000}}}}}
        )
        hp = config_eos.devices.heat_pumps["wbb12"]
        assert hp.device_id == "wbb12"
        assert hp.dhw.rod_power_w == 3000
        assert "wbb12-dhw-temperature" in config_eos.devices.measurement_keys

    def test_device_id_must_match_key(self, config_eos):
        with pytest.raises(ValueError):
            config_eos.merge_settings_from_dict(
                {"devices": {"heat_pumps": {"wbb12": {"device_id": "other"}}}}
            )


def test_daily_cop_in_measured_range():
    """With a calibrated scale the model lands in the measured daily COP range (3.7-4.5).

    The rated points describe steady operation; real daily COPs include cycling,
    defrost and pumps, so the calibration typically yields a scale of 0.6-0.8.
    """
    sim = HeatPumpSystem(BUILDING, TANK, CopModel(WBB12_RATING_POINTS, scale=0.65), slot_hours=0.25)
    state = ThermalState(22.0, 21.0, 45.0)
    inputs = SlotInputs(outdoor_c=12.0, comfort_min_c=20.5, setpoint_c=21.0, draw_wh=4000 / 96)
    state, results = run(sim, state, inputs, (HeatingMode.NORMAL, DhwMode.HOLD), 96)
    heat = sum(r.heat_space_wh + r.heat_dhw_hp_wh for r in results)
    electric = sum(r.hp_electric_wh for r in results)
    assert 3.5 < heat / electric < 5.2
    assert np.isfinite(state.room_c)
