"""Plan versus actual of the heat pump (RL29 fork).

Plans are recorded per optimization run and compared with the measurements of
the last day for two lead times; the result lands in the JSON store and the
log. The optimizer records the plan of a calibrated heat pump on its own.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.heatpumpplanactual import (
    KEEP_HOURS,
    RECORD_HOURS,
    PlanActualStore,
    evaluate_device,
    evaluate_plans,
    summary,
)
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
from akkudoktoreos.utils.datetimeutil import to_datetime

NOW = pendulum.datetime(2026, 11, 20, 9, 7, tz="Europe/Berlin")
DEVICE = HeatPumpCommonSettings(device_id="wbb12", calibration={"enabled": True})


def plan_result(hours: int = 48, room: float = 21.0, energy: float = 500.0) -> dict[str, Any]:
    return {
        "room_c": [room] * hours,
        "slab_c": [24.0] * hours,
        "dhw_c": [44.0] * hours,
        "electric_wh": [energy] * hours,
        "heating_mode": ["NORMAL"] * hours,
        "dhw_mode": ["HOLD"] * hours,
        "summary": {"electric_wh": energy * hours},
    }


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------


def test_record_keeps_the_first_day_and_drops_old_plans(tmp_path: Path):
    store = PlanActualStore(tmp_path / "plans.json")
    issued = NOW.subtract(hours=KEEP_HOURS + 2)
    starts = [issued.start_of("hour").add(hours=i) for i in range(48)]
    store.record("wbb12", issued, starts, 1.0, plan_result())
    recent = NOW.subtract(minutes=20)
    store.record(
        "wbb12",
        recent,
        [recent.start_of("hour").add(hours=i) for i in range(48)],
        1.0,
        plan_result(),
    )
    plans = store.plans("wbb12")
    assert len(plans) == 1  # the old one is gone
    assert len(plans[0]["rows"]["room_c"]) == RECORD_HOURS
    assert "summary" not in plans[0]["rows"]
    # the same run recorded twice replaces itself
    store.record("wbb12", recent, [recent.start_of("hour")], 1.0, plan_result(1))
    assert len(store.plans("wbb12")) == 1
    store.save()
    assert PlanActualStore(tmp_path / "plans.json").plans("wbb12") == store.plans("wbb12")


# ----------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluation_reports_bias_mae_and_energy_per_lead(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    config_eos.merge_settings_from_dict(
        {
            "devices": {
                "max_heat_pumps": 1,
                "heat_pumps": {"wbb12": {"calibration": {"enabled": True}}},
            }
        }
    )
    store = PlanActualStore(tmp_path / "plans.json")
    # Plans issued every hour over the last 30 h: the fresh plan says 21.0 degC,
    # the plan from 6 h earlier said 21.5 degC; energy 500 Wh/h in both.
    for h in range(30, 0, -1):
        issued = NOW.subtract(hours=h).start_of("hour").add(minutes=5)
        starts = [issued.start_of("hour").add(hours=i) for i in range(48)]
        store.record("wbb12", issued, starts, 1.0, plan_result(room=21.0, energy=500.0))
    # the plans are identical, so differentiate by patching the stored rows
    for plan in store._device("wbb12")["plans"]:
        plan["rows"]["room_c"] = [21.0 + 0.5 * (i >= 6) for i in range(RECORD_HOURS)]
    # measurements: room 20.8 flat, tank 44.0, heat pump 400 W flat
    t = NOW.subtract(hours=26)
    while t <= NOW:
        await isolated_measurement.update_value(t, "wbb12-room-temperature", 20.8)
        await isolated_measurement.update_value(t, "wbb12-dhw-temperature", 44.0)
        await isolated_measurement.update_value(t, "wbb12-electric-power", 400.0)
        t = t.add(minutes=15)
    device = config_eos.devices.heat_pumps["wbb12"]
    evaluation = await evaluate_plans(device, store, isolated_measurement, NOW)
    assert evaluation is not None
    one = evaluation["leads"]["1h"]
    six = evaluation["leads"]["6h"]
    assert one["room"]["slots"] >= 20 and six["room"]["slots"] >= 20
    # lead 1 h uses index 0..1 of a fresh plan (21.0), lead 6 h index >= 6 (21.5)
    assert one["room"]["bias_k"] == pytest.approx(0.2, abs=0.05)
    assert six["room"]["bias_k"] == pytest.approx(0.7, abs=0.05)
    assert one["room"]["mae_k"] == pytest.approx(0.2, abs=0.05)
    assert one["dhw"]["bias_k"] == pytest.approx(0.0, abs=0.01)
    assert one["energy"]["ratio"] == pytest.approx(0.8, abs=0.02)
    assert one["energy"]["planned_wh"] == 500 * one["energy"]["slots"]
    text = summary(evaluation)
    assert "lead 1h: room bias +0.2" in text and "lead 6h: room bias +0.7" in text
    assert "x0.80" in text
    assert store.evaluation("wbb12") == evaluation


@pytest.mark.asyncio
async def test_evaluate_device_runs_hourly_and_persists(
    config_eos: ConfigEOS, isolated_measurement: Measurement, tmp_path: Path
):
    config_eos.merge_settings_from_dict(
        {
            "devices": {
                "max_heat_pumps": 1,
                "heat_pumps": {"wbb12": {"calibration": {"enabled": True}}},
            }
        }
    )
    device = config_eos.devices.heat_pumps["wbb12"]
    # nothing recorded: nothing to evaluate, no file
    assert await evaluate_device(device, isolated_measurement, NOW, tmp_path) is None
    path = tmp_path / "rl29" / "heatpump_plan_actual.json"
    store = PlanActualStore(path)
    issued = NOW.subtract(hours=3)
    store.record(
        "wbb12",
        issued,
        [issued.start_of("hour").add(hours=i) for i in range(24)],
        1.0,
        plan_result(24),
    )
    store.save()
    await isolated_measurement.update_value(NOW.subtract(hours=2), "wbb12-room-temperature", 21.3)
    await isolated_measurement.update_value(NOW.subtract(hours=1), "wbb12-electric-power", 300.0)
    first = await evaluate_device(device, isolated_measurement, NOW, tmp_path)
    assert first is not None and first["leads"]["1h"]["room"]["slots"] >= 1
    assert json.loads(path.read_text())["devices"]["wbb12"]["evaluation"] == first
    # within the hour the stored evaluation is returned, afterwards a new one
    again = await evaluate_device(device, isolated_measurement, NOW.add(minutes=20), tmp_path)
    assert again == first
    later = await evaluate_device(device, isolated_measurement, NOW.add(hours=2), tmp_path)
    assert later is not None and later["evaluated_at"] != first["evaluated_at"]


# ----------------------------------------------------------------------
# Hook in the optimizer
# ----------------------------------------------------------------------


def test_optimizer_records_the_plan_of_a_calibrated_heat_pump(
    config_eos: ConfigEOS, tmp_path: Path
):
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": "Europe/Berlin", "data_folder_path": str(tmp_path)},
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
    get_ems(init=True).set_start_datetime(to_datetime("2026-11-03T00:00:00+01:00"))
    slots = 8

    def params(enabled: bool):
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

    path = tmp_path / "rl29" / "heatpump_plan_actual.json"
    GeneticOptimization(fixed_seed=7).optimize_ems(params(False), ngen=2, individuals=12)
    assert not path.exists()  # not calibrated: nothing recorded
    solution = GeneticOptimization(fixed_seed=7).optimize_ems(params(True), ngen=2, individuals=12)
    plans = PlanActualStore(path).plans("wbb12")
    assert len(plans) == 1
    rows = plans[0]["rows"]
    assert rows["electric_wh"] == pytest.approx(solution.heat_pumps["wbb12"]["electric_wh"])
    assert rows["heating_mode"] == solution.heat_pumps["wbb12"]["heating_mode"]
    assert len(rows["room_c"]) == slots
    assert plans[0]["start"].startswith("2026-11-02T23:00:00+00:00")
