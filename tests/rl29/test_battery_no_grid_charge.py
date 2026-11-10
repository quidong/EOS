"""Fork: ``max_ac_charge_power_w = 0`` disables grid charging in the plan as well.

The simulation ignores AC charge genes when the inverter cannot charge from AC
(``Inverter.ac_charge_factor``). The battery operation modes of the exported
solution must say the same, otherwise the controller would execute a grid
charge the optimizer never planned.
"""

from collections.abc import Callable
from typing import Any, Optional

import pytest

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
from akkudoktoreos.utils.datetimeutil import to_datetime

GRID_CHARGE_MODES = ("forced_charge", "grid_support_import")


@pytest.fixture(autouse=True)
def local_clock(set_other_timezone: Callable[[str], str]) -> None:
    set_other_timezone("Europe/Berlin")


def configure(config: ConfigEOS) -> None:
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
                },
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-11-10T00:00:00"))


def parameters(max_ac_charge_power_w: Optional[float]) -> GeneticOptimizationParameters:
    slots = 16
    inverter: dict[str, Any] = {"device_id": "inv", "max_power_wh": 10000, "battery_id": "byd"}
    if max_ac_charge_power_w is not None:
        inverter["max_ac_charge_power_w"] = max_ac_charge_power_w
    return GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * slots,
                "gesamtlast": [200.0] * slots,
                # cheap night, expensive morning: grid charging would pay off
                "strompreis_euro_pro_wh": [0.0002] * 8 + [0.0008] * 8,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * slots,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "pv_battery": {
                "device_id": "byd",
                "capacity_wh": 11040,
                "initial_soc_percentage": 10,
                "charging_efficiency": 0.96,
                "discharging_efficiency": 0.96,
                "min_soc_percentage": 5,
                "max_charge_power_w": 10200,
            },
            "inverter": inverter,
            "forecast_interval_seconds": 900,
            "home_appliances": None,
            "ev": None,
        }
    )


async def planned_modes(max_ac_charge_power_w: Optional[float]) -> dict[str, float]:
    """Sum of every battery op-mode column with all AC charge genes set to full."""
    optimizer = GeneticOptimization(fixed_seed=42)
    solution = optimizer.optimize_ems(parameters(max_ac_charge_power_w), ngen=1, individuals=12)
    # Force the optimizer's intent to "charge from the grid" in every slot.
    solution.ac_charge = [1.0] * len(solution.ac_charge)
    solution.discharge_allowed = [0] * len(solution.discharge_allowed)
    table = (await solution.optimization_solution()).solution.to_dataframe()
    return {
        column: float(table[column].sum())
        for column in table.columns
        if column.startswith("byd_") and column.endswith("_op_mode")
    }


@pytest.mark.asyncio
async def test_no_grid_charge_modes_when_ac_charging_is_disabled(config_eos: ConfigEOS) -> None:
    configure(config_eos)
    modes = await planned_modes(0.0)
    for mode in GRID_CHARGE_MODES:
        assert modes.get(f"byd_{mode}_op_mode", 0.0) == 0.0, modes
    # The slots fall back to holding (PV charge allowed) or idle, never to discharge.
    held = modes.get("byd_non_export_op_mode", 0.0) + modes.get("byd_idle_op_mode", 0.0)
    assert held > 0.0, modes


@pytest.mark.asyncio
async def test_grid_charge_modes_stay_when_ac_charging_is_allowed(config_eos: ConfigEOS) -> None:
    configure(config_eos)
    modes = await planned_modes(None)
    assert sum(modes.get(f"byd_{mode}_op_mode", 0.0) for mode in GRID_CHARGE_MODES) > 0.0, modes
