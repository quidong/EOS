"""PDF report pages of the fork (B14): heat pump, plan vs actual, calibration, learned profiles."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pendulum
import pytest
from pypdf import PdfReader

from akkudoktoreos.optimization.genetic import geneticvisualize as visualize


def solution_with_heat_pump(start):
    energy = [100.0, 150.0, 200.0, 250.0]
    result = SimpleNamespace(
        load_wh_per_hour=energy,
        home_appliance_wh_per_hour=energy,
        grid_feed_in_wh_per_hour=energy,
        grid_consumption_wh_per_hour=energy,
        losses_per_hour=[1.0] * 4,
        battery_soc_per_hour=[20.0, 25.0, 30.0, 35.0],
        ev_soc_per_hour=[0.0] * 4,
        costs_per_hour=[0.03] * 4,
        revenue_per_hour=[0.01] * 4,
        total_costs=0.12,
        total_revenue=0.04,
        total_balance=0.08,
        home_appliance_energy_wh={},
    )
    return SimpleNamespace(
        parameters=SimpleNamespace(
            ems=SimpleNamespace(
                total_load=energy,
                pv_forecast_wh=energy,
                feed_in_tariff_per_wh=[0.00007] * 4,
                electricity_price_per_wh=[0.0003] * 4,
            ),
            temperature_forecast=[12.0] * 4,
        ),
        interval_seconds=3600,
        start_solution_datetime=start,
        controls_start_at_now=True,
        start_hour=1,
        ac_charge=[0.0] * 4,
        dc_charge=[1.0] * 4,
        discharge_allowed=[0] * 4,
        battery_grid_export_allowed=[0] * 4,
        battery_grid_export_factor=[0.0] * 4,
        result=result,
        extra_data=None,
        fitness_history=None,
        fixed_seed=None,
        appliance_starts={},
        appliance_deadline_missed={},
        terminal_value=None,
        heat_pumps={
            "wbb12": {
                "heating_mode": ["NORMAL", "NORMAL", "RAISED", "NORMAL"],
                "dhw_mode": ["HOLD", "HEAT_PUMP", "HOLD", "HOLD"],
                "rod_target_c": [0.0, 0.0, 0.0, 70.0],
                "electric_wh": [700.0, 1500.0, 2200.0, 3700.0],
                "rod_electric_wh": [0.0, 0.0, 0.0, 3000.0],
                "room_c": [21.0, 21.0, 21.4, 21.6],
                "slab_c": [24.0, 24.2, 25.1, 25.3],
                "dhw_c": [44.0, 45.0, 44.5, 60.0],
                "cop_space": [4.2, 4.1, 3.9, 3.8],
                "comfort_min_c": [20.5] * 4,
                "comfort_max_c": [22.0] * 4,
                "outdoor_c": [8.0, 7.5, 7.0, 6.5],
                "summary": {"extra_cost_amt": 0.1234, "mode_changes": 3},
            }
        },
    )


def write_stores(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "heatpump_plan_actual.json").write_text(
        json.dumps(
            {
                "devices": {
                    "wbb12": {
                        "plans": [],
                        "evaluation": {
                            "evaluated_at": "2026-10-25T09:00:00+00:00",
                            "hours": 24,
                            "leads": {
                                "1h": {
                                    "room": {"slots": 23, "bias_k": 0.15, "mae_k": 0.21},
                                    "dhw": {"slots": 23, "bias_k": -0.4, "mae_k": 0.9},
                                    "energy": {
                                        "slots": 23,
                                        "planned_wh": 7300,
                                        "measured_wh": 6100,
                                        "ratio": 0.836,
                                    },
                                }
                            },
                        },
                    }
                }
            }
        )
    )
    (folder / "heatpump_calibration.json").write_text(
        json.dumps(
            {
                "devices": {
                    "wbb12": {
                        "result": {
                            "fitted_at": "2026-10-25T08:00:00+00:00",
                            "window_days": 30,
                            "samples": 500,
                        },
                        "summary": "loss 180 -> 172 W/K (12 days)",
                    }
                }
            }
        )
    )
    (folder / "appliance_profiles.json").write_text(
        json.dumps(
            {
                "devices": {
                    "waschmaschine": {
                        "programs": {
                            "3": {
                                "power_w": [4200.0] * 4 + [180.0] * 20,
                                "interval_seconds": 300,
                                "runs": 5,
                                "energy_wh": 2000,
                                "duration_s": 7200,
                            }
                        }
                    }
                }
            }
        )
    )


def test_pdf_has_heat_pump_plan_actual_calibration_and_profile_pages(
    config_eos, tmp_path, monkeypatch
):
    config_eos.merge_settings_from_dict({"general": {"data_folder_path": str(tmp_path)}})
    write_stores(tmp_path / "plus")
    start = pendulum.datetime(2026, 10, 25, 9, 0, tz="Europe/Berlin")
    captured = []
    original = visualize.GeneticVisualizationReport.create_line_chart_date

    def capture(self, start_date, y_list, **kwargs):
        captured.append((kwargs.get("title"), y_list, kwargs.get("labels")))
        return original(self, start_date, y_list, **kwargs)

    monkeypatch.setattr(visualize.GeneticVisualizationReport, "create_line_chart_date", capture)
    pdf = visualize.genetic_prepare_visualize(solution_with_heat_pump(start))
    assert pdf.startswith(b"%PDF-")
    titles = [c[0] for c in captured]
    assert "Heat Pump wbb12: Room and Screed" in titles
    assert "Heat Pump wbb12: Hot Water" in titles
    assert "Heat Pump wbb12: Electric Energy" in titles
    assert "Heat Pump wbb12: COP / EER" in titles
    room = next(c for c in captured if c[0] == "Heat Pump wbb12: Room and Screed")
    assert room[2] == ["room", "screed", "comfort min", "comfort max", "outdoor"]
    assert room[1][0] == [21.0, 21.0, 21.4, 21.6]
    hot = next(c for c in captured if c[0] == "Heat Pump wbb12: Hot Water")
    assert hot[2] == ["tank", "rod target"]
    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    text = " ".join(text.split())  # line wrapping of the text pages
    for expected in (
        "Heat Pump wbb12: Schedule",
        "NORMAL x2, RAISED x1, NORMAL x1",
        "HOLD x1, HEAT_PUMP x1, HOLD x2",
        "Plan vs Actual",
        "room bias +0.15 K",
        "x0.84",
        "Calibration",
        "loss 180 -> 172 W/K",
        "Learned profile waschmaschine program 3",
        "5 runs, 2000 Wh, 120 min",
    ):
        assert expected in text, expected


def test_pdf_without_heat_pump_and_without_stores_is_unchanged(config_eos, tmp_path):
    config_eos.merge_settings_from_dict({"general": {"data_folder_path": str(tmp_path)}})
    start = pendulum.datetime(2026, 10, 25, 9, 0, tz="Europe/Berlin")
    solution = solution_with_heat_pump(start)
    solution.heat_pumps = {}
    pdf = visualize.genetic_prepare_visualize(solution)
    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert "Heat Pump" not in text and "Learned profile" not in text


def test_pdf_survives_a_broken_store(config_eos, tmp_path):
    config_eos.merge_settings_from_dict({"general": {"data_folder_path": str(tmp_path)}})
    (tmp_path / "plus").mkdir()
    (tmp_path / "plus" / "heatpump_calibration.json").write_text("{broken")
    start = pendulum.datetime(2026, 10, 25, 9, 0, tz="Europe/Berlin")
    pdf = visualize.genetic_prepare_visualize(solution_with_heat_pump(start))
    assert pdf.startswith(b"%PDF-")
