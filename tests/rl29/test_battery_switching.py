"""Battery mode changes: minimum saving per change and hold time (RL29 fork).

``optimization.genetic.battery_change_penalty_amt`` charges every change of the
battery operating mode, ``battery_min_hold_minutes`` charges modes that last
shorter than the hold time. The modes are what the controller switches: grid
charge, discharge allowed, PV charge allowed, export.
"""

from collections.abc import Callable

import pytest

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
from akkudoktoreos.utils.datetimeutil import to_datetime

SLOTS = 16


@pytest.fixture(autouse=True)
def local_clock(set_other_timezone: Callable[[str], str]) -> None:
    set_other_timezone("Europe/Berlin")


def configure(config: ConfigEOS, **genetic) -> None:
    config.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 4,
                    "tail_horizon_hours": 0,
                    "interval_sec": 900,
                    "individuals": 12,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                    **genetic,
                },
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-11-10T00:00:00"))


def parameters(prices=None) -> GeneticOptimizationParameters:
    return GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * SLOTS,
                "gesamtlast": [200.0] * SLOTS,
                "strompreis_euro_pro_wh": prices or [0.0003] * SLOTS,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * SLOTS,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "pv_battery": {
                "device_id": "byd",
                "capacity_wh": 11040,
                "initial_soc_percentage": 50,
                "min_soc_percentage": 5,
            },
            "inverter": {
                "device_id": "inv",
                "max_power_wh": 10000,
                "battery_id": "byd",
                "max_ac_charge_power_w": 0,
            },
            "forecast_interval_seconds": 900,
            "home_appliances": None,
            "ev": None,
        }
    )


def prepared(config: ConfigEOS, **genetic) -> GeneticOptimization:
    configure(config, **genetic)
    optimizer = GeneticOptimization(fixed_seed=7)
    optimizer.optimize_ems(parameters(), ngen=1, individuals=12)
    return optimizer


def genes(optimizer: GeneticOptimization, pattern: str) -> list[int]:
    """``i`` = idle, ``d`` = discharge allowed, one letter per slot."""
    idle = 0
    discharge = len(optimizer.bat_possible_charge_values)
    states = [discharge if c == "d" else idle for c in pattern]
    individual = optimizer.toolbox.individual()
    individual[: len(states)] = states
    return individual


@pytest.mark.asyncio
async def test_without_settings_switching_is_free(config_eos: ConfigEOS) -> None:
    optimizer = prepared(config_eos)
    assert optimizer._battery_switch_penalty(genes(optimizer, "didididididididi")) == 0.0


@pytest.mark.asyncio
async def test_every_change_costs_the_minimum_saving(config_eos: ConfigEOS) -> None:
    optimizer = prepared(config_eos, battery_change_penalty_amt=0.015)
    steady = genes(optimizer, "dddddddddddddddd")
    two = genes(optimizer, "ddddiiiiiiiidddd")
    assert optimizer._battery_switch_penalty(steady) == pytest.approx(0.0)
    assert optimizer._battery_switch_penalty(two) == pytest.approx(0.03)
    signature = optimizer.battery_mode_signature(two)
    assert len(signature) == optimizer.control_end_slot
    assert signature[0] != signature[4]


@pytest.mark.asyncio
async def test_a_mode_shorter_than_the_hold_time_costs_extra(config_eos: ConfigEOS) -> None:
    optimizer = prepared(
        config_eos,
        battery_change_penalty_amt=0.0,
        battery_min_hold_minutes=45,
        battery_hold_penalty_amt=0.5,
    )
    # idle for 30 min (2 slots) between two discharge blocks: one short mode
    short = genes(optimizer, "ddddiidddddddddd")
    # idle for 45 min (3 slots): long enough
    held = genes(optimizer, "ddddiiiddddddddd")
    assert optimizer._battery_switch_penalty(short) == pytest.approx(0.5)
    assert optimizer._battery_switch_penalty(held) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_the_penalty_enters_the_fitness(config_eos: ConfigEOS) -> None:
    optimizer = prepared(config_eos, battery_change_penalty_amt=0.2)
    params = parameters()
    steady = genes(optimizer, "dddddddddddddddd")
    flicker = genes(optimizer, "dididididddddddd")
    base = optimizer._evaluate_uncached(steady, params, 0, False)[0]
    noisy = optimizer._evaluate_uncached(flicker, params, 0, False)[0]
    assert noisy - base > 8 * 0.2 - 0.5  # 8 changes; the energy difference is small
