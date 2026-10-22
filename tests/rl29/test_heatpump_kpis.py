"""Plan-vs-actual and calibration figures as solution columns (RL29 fork, B5).

The figures are empty until enough data exists and fill in by themselves; the
Home Assistant adapter publishes empty figures as ``unknown``.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.devices.heatpumpcalibration import HeatPumpCalibrationStore
from akkudoktoreos.devices.heatpumpkpis import KPI_COLUMNS, MIN_SLOTS, device_kpis
from akkudoktoreos.devices.heatpumpplanactual import PlanActualStore
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.calibration import (
    BuildingFit,
    CalibrationResult,
    CopFit,
    DrawFit,
)
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
from akkudoktoreos.utils.datetimeutil import to_datetime

NOW = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)


def evaluation(slots: int) -> dict[str, Any]:
    lead = {
        "room": {"slots": slots, "bias_k": 0.3, "mae_k": 0.4},
        "dhw": {"slots": slots, "bias_k": -1.2, "mae_k": 1.5},
        "energy": {"slots": slots, "planned_wh": 1000, "measured_wh": 900, "ratio": 0.9},
    }
    return {"evaluated_at": NOW.isoformat(), "leads": {"1h": lead, "6h": lead}}


def write_stores(folder: Path, slots: int, result: CalibrationResult | None) -> None:
    plans = PlanActualStore(folder / "rl29" / "heatpump_plan_actual.json")
    plans.set_evaluation("wbb12", evaluation(slots))
    plans.save()
    if result is not None:
        store = HeatPumpCalibrationStore(folder / "rl29" / "heatpump_calibration.json")
        store.set_result("wbb12", result, "summary", "priors")
        store.save()


def calibration() -> CalibrationResult:
    return CalibrationResult(
        fitted_at=NOW,
        building=BuildingFit(
            loss_w_per_k=175.0, solar_aperture_m2=3.5, internal_gains_w=250.0, days=20
        ),
        cop=CopFit(cop_scale=0.92, hours=80, measured_cop=3.8, model_cop=4.1, raw_scale=0.9),
        draw=DrawFit(profile_wh=[100.0] * 24, weekend_wh=None, days=16, daily_wh=3100.0),
    )


def test_nothing_known_gives_empty_figures(tmp_path: Path):
    kpis = device_kpis("wbb12", tmp_path)
    assert set(kpis) == {name for name, _ in KPI_COLUMNS} | {"kpi_calibrated"}
    assert all(v is None for k, v in kpis.items() if k != "kpi_calibrated")
    assert kpis["kpi_calibrated"] == 0.0


def test_plan_actual_figures_need_enough_slots(tmp_path: Path):
    write_stores(tmp_path, MIN_SLOTS - 1, None)
    assert device_kpis("wbb12", tmp_path)["kpi_room_bias_1h_k"] is None
    write_stores(tmp_path, MIN_SLOTS, None)
    kpis = device_kpis("wbb12", tmp_path)
    assert kpis["kpi_room_bias_1h_k"] == 0.3
    assert kpis["kpi_room_mae_6h_k"] == 0.4
    assert kpis["kpi_dhw_bias_6h_k"] == -1.2
    assert kpis["kpi_energy_ratio_6h"] == 0.9


def test_calibration_figures_follow_the_fits(tmp_path: Path):
    write_stores(tmp_path, 0, calibration())
    kpis = device_kpis("wbb12", tmp_path)
    assert kpis["kpi_calibrated"] == 1.0
    assert kpis["kpi_loss_w_per_k"] == 175.0
    assert kpis["kpi_building_days"] == 20
    assert kpis["kpi_cop_scale"] == 0.92 and kpis["kpi_measured_cop"] == 3.8
    assert kpis["kpi_draw_kwh_per_day"] == pytest.approx(3.1)
    assert kpis["kpi_emitter_w_per_k"] is None and kpis["kpi_dhw_loss_w_per_k"] is None


def params(enabled: bool) -> GeneticOptimizationParameters:
    slots = 8
    device = HeatPumpCommonSettings(device_id="wbb12", calibration={"enabled": enabled})
    return GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * slots,
                "gesamtlast": [400.0] * slots,
                "strompreis_euro_pro_wh": [0.0003] * slots,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * slots,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": [0.0] * slots,
            "pv_battery": None,
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "wbb12",
                    "settings": device,
                    "initial_room_c": 21.0,
                    "initial_dhw_c": 45.0,
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_solution_carries_the_figures_of_a_calibrated_heat_pump(
    config_eos: ConfigEOS, tmp_path: Path
):
    config_eos.merge_settings_from_dict(
        {
            "general": {"data_folder_path": str(tmp_path)},
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 8,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 12,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                },
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-11-03T00:00:00"))
    write_stores(tmp_path, MIN_SLOTS, calibration())

    plain = GeneticOptimization(fixed_seed=7).optimize_ems(params(False), ngen=2, individuals=12)
    table = (await plain.optimization_solution()).solution.to_dataframe()
    assert not [c for c in table.columns if "_kpi_" in c]

    solution = GeneticOptimization(fixed_seed=7).optimize_ems(params(True), ngen=2, individuals=12)
    table = (await solution.optimization_solution()).solution.to_dataframe()
    assert set(table["wbb12_kpi_loss_w_per_k"]) == {175.0}
    assert set(table["wbb12_kpi_room_bias_6h_k"]) == {0.3}
    assert table["wbb12_kpi_calibrated"].iloc[0] == 1.0
    assert table["wbb12_kpi_emitter_w_per_k"].isna().all()  # no emitter fit yet


@pytest.mark.asyncio
async def test_adapter_publishes_missing_figures_as_unknown(config_eos: ConfigEOS):
    from akkudoktoreos.adapter.homeassistant import HomeAssistantAdapter
    from akkudoktoreos.core.ems import EnergyManagementStage

    config_eos.merge_settings_from_dict(
        {
            "adapter": {
                "provider": ["HomeAssistant"],
                "homeassistant": {
                    "solution_entity_ids": [
                        "sensor.eos_wbb12_kpi_cop_scale",
                        "sensor.eos_wbb12_kpi_emitter_w_per_k",
                    ]
                },
            }
        }
    )
    index = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=2, freq="h")
    frame = pd.DataFrame(
        {"wbb12_kpi_cop_scale": [0.9, 0.9], "wbb12_kpi_emitter_w_per_k": [math.nan] * 2},
        index=index,
    )
    ems = MagicMock()
    ems.stage.return_value = EnergyManagementStage.CONTROL_DISPATCH
    ems.optimization_solution.return_value.solution.to_dataframe.return_value = frame
    ems.plan.return_value.get_active_instructions.return_value = []
    adapter = HomeAssistantAdapter()
    object.__setattr__(adapter, "ems", ems)
    posted: dict[str, Any] = {}
    with patch.object(
        HomeAssistantAdapter,
        "set_entity_state",
        autospec=True,
        side_effect=lambda self, entity_id, value, *a: posted.__setitem__(entity_id, value),
    ):
        await adapter._update_data()
    assert posted["sensor.eos_wbb12_kpi_cop_scale"] == 0.9
    assert posted["sensor.eos_wbb12_kpi_emitter_w_per_k"] == "unknown"
