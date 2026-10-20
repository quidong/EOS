"""Heat pump genes in the GENETIC optimizer (RL29 fork).

Covers the gene layout and thermal evaluation in isolation, the integration
into the genome (regression without heat pump, genome position, warm start),
real small optimizer runs and the configuration-owned request with
measurements.
"""

from __future__ import annotations

import random
from collections.abc import AsyncGenerator
from typing import Any

import numpy as np
import pendulum
import pytest
import pytest_asyncio
from deap import creator

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.core.emplan import FRBCInstruction
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import DhwMode, HeatingMode
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    HeatPumpGeneLayout,
    build_run_plan,
    plan_result,
)
from akkudoktoreos.utils.datetimeutil import to_datetime

T0 = pendulum.datetime(2026, 11, 3, 10, 0, tz="Europe/Berlin")


def make_plan(
    n_slots: int = 24,
    slot_hours: float = 1.0,
    rod_w: float = 3000.0,
    hours_since_legionella: float | None = None,
    outdoor: float = 2.0,
    prices: list[float] | None = None,
    **settings: Any,
):
    device = HeatPumpCommonSettings(device_id="wbb12", dhw={"rod_power_w": rod_w}, **settings)
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=device,
        initial_room_c=21.0,
        initial_dhw_c=45.0,
        hours_since_legionella=hours_since_legionella,
    )
    starts = [T0.add(seconds=int(slot_hours * 3600 * i)) for i in range(n_slots)]
    return build_run_plan(
        params,
        starts,
        slot_hours,
        [outdoor] * n_slots,
        [0.0] * n_slots,
        prices if prices is not None else [0.0003] * n_slots,
    )


# ----------------------------------------------------------------------
# Gene layout and thermal evaluation
# ----------------------------------------------------------------------


class TestRunPlan:
    def test_layout_and_status_quo(self):
        plan = make_plan()
        assert plan.n_periods == 24
        assert plan.n_genes == 48
        heating, dhw = plan.modes(plan.default_genes())
        assert set(heating) == {HeatingMode.NORMAL}
        assert set(dhw) == {DhwMode.HOLD}
        assert plan.valid(plan.default_genes())
        # Default modes: PREHEAT needs the KNX forced valve position first.
        assert plan.heating_modes == (0, 1, 2)
        assert plan.dhw_modes == (0, 1, 2, 3)

    def test_quarter_hour_slots_share_hourly_genes(self):
        plan = make_plan(n_slots=96, slot_hours=0.25)
        assert plan.slots_per_gene == 4
        assert plan.n_periods == 24

    def test_rod_modes_need_a_rod(self):
        plan = make_plan(rod_w=0.0)
        assert plan.dhw_modes == (0, 1)
        assert plan.legionella_deadline_slot is None

    def test_status_quo_keeps_comfort(self):
        plan = make_plan()
        outcome = plan.evaluate(plan.default_genes())
        assert outcome.comfort_kh == pytest.approx(0.0, abs=1e-6)
        assert outcome.dhw_kh == pytest.approx(0.0, abs=1e-6)
        assert 5000 < sum(outcome.electric_wh) < 40000
        assert outcome.mode_changes == 0

    def test_results_are_cached_per_gene_combination(self):
        plan = make_plan()
        genes = plan.default_genes()
        assert plan.evaluate(genes) is plan.evaluate(list(genes))
        assert plan.evaluate(genes, detail=True).detail is not None
        assert plan.evaluate(genes).detail is None

    def test_raised_operation_stores_heat_and_costs_energy(self):
        plan = make_plan()
        base = plan.evaluate(plan.default_genes())
        raised = plan.evaluate(plan.genes_for_modes([HeatingMode.RAISED] * 24, [DhwMode.HOLD] * 24))
        assert sum(raised.electric_wh) > sum(base.electric_wh)
        assert raised.stored_heat_credit > base.stored_heat_credit

    def test_reduced_operation_saves_energy(self):
        plan = make_plan()
        base = plan.evaluate(plan.default_genes())
        # Reduced during the day (10:00-22:00, constant comfort minimum).
        day = [HeatingMode.REDUCED] * 12 + [HeatingMode.NORMAL] * 12
        reduced = plan.evaluate(plan.genes_for_modes(day, [DhwMode.HOLD] * 24))
        assert sum(reduced.electric_wh) < sum(base.electric_wh)
        # The comfort minimum still holds (controller repair).
        assert reduced.comfort_kh == pytest.approx(0.0, abs=0.05)

    def test_reduced_into_a_rising_comfort_minimum_is_penalized(self):
        """Night setback until 06:00 cannot reach the day minimum instantly."""
        plan = make_plan()
        outcome = plan.evaluate(
            plan.genes_for_modes([HeatingMode.REDUCED] * 24, [DhwMode.HOLD] * 24)
        )
        assert outcome.comfort_kh > 0.1
        assert outcome.extra_cost > plan.evaluate(plan.default_genes()).extra_cost

    def test_mode_changes_cost(self):
        plan = make_plan()
        heating = [HeatingMode.NORMAL, HeatingMode.RAISED] * 12
        outcome = plan.evaluate(plan.genes_for_modes(heating, [DhwMode.HOLD] * 24))
        assert outcome.mode_changes == 23

    def test_power_limit_window_caps_electric_power(self):
        plan = make_plan(
            outdoor=-10.0,
            power_limit_w={
                "windows": [{"start_time": "17:00", "duration": "2 hours", "value": 1500}]
            },
        )
        genes = plan.genes_for_modes([HeatingMode.RAISED] * 24, [DhwMode.ROD_70] * 24)
        outcome = plan.evaluate(genes)
        # T0 is 10:00 local: slots 7 and 8 are 17:00 and 18:00.
        assert outcome.electric_wh[7] <= 1500.0 + 1e-6
        assert outcome.electric_wh[8] <= 1500.0 + 1e-6
        assert max(outcome.electric_wh) > 1500.0


class TestLegionella:
    def test_unknown_history_requires_nothing(self):
        assert make_plan(hours_since_legionella=None).legionella_deadline_slot is None

    def test_deadline_beyond_horizon_requires_nothing(self):
        assert make_plan(hours_since_legionella=24.0).legionella_deadline_slot is None

    def test_due_heat_up_is_planned_with_the_rod(self):
        plan = make_plan(hours_since_legionella=7 * 24 - 12)
        assert plan.legionella_deadline_slot == 12
        status_quo = plan.evaluate(plan.default_genes())
        assert status_quo.legionella_ok is False
        seeds = plan.heuristic_genes([0.0] * 24, [300.0] * 24, [0.0003] * 24)
        outcome = plan.evaluate(seeds[0])
        assert outcome.legionella_ok is True
        assert outcome.extra_cost < status_quo.extra_cost
        _, dhw = plan.modes(seeds[0])
        assert any(mode in (DhwMode.ROD_60, DhwMode.ROD_70) for mode in dhw[:12])

    def test_overdue_heat_up_is_required_within_the_horizon(self):
        plan = make_plan(hours_since_legionella=30 * 24)
        assert plan.legionella_deadline_slot == 24


class TestSeedsAndMutation:
    def test_pv_led_seed_raises_in_surplus_hours(self):
        plan = make_plan()
        pv = [0.0] * 24
        pv[2:6] = [6000.0] * 4
        seeds = plan.heuristic_genes(pv, [300.0] * 24, [0.0003] * 24)
        heating, dhw = plan.modes(seeds[1])
        assert all(mode == HeatingMode.RAISED for mode in heating[2:6])
        assert all(mode == DhwMode.ROD_60 for mode in dhw[2:6])
        assert heating[10] == HeatingMode.NORMAL

    def test_price_led_seed_follows_prices(self):
        plan = make_plan()
        prices = [0.0001] * 6 + [0.0003] * 12 + [0.0006] * 6
        seeds = plan.heuristic_genes([0.0] * 24, [300.0] * 24, prices)
        heating = [plan.modes(seed)[0] for seed in seeds]
        assert any(h[0] == HeatingMode.RAISED and h[20] == HeatingMode.REDUCED for h in heating)

    def test_mutation_changes_and_stays_valid(self):
        plan = make_plan()
        rng = random.Random(1)
        genes = plan.default_genes()
        for _ in range(500):
            before = list(genes)
            assert plan.mutate(genes, rng)
            assert genes != before
            assert plan.valid(genes)

    def test_shift_drops_elapsed_periods(self):
        plan = make_plan(n_slots=4)
        genes = [0, 1, 2, 1, 3, 2, 1, 0]
        assert plan.shift(genes, 1) == [1, 2, 1, 1, 2, 1, 0, 0]
        assert plan.shift(genes, 0) == genes

    def test_layout_concatenates_heat_pumps(self):
        a, b = make_plan(n_slots=4), make_plan(n_slots=4)
        layout = HeatPumpGeneLayout([a, b])
        assert layout.n_genes == 16
        genes = layout.default_genes()
        load, extra = layout.evaluate(genes)
        assert load is not None and len(load) == 4
        single, _ = HeatPumpGeneLayout([a]).evaluate(a.default_genes())
        assert load == pytest.approx([2 * v for v in single])
        assert not HeatPumpGeneLayout()
        assert HeatPumpGeneLayout().evaluate([]) == (None, 0.0)

    def test_plan_result_maps_actuators(self):
        plan = make_plan(n_slots=4)
        genes = plan.genes_for_modes(
            [HeatingMode.REDUCED, HeatingMode.NORMAL, HeatingMode.RAISED, HeatingMode.NORMAL],
            [DhwMode.HOLD, DhwMode.HEAT_PUMP, DhwMode.ROD_60, DhwMode.ROD_70],
        )
        result = plan_result(plan, genes)
        assert result["sg_ready_state"] == [1, 2, 3, 2]
        assert result["dhw_push"] == [False, True, False, False]
        assert result["rod_target_c"] == [0.0, 0.0, 60.0, 70.0]
        assert result["heating_mode"][0] == "REDUCED"
        assert 4 not in result["sg_ready_state"]
        assert len(result["room_c"]) == 4
        assert result["summary"]["electric_wh"] == pytest.approx(sum(result["electric_wh"]))


# ----------------------------------------------------------------------
# Optimizer integration
# ----------------------------------------------------------------------


def configure(config: ConfigEOS, hours: int = 8, start: str = "2026-11-03T00:00:00") -> None:
    config.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": hours,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 40,
                    "generations": 20,
                    "terminal_value_mode": "FIXED",
                },
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime(start))


def heat_pump(**settings: Any) -> dict[str, Any]:
    device = HeatPumpCommonSettings(device_id="wbb12", **settings)
    return {
        "device_id": "wbb12",
        "settings": device,
        "initial_room_c": 21.0,
        "initial_dhw_c": 45.0,
    }


def parameters(hours: int = 8, prices: list[float] | None = None, **kwargs: Any):
    slots = hours
    return GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * slots,
                "gesamtlast": [400.0] * slots,
                "strompreis_euro_pro_wh": prices or [0.0003] * slots,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * slots,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": [0.0] * slots,
            "pv_battery": None,
            "ev": None,
            **kwargs,
        }
    )


class TestOptimizer:
    def test_without_heat_pump_the_genome_is_unchanged(self, config_eos: ConfigEOS):
        configure(config_eos)
        optimizer = GeneticOptimization(fixed_seed=7)
        solution = optimizer.optimize_ems(parameters(), ngen=2, individuals=12)
        assert optimizer.heat_pump_layout.n_genes == 0
        assert len(solution.start_solution) == 8
        assert solution.heat_pumps == {}
        assert solution.result.heat_pump_wh_per_hour is None

    def test_heat_pump_load_enters_the_energy_balance(self, config_eos: ConfigEOS):
        configure(config_eos)
        optimizer = GeneticOptimization(fixed_seed=7)
        solution = optimizer.optimize_ems(
            parameters(heat_pumps=[heat_pump()]), ngen=2, individuals=12
        )
        assert optimizer.heat_pump_layout.n_genes == 16
        assert len(solution.start_solution) == 8 + 16
        hp = solution.result.heat_pump_wh_per_hour
        assert hp is not None and len(hp) == 8 and min(hp) > 0
        load = np.asarray(solution.result.load_wh_per_hour)
        assert load == pytest.approx(400.0 + np.asarray(hp))
        schedule = solution.heat_pumps["wbb12"]
        assert schedule["electric_wh"] == pytest.approx(hp)
        assert len(schedule["heating_mode"]) == 8

    @pytest.mark.asyncio
    async def test_solution_exports_columns_and_instructions(self, config_eos: ConfigEOS):
        configure(config_eos)
        optimizer = GeneticOptimization(fixed_seed=7)
        solution = optimizer.optimize_ems(
            parameters(heat_pumps=[heat_pump()]), ngen=2, individuals=12
        )
        table = (await solution.optimization_solution()).solution.to_dataframe()
        for column in (
            "wbb12_heating_mode",
            "wbb12_sg_ready_state",
            "wbb12_dhw_push",
            "wbb12_rod_target_c",
            "wbb12_energy_wh",
            "wbb12_room_temperature_c",
            "wbb12_dhw_temperature_c",
        ):
            assert column in table.columns
        assert set(table["wbb12_sg_ready_state"]) <= {1.0, 2.0, 3.0}
        plan = solution.energy_management_plan()
        actuators = {
            instruction.actuator_id
            for instruction in plan.instructions
            if isinstance(instruction, FRBCInstruction)
        }
        assert {"wbb12_space_heating", "wbb12_dhw"} <= actuators
        modes = {
            instruction.operation_mode_id
            for instruction in plan.instructions
            if isinstance(instruction, FRBCInstruction)
            and instruction.actuator_id == "wbb12_space_heating"
        }
        assert modes <= {"REDUCED", "NORMAL", "RAISED", "PREHEAT"}

    def test_joint_optimization_beats_status_quo(self, config_eos: ConfigEOS):
        """Cheap first hours, expensive evening: heat early, coast later."""
        configure(config_eos)
        prices = [0.00010] * 3 + [0.00030] * 2 + [0.00060] * 3
        params = parameters(prices=prices, heat_pumps=[heat_pump()])
        optimizer = GeneticOptimization(fixed_seed=3)
        solution = optimizer.optimize_ems(params, ngen=25, individuals=40)

        best = list(solution.start_solution)
        status_quo = list(best)
        optimizer._set_heat_pump_genes(status_quo, optimizer.heat_pump_layout.default_genes())
        best_fitness = optimizer.evaluate(creator.Individual(best), params, 0, False)[0]
        status_quo_fitness = optimizer.evaluate(creator.Individual(status_quo), params, 0, False)[0]
        assert best_fitness < status_quo_fitness

        hp = np.asarray(solution.result.heat_pump_wh_per_hour)
        cheap, expensive = hp[:3].mean(), hp[5:].mean()
        assert cheap > expensive

    def test_warm_start_with_heat_pump_genes(self, config_eos: ConfigEOS):
        configure(config_eos)
        params = parameters(heat_pumps=[heat_pump()])
        first = GeneticOptimization(fixed_seed=7).optimize_ems(params, ngen=2, individuals=12)
        warm = params.model_copy(update={"start_solution": first.start_solution})
        optimizer = GeneticOptimization(fixed_seed=7)
        optimizer.optimize_ems(warm, ngen=1, individuals=12)
        assert optimizer.heat_pump_layout.valid(
            optimizer._heat_pump_genes(list(first.start_solution))
        )
        # Out-of-range heat pump genes invalidate a warm start.
        broken = list(first.start_solution)
        broken[8] = 99
        assert not optimizer.heat_pump_layout.valid(optimizer._heat_pump_genes(broken))

    def test_load_forecast_with_heat_pump_is_corrected(self, config_eos: ConfigEOS):
        configure(config_eos)
        hp = heat_pump(optimization={"load_forecast_includes_heat_pump": True})
        params = parameters(heat_pumps=[hp])
        params.ems.total_load = [3000.0] * 8
        optimizer = GeneticOptimization(fixed_seed=7)
        optimizer.optimize_ems(params, ngen=1, individuals=12)
        corrected = np.asarray(optimizer.simulation.load_energy_array)
        assert corrected.max() < 3000.0
        assert corrected.min() >= 0.0

    def test_parameters_roundtrip(self):
        params = parameters(heat_pumps=[heat_pump()])
        again = GeneticOptimizationParameters.model_validate(params.model_dump())
        assert again.heat_pumps is not None
        assert again.heat_pumps[0].settings.comfort.setpoint_c == 21.0

    def test_duplicate_heat_pump_ids_rejected(self):
        with pytest.raises(ValueError):
            parameters(heat_pumps=[heat_pump(), heat_pump()])


# ----------------------------------------------------------------------
# Configuration-owned request
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_config_request_resolves_heat_pump_state(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    configure(config_eos)
    config_eos.merge_settings_from_dict(
        {
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "max_heat_pumps": 1,
                "heat_pumps": {"wbb12": {"dhw": {"rod_power_w": 3000}}},
            }
        }
    )
    device = config_eos.devices.heat_pumps["wbb12"]
    assert device.measurement_key_room_temperature in config_eos.devices.measurement_keys
    now = get_ems().start_datetime
    measurement = isolated_measurement
    await measurement.update_value(now.subtract(minutes=5), "wbb12-room-temperature", 20.6)
    await measurement.update_value(now.subtract(minutes=5), "wbb12-dhw-temperature", 47.0)
    await measurement.update_value(now.subtract(minutes=5), "wbb12-dhw-setpoint", 48.0)
    await measurement.update_value(now.subtract(days=3), "wbb12-dhw-temperature", 61.0)
    prepared = await ConfigOptimizationRequest.model_validate(
        {
            "forecasts": {
                "pv_forecast_wh": [0.0] * 48,
                "total_load": [300.0] * 48,
                "electricity_price_per_wh": [0.0003] * 48,
                "feed_in_tariff_per_wh": [0.00007] * 48,
                "temperature_forecast": [3.0] * 48,
                "ghi_forecast": [0.0] * 48,
            }
        }
    ).resolve()
    assert prepared.heat_pumps is not None
    state = prepared.heat_pumps[0]
    assert state.initial_room_c == pytest.approx(20.6)
    assert state.initial_dhw_c == pytest.approx(47.0)
    assert state.dhw_setpoint_c == pytest.approx(48.0)
    assert state.initial_slab_c is None
    assert state.hours_since_legionella == pytest.approx(72.0, abs=0.1)
    assert prepared.ghi_forecast == [0.0] * 48

    optimizer = GeneticOptimization(fixed_seed=1)
    solution = optimizer.optimize_ems(prepared, ngen=1, individuals=12)
    assert "wbb12" in solution.heat_pumps
    assert optimizer.heat_pump_layout.plans[0].system.dhw.setpoint_c == 48.0


@pytest.mark.asyncio
async def test_config_request_without_heat_pump_adds_nothing(config_eos: ConfigEOS):
    configure(config_eos)
    config_eos.merge_settings_from_dict(
        {
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "heat_pumps": {},
            }
        }
    )
    prepared = await ConfigOptimizationRequest.model_validate(
        {
            "forecasts": {
                "pv_forecast_wh": [0.0] * 48,
                "total_load": [300.0] * 48,
                "electricity_price_per_wh": [0.0003] * 48,
                "feed_in_tariff_per_wh": [0.00007] * 48,
            }
        }
    ).resolve()
    assert prepared.heat_pumps is None
    assert prepared.ghi_forecast is None
