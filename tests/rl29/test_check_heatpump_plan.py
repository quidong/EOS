"""Plausibility check of the heat pump plan (scripts/rl29/check_heatpump_plan.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rl29" / "check_heatpump_plan.py"
spec = importlib.util.spec_from_file_location("check_heatpump_plan", SCRIPT)
assert spec and spec.loader
script = importlib.util.module_from_spec(spec)
sys.modules["check_heatpump_plan"] = script
spec.loader.exec_module(script)

TZ = timezone(timedelta(hours=2))
START = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
NOW = START + timedelta(minutes=5)

CONFIG = {
    "devices": {
        "heat_pumps": {
            "wbb12": {
                "unit": {"forced_overshoot_k": 1.0},
                "comfort": {
                    "max_c": 22.0,
                    "min_c": {"windows": [{"value": 19.5}, {"value": 20.5}]},
                },
            }
        }
    }
}


def solution(
    modes: list[int],
    rooms: list[float] | None = None,
    pv: list[float] | None = None,
    prices: list[float] | None = None,
    dhw: list[int] | None = None,
    generated: datetime = START,
) -> dict[str, Any]:
    n = len(modes)
    rooms = rooms or [21.0] * n
    pv = pv or [0.0] * n
    prices = prices or [0.30] * n
    dhw = dhw or [0] * n
    data, pred = {}, {}
    for i in range(n):
        t = (START + timedelta(hours=i)).isoformat()
        data[t] = {
            "wbb12_heating_mode": float(modes[i]),
            "wbb12_dhw_mode": float(dhw[i]),
            "wbb12_room_temperature_c": rooms[i],
            "load_energy_wh": 800.0,
            "grid_feedin_energy_wh": max(0.0, pv[i] - 800.0),
        }
        pred[t] = {"elec_price_amt_kwh": prices[i], "pvforecast_ac_energy_wh": pv[i]}
    return {
        "generated_at": generated.isoformat(),
        "solution": {"data": data},
        "prediction": {"data": pred},
    }


def run(sol: dict[str, Any], **kwargs: Any):
    return script.check(sol, CONFIG, "wbb12", NOW, **kwargs)


def test_normal_plan_is_plausible():
    report = run(solution([1] * 24))
    assert report.exit_code == 0, report.lines()


def test_pv_led_raise_is_plausible():
    modes = [1] * 2 + [2] * 4 + [1] * 18
    pv = [0.0] * 2 + [3500.0] * 4 + [0.0] * 18
    rooms = [21.0] * 2 + [21.5, 21.8, 22.0, 22.0] + [21.5] * 18
    report = run(solution(modes, rooms=rooms, pv=pv))
    assert report.exit_code == 0, report.lines()


def test_first_live_plan_of_22_october_is_implausible():
    """FORCED 10-20 h with rooms up to 24 degC."""
    modes = [4] * 11 + [1] * 13
    rooms = [21.1, 21.4, 21.9, 22.4, 22.9, 23.5, 23.9, 24.1, 24.1, 24.0, 23.9] + [23.0] * 13
    report = run(solution(modes, rooms=rooms))
    assert report.exit_code == 2
    text = "\n".join(report.lines())
    assert "24.1 °C > 23.5 °C" in text
    assert "11 h am Stück aktiv" in text


def test_stale_plan_fails():
    report = script.check(
        solution([1] * 24), CONFIG, "wbb12", NOW + timedelta(hours=1), max_age_min=45
    )
    assert report.exit_code == 2
    assert any("alt" in m for m in report.failures)


def test_cold_plan_fails():
    report = run(solution([0] * 24, rooms=[18.0] * 24))
    assert report.exit_code == 2


def test_raise_without_surplus_at_peak_price_warns():
    prices = [0.25] * 18 + [0.48] * 6
    modes = [1] * 18 + [2] + [1] * 5
    report = run(solution(modes, prices=prices))
    assert report.exit_code == 1
    assert any("ohne PV-Überschuss" in m for m in report.warnings)


def test_long_reduction_and_many_hot_water_charges_warn():
    report = run(solution([0] * 5 + [1] * 19, dhw=[1] * 5 + [0] * 19))
    assert report.exit_code == 1
    assert len(report.warnings) == 2


def test_missing_device_fails():
    sol = solution([1] * 3)
    for row in sol["solution"]["data"].values():
        del row["wbb12_heating_mode"]
    assert run(sol).exit_code == 2


def test_cli_with_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    sol_file = tmp_path / "solution.json"
    cfg_file = tmp_path / "config.json"
    sol_file.write_text(json.dumps(solution([1] * 24)))
    cfg_file.write_text(json.dumps(CONFIG))
    code = script.main(
        ["--solution", str(sol_file), "--config", str(cfg_file), "--now", NOW.isoformat()]
    )
    assert code == 0
    assert "Status: PLAUSIBEL" in capsys.readouterr().out


def test_unreachable_eos_fails(capsys: pytest.CaptureFixture[str]):
    code = script.main(["--url", "http://127.0.0.1:9"])
    assert code == 2
    assert "Kein Plan abrufbar" in capsys.readouterr().out
