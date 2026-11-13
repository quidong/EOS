"""Plausibility check of the battery plan (scripts/rl29/check_battery_plan.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rl29" / "check_battery_plan.py"
spec = importlib.util.spec_from_file_location("check_battery_plan", SCRIPT)
assert spec and spec.loader
script = importlib.util.module_from_spec(spec)
sys.modules["check_battery_plan"] = script
spec.loader.exec_module(script)

TZ = timezone(timedelta(hours=1))
START = datetime(2026, 11, 10, 13, 0, tzinfo=TZ)
NOW = START + timedelta(minutes=5)
STEP = timedelta(minutes=15)

CONFIG: dict[str, Any] = {
    "devices": {
        "batteries": {
            "byd": {
                "device_id": "byd",
                "capacity_wh": 11040,
                "charging_efficiency": 0.96,
                "discharging_efficiency": 0.96,
                "levelized_cost_of_storage_amt_kwh": 0.03,
                "max_charge_power_w": 10200,
                "min_soc_percentage": 5,
                "max_soc_percentage": 100,
                "full_charge_interval_days": 14,
                "full_charge_soc_percentage": 100,
            }
        }
    }
}


def solution(
    modes: list[str],
    socs: Optional[list[float]] = None,
    prices: Optional[list[float]] = None,
    factors: Optional[list[float]] = None,
    feedin: Optional[list[float]] = None,
    generated: datetime = START,
) -> dict[str, Any]:
    n = len(modes)
    socs = socs if socs is not None else [0.5] * n
    prices = prices or [0.60] * n
    factors = factors or [0.0] * n
    feedin = feedin or [0.0] * n
    data, pred = {}, {}
    for i in range(n):
        t = (START + i * STEP).isoformat()
        row: dict[str, Any] = {"byd_soc_factor": socs[i], "grid_feedin_energy_wh": feedin[i]}
        for mode in script.MODES:
            row[f"byd_{mode.lower()}_op_mode"] = 1.0 if mode == modes[i] else 0.0
            row[f"byd_{mode.lower()}_op_factor"] = factors[i] if mode == modes[i] else 0.0
        data[t] = row
        pred[t] = {"elec_price_amt_kwh": prices[i]}
    return {
        "generated_at": generated.isoformat(),
        "solution": {"data": data},
        "prediction": {"data": pred},
    }


def run(sol: dict[str, Any], **kwargs: Any):
    return script.check(sol, CONFIG, "byd", NOW, **kwargs)


def test_self_consumption_plan_is_plausible():
    report = run(solution(["SELF_CONSUMPTION"] * 96), measured_soc_factor=0.5)
    assert report.exit_code == 0, report.lines()
    assert "Betriebsarten: 13:00 Eigenverbrauch" in report.lines()[-1]


def test_live_plan_of_10_november_is_plausible():
    """Discharge in the evening, grid charge 22:45-24:00 at 46 ct, use at 75 ct in the morning."""
    modes = (
        ["SELF_CONSUMPTION"] * 30
        + ["NON_EXPORT"] * 9
        + ["FORCED_CHARGE"] * 5
        + ["NON_EXPORT"] * 25
        + ["SELF_CONSUMPTION"] * 27
    )
    factors = [0.0] * 39 + [0.5, 0.7, 1.0, 1.0, 1.0] + [0.0] * 52
    prices = [0.65] * 30 + [0.50] * 9 + [0.46] * 5 + [0.45] * 25 + [0.75] * 27
    socs = [0.41] * 30 + [0.05] * 9 + [0.05, 0.16, 0.32, 0.54, 0.76] + [0.98] * 52
    report = run(solution(modes, socs=socs, prices=prices, factors=factors))
    assert report.exit_code == 0, report.lines()
    assert any("Netzladen geplant: 10.7 kWh" in line for line in report.lines())


def test_unprofitable_grid_charge_warns():
    modes = ["FORCED_CHARGE"] * 4 + ["SELF_CONSUMPTION"] * 92
    report = run(solution(modes, factors=[1.0] * 4 + [0.0] * 92, prices=[0.60] * 96))
    assert report.exit_code == 1
    assert "Netzladen bei 60.0 ct/kWh lohnt nicht" in report.warnings[0]
    assert len(report.warnings) == 1  # one warning per hour


def test_soc_outside_limits_fails():
    socs = [0.5] * 95 + [0.02]
    report = run(solution(["SELF_CONSUMPTION"] * 96, socs=socs))
    assert report.exit_code == 2
    assert "Ladestand 2.0 % < Minimum 5 %" in report.failures[0]


def test_slot_without_mode_fails():
    sol = solution(["SELF_CONSUMPTION"] * 8)
    first = sorted(sol["solution"]["data"])[0]
    sol["solution"]["data"][first]["byd_self_consumption_op_mode"] = 0.0
    report = run(sol)
    assert report.exit_code == 2
    assert "0 Betriebsarten aktiv (keine)" in report.failures[0]


def test_two_modes_fail():
    sol = solution(["SELF_CONSUMPTION"] * 8)
    first = sorted(sol["solution"]["data"])[0]
    sol["solution"]["data"][first]["byd_forced_charge_op_mode"] = 1.0
    report = run(sol)
    assert report.exit_code == 2
    assert "2 Betriebsarten aktiv" in report.failures[0]


def test_old_plan_fails():
    report = run(solution(["SELF_CONSUMPTION"] * 8, generated=START - timedelta(hours=2)))
    assert report.exit_code == 2
    assert "min alt" in report.failures[0]


def test_missing_battery_columns_fail():
    sol = solution(["SELF_CONSUMPTION"] * 8)
    for row in sol["solution"]["data"].values():
        for key in [k for k in row if k.startswith("byd_")]:
            del row[key]
    report = run(sol)
    assert report.exit_code == 2
    assert "keine Spalten" in report.failures[0]


def test_battery_not_configured_fails():
    report = script.check(solution(["SELF_CONSUMPTION"] * 4), {"devices": {}}, "byd", NOW)
    assert report.exit_code == 2


def test_export_warns():
    modes = ["GRID_SUPPORT_EXPORT"] * 4 + ["SELF_CONSUMPTION"] * 4
    report = run(solution(modes, factors=[0.5] * 4 + [0.0] * 4))
    assert report.exit_code == 1
    assert "Einspeisen aus dem Akku geplant" in report.warnings[0]


def test_blocked_pv_charge_with_surplus_warns_but_not_when_full():
    modes = ["IDLE"] * 4 + ["SELF_CONSUMPTION"] * 4
    feedin = [1000.0] * 4 + [0.0] * 4  # Wh per 15 min = 4 kW
    report = run(solution(modes, feedin=feedin, socs=[0.5] * 8))
    assert report.exit_code == 1
    assert "PV-Laden gesperrt (Sperren) bei 4000 W Einspeisung" in report.warnings[0]
    report = run(solution(modes, feedin=feedin, socs=[0.99] * 8))
    assert report.exit_code == 0, report.lines()


def test_measured_soc_mismatch_warns():
    report = run(solution(["SELF_CONSUMPTION"] * 8, socs=[0.2] * 8), measured_soc_factor=0.45)
    assert report.exit_code == 1
    assert "Plan beginnt bei 20 %, gemessen 45 %" in report.warnings[0]


def test_full_charge_raises_upper_limit():
    config = json.loads(json.dumps(CONFIG))
    config["devices"]["batteries"]["byd"]["max_soc_percentage"] = 90
    sol = solution(["SELF_CONSUMPTION"] * 8, socs=[0.99] * 8)
    assert script.check(sol, config, "byd", NOW).exit_code == 0
    del config["devices"]["batteries"]["byd"]["full_charge_interval_days"]
    assert script.check(sol, config, "byd", NOW).exit_code == 2


def test_cli_reads_files(tmp_path, capsys):
    sol_file = tmp_path / "solution.json"
    cfg_file = tmp_path / "config.json"
    sol_file.write_text(json.dumps(solution(["SELF_CONSUMPTION"] * 8)), encoding="utf-8")
    cfg_file.write_text(json.dumps(CONFIG), encoding="utf-8")
    code = script.main(
        ["--solution", str(sol_file), "--config", str(cfg_file), "--now", NOW.isoformat()]
    )
    assert code == 0
    assert "Status: PLAUSIBEL" in capsys.readouterr().out


def test_grid_charge_while_disabled_fails():
    config = json.loads(json.dumps(CONFIG))
    config["devices"]["inverters"] = {
        "fronius": {"battery_id": "byd", "max_ac_charge_power_w": 0.0}
    }
    modes = ["SELF_CONSUMPTION"] * 4 + ["FORCED_CHARGE"] * 4
    sol = solution(modes, factors=[0.0] * 4 + [0.5] * 4, prices=[0.75] * 4 + [0.46] * 4)
    report = script.check(sol, config, "byd", NOW)
    assert report.exit_code == 2
    assert "obwohl Laden aus dem Netz gesperrt ist" in report.failures[0]
    config["devices"]["inverters"]["fronius"]["max_ac_charge_power_w"] = None
    assert script.check(sol, config, "byd", NOW).exit_code != 2


def _full_charge_config(power_w: float | None) -> dict[str, Any]:
    config = json.loads(json.dumps(CONFIG))
    config["devices"]["inverters"] = {
        "fronius": {"battery_id": "byd", "max_ac_charge_power_w": 0.0}
    }
    battery = config["devices"]["batteries"]["byd"]
    battery["full_charge_allow_grid"] = True
    battery["full_charge_grid_power_w"] = power_w
    return config


def test_grid_charge_for_full_charge_within_power_is_allowed():
    """3 kW of 10.2 kW (factor 0.3) is within the full charge exception of 3 kW."""
    modes = ["SELF_CONSUMPTION"] * 4 + ["FORCED_CHARGE"] * 4
    sol = solution(modes, factors=[0.0] * 4 + [0.29] * 4, prices=[0.75] * 4 + [0.46] * 4)
    report = script.check(sol, _full_charge_config(3000.0), "byd", NOW)
    assert report.exit_code == 0, report.lines()
    assert any("für die Vollladung freigegeben" in line for line in report.info)


def test_grid_charge_for_full_charge_above_power_fails():
    modes = ["SELF_CONSUMPTION"] * 4 + ["FORCED_CHARGE"] * 4
    sol = solution(modes, factors=[0.0] * 4 + [0.8] * 4)
    report = script.check(sol, _full_charge_config(3000.0), "byd", NOW)
    assert report.exit_code == 2
    assert "über der Freigabe für die Vollladung" in report.failures[0]


def test_grid_charge_without_full_charge_power_still_fails():
    modes = ["SELF_CONSUMPTION"] * 4 + ["FORCED_CHARGE"] * 4
    sol = solution(modes, factors=[0.0] * 4 + [0.29] * 4)
    report = script.check(sol, _full_charge_config(None), "byd", NOW)
    assert report.exit_code == 2
    assert "obwohl Laden aus dem Netz gesperrt ist" in report.failures[0]
