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
    away: list[int] | None = None,
    step: timedelta = timedelta(hours=1),
) -> dict[str, Any]:
    n = len(modes)
    rooms = rooms or [21.0] * n
    pv = pv or [0.0] * n
    prices = prices or [0.30] * n
    dhw = dhw or [0] * n
    data, pred = {}, {}
    for i in range(n):
        t = (START + i * step).isoformat()
        data[t] = {
            "wbb12_heating_mode": float(modes[i]),
            "wbb12_dhw_mode": float(dhw[i]),
            "wbb12_room_temperature_c": rooms[i],
            "load_energy_wh": 800.0,
            "grid_feedin_energy_wh": max(0.0, pv[i] - 800.0),
        }
        if away is not None:
            data[t]["wbb12_away"] = float(away[i])
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
    assert "11 h am Stück Vorheizen/Forciert" in text


def test_long_raise_only_warns():
    """Plan of 22 October 21:42: RAISED 14-20 h (7 h), rooms up to 21.9 degC."""
    modes = [1] * 14 + [2] * 7 + [1] * 3
    report = run(solution(modes))
    assert report.exit_code == 0, report.lines()
    report = run(solution([1] * 4 + [2] * 9 + [1] * 11))
    assert report.exit_code == 1
    assert "9 h am Stück aktiv geheizt inkl. Angehoben" in report.warnings[0]


def test_long_preheat_or_forced_fails():
    report = run(solution([1] * 4 + [3] * 4 + [4] * 3 + [1] * 13))
    assert report.exit_code == 2
    assert "7 h am Stück Vorheizen/Forciert" in report.failures[0]


def test_cli_passes_all_limits(tmp_path, capsys):
    import json

    sol = tmp_path / "solution.json"
    cfg = tmp_path / "config.json"
    sol.write_text(json.dumps(solution([1] * 4 + [2] * 9 + [1] * 11)), encoding="utf-8")
    cfg.write_text(json.dumps(CONFIG), encoding="utf-8")
    args = ["--solution", str(sol), "--config", str(cfg), "--now", NOW.isoformat()]
    assert script.main(args) == 1
    assert script.main(args + ["--max-raised-h", "10"]) == 0
    assert script.main(args + ["--max-active-h", "2", "--max-raised-h", "10"]) == 0


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
    report = run(solution([0] * 7 + [1] * 17, dhw=[1] * 5 + [0] * 19))
    assert report.exit_code == 1
    assert len(report.warnings) == 2


def test_reduction_up_to_the_limit_is_plausible():
    # HA allows up to 6 h of reduction (input_number.eos_wp_max_absenkdauer)
    assert run(solution([0] * 6 + [1] * 18)).exit_code == 0
    report = run(solution([0] * 6 + [1] * 18), max_reduced_h=4)
    assert report.exit_code == 1
    assert "6 h am Stück Reduziert" in report.warnings[0]


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


# --- Holiday mode -----------------------------------------------------------

CONFIG_AWAY = {
    "devices": {
        "heat_pumps": {
            "wbb12": {
                **CONFIG["devices"]["heat_pumps"]["wbb12"],
                "away": {"room_c": 20.0, "dhw_off": True, "standby_min_c": None},
            }
        }
    }
}


def test_holiday_plan_of_26_october_is_plausible():
    """Live plan 26 Oct 21:30: away 18 degC / floor 16 degC, rooms 17.6-18.7 degC."""
    modes = [1] * 6 + [0] * 6 + [1] * 3 + [2] + [1] * 8
    rooms = [18.7] * 6 + [18.2, 18.0, 17.8, 17.7, 17.6, 17.6] + [18.0] * 12
    report = script.check(
        solution(modes, rooms=rooms, away=[1] * 24),
        CONFIG_AWAY,
        "wbb12",
        NOW,
        away_room_c=18.0,
        away_min_c=16.0,
    )
    assert report.exit_code == 0, report.lines()
    assert any("Urlaub in 24 von 24 Stunden, Untergrenze 16.0" in m for m in report.info)


def test_same_plan_without_holiday_is_implausible():
    rooms = [18.7] * 6 + [17.6] * 18
    assert run(solution([1] * 24, rooms=rooms)).exit_code == 2


def test_holiday_plan_below_the_floor_fails():
    rooms = [17.0] * 12 + [14.8] * 12
    report = script.check(
        solution([0] * 24, rooms=rooms, away=[1] * 24),
        CONFIG_AWAY,
        "wbb12",
        NOW,
        away_room_c=18.0,
        away_min_c=16.0,
    )
    assert report.exit_code == 2
    assert "14.8 °C < 15.0 °C (Urlaub)" in report.failures[0]


def test_slots_after_the_return_use_the_normal_band():
    """Away until 12:00, then the normal comfort band applies again."""
    rooms = [17.0] * 2 + [17.5] * 22
    away = [1] * 2 + [0] * 22
    report = script.check(
        solution([1] * 24, rooms=rooms, away=away), CONFIG_AWAY, "wbb12", NOW, away_min_c=16.0
    )
    assert report.exit_code == 2
    assert "(Urlaub)" not in report.failures[0]


def test_holiday_floor_falls_back_to_the_configuration():
    assert script.away_floor(CONFIG_AWAY, "wbb12") == 20.0
    assert script.away_floor(CONFIG_AWAY, "wbb12", away_room_c=18.0) == 18.0
    assert script.away_floor(CONFIG_AWAY, "wbb12", 18.0, 16.0) == 16.0
    assert script.away_floor(CONFIG, "wbb12") == 20.0


def test_long_holiday_reduction_does_not_warn():
    report = script.check(
        solution([0] * 24, rooms=[18.0] * 24, away=[1] * 24),
        CONFIG_AWAY,
        "wbb12",
        NOW,
        away_min_c=16.0,
    )
    assert report.exit_code == 0, report.lines()


def test_latest_measurement_value(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def fake_get(base: str, path: str) -> Any:
        calls.append(path)
        return {"data": {"2026-10-25T10:00:00+01:00": 19.0, "2026-10-26T10:00:00+01:00": 18.0}}

    monkeypatch.setattr(script, "_get", fake_get)
    assert script._latest("http://eos", "wbb12-away-room-temperature") == 18.0
    assert "key=wbb12-away-room-temperature" in calls[0]

    monkeypatch.setattr(script, "_get", lambda base, path: {"data": {}})
    assert script._latest("http://eos", "x") is None

    def broken(base: str, path: str) -> Any:
        raise OSError("down")

    monkeypatch.setattr(script, "_get", broken)
    assert script._latest("http://eos", "x") is None


def test_cli_holiday_overrides(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    sol = tmp_path / "solution.json"
    cfg = tmp_path / "config.json"
    sol.write_text(json.dumps(solution([0] * 24, rooms=[17.0] * 24, away=[1] * 24)))
    cfg.write_text(json.dumps(CONFIG_AWAY))
    base = ["--solution", str(sol), "--config", str(cfg), "--now", NOW.isoformat()]
    assert script.main(base) == 2  # fallback room_c 20 -> lower bound 19
    assert script.main(base + ["--away-room-c", "18", "--away-min-c", "16"]) == 0
    assert "Untergrenze 16.0" in capsys.readouterr().out


# --- 15 min plans (EOS MILP) ------------------------------------------------

Q = timedelta(minutes=15)


def quarters(hourly: list[int]) -> list[int]:
    """Expand an hourly list to 15 min slots."""
    return [v for v in hourly for _ in range(4)]


def test_slot_length_from_timestamps():
    assert script.slot_hours([(START + i * Q).isoformat() for i in range(8)]) == 0.25
    assert script.slot_hours([(START + timedelta(hours=i)).isoformat() for i in range(3)]) == 1.0
    assert script.slot_hours([START.isoformat()]) == 1.0


def test_two_hours_preheat_in_quarters_is_plausible():
    """2 h PREHEAT = 8 slots must not count as 8 h."""
    modes = quarters([1] * 4 + [3] * 2 + [1] * 18)
    report = run(solution(modes, step=Q))
    assert report.exit_code == 0, report.lines()
    assert any("Raster 15 min" in m for m in report.info)


def test_long_preheat_in_quarters_fails_with_hours():
    modes = quarters([1] * 4 + [3] * 4 + [4] * 3 + [1] * 13)
    report = run(solution(modes, step=Q))
    assert report.exit_code == 2
    assert "7 h am Stück Vorheizen/Forciert" in report.failures[0]


def test_partial_hours_are_reported():
    modes = [1] * 8 + [2] * 37 + [1] * 51  # 9.25 h RAISED
    report = run(solution(modes, step=Q))
    assert report.exit_code == 1
    assert "9.25 h am Stück aktiv geheizt" in report.warnings[0]


def test_reduction_and_hot_water_in_quarters():
    assert run(solution(quarters([0] * 6 + [1] * 18), step=Q)).exit_code == 0
    report = run(solution(quarters([0] * 7 + [1] * 17), step=Q))
    assert "7 h am Stück Reduziert" in report.warnings[0]
    # 4 h hot water = 16 slots: no warning; 5 h: warning
    assert (
        run(solution(quarters([1] * 24), dhw=quarters([1] * 4 + [0] * 20), step=Q)).exit_code == 0
    )
    report = run(solution(quarters([1] * 24), dhw=quarters([1] * 5 + [0] * 19), step=Q))
    assert "Warmwasser-Ladung in 5 Stunden" in report.warnings[0]


def test_peak_price_warning_once_per_hour():
    prices = quarters([0.25] * 18 + [0.48] * 6)
    modes = quarters([1] * 18 + [2] + [1] * 5)
    report = run(solution(modes, prices=prices, step=Q))
    assert len([m for m in report.warnings if "ohne PV-Überschuss" in m]) == 1


def test_holiday_hours_in_quarters():
    report = script.check(
        solution(quarters([0] * 24), rooms=[18.0] * 96, away=[1] * 96, step=Q),
        CONFIG_AWAY,
        "wbb12",
        NOW,
        away_min_c=16.0,
    )
    assert report.exit_code == 0, report.lines()
    assert any("Urlaub in 24 von 24 Stunden" in m for m in report.info)


def test_mode_overview_lists_changes_only():
    times = [(START + i * Q).isoformat() for i in range(6)]
    assert script._mode_changes(times, [1, 1, 3, 3, 1, 1]) == "10:00:1 10:30:3 11:00:1"


# ----------------------------------------------------------------------
# Heating zones
# ----------------------------------------------------------------------

ZONE_CONFIG = json.loads(json.dumps(CONFIG))
ZONE_CONFIG["devices"]["heat_pumps"]["wbb12"]["zones"] = {
    "enabled": True,
    "zones": [
        {
            "zone_id": "og",
            "setback_windows": {"windows": [{"start_time": "22:00", "duration": "2 hours"}]},
            "setback_room_c": 19.0,
        },
        {"zone_id": "eg"},
    ],
}


def zone_solution(
    og_modes: list[int],
    og_rooms: list[float],
    eg_modes: list[int] | None = None,
    eg_rooms: list[float] | None = None,
) -> dict[str, Any]:
    n = len(og_modes)
    sol = solution([max(m, e) for m, e in zip(og_modes, eg_modes or [1] * n)])
    for i, t in enumerate(sorted(sol["solution"]["data"])):
        row = sol["solution"]["data"][t]
        row["wbb12_zone_og_heating_mode"] = float(og_modes[i])
        row["wbb12_zone_og_room_temperature_c"] = og_rooms[i]
        row["wbb12_zone_eg_heating_mode"] = float((eg_modes or [1] * n)[i])
        row["wbb12_zone_eg_room_temperature_c"] = (eg_rooms or [21.0] * n)[i]
    return sol


def run_zones(sol: dict[str, Any], **kwargs: Any):
    return script.check(sol, ZONE_CONFIG, "wbb12", NOW, **kwargs)


def test_zone_ids_only_with_active_zones():
    assert script.zone_ids(ZONE_CONFIG, "wbb12") == ["og", "eg"]
    assert script.zone_ids(CONFIG, "wbb12") == []


def test_set_back_zone_may_drop_to_its_set_back_temperature():
    assert script.zone_floor(ZONE_CONFIG, "wbb12", "og", 19.5) == pytest.approx(19.0)
    assert script.zone_floor(ZONE_CONFIG, "wbb12", "eg", 19.5) == pytest.approx(19.5)


def test_plausible_zone_plan_lists_every_zone():
    report = run_zones(zone_solution([1] * 24, [20.5] * 24))
    assert report.exit_code == 0
    assert any(line.startswith("Zone og: Modi") for line in report.info)
    assert any(line.startswith("Zone eg: Modi") for line in report.info)


def test_cold_zone_fails_even_if_the_house_mean_is_fine():
    report = run_zones(zone_solution([1] * 24, [17.5] * 24))
    assert report.exit_code == 2
    assert any("Zone og: geplant 17.5" in m for m in report.failures)


def test_hot_zone_fails():
    report = run_zones(zone_solution([1] * 24, [21.0] * 23 + [23.8]))
    assert any("Zone og: geplant 23.8" in m for m in report.failures)


def test_long_forced_zone_fails():
    report = run_zones(zone_solution([4] * 8 + [1] * 16, [21.0] * 24), max_active_h=6)
    assert any("Zone og: 8 h am Stück" in m for m in report.failures)


def test_cold_zone_left_reduced_warns():
    report = run_zones(zone_solution([0] * 2 + [3] * 3 + [1] * 19, [18.9] + [20.0] * 23))
    assert report.exit_code == 1
    assert any("Zone og: startet mit 18.9" in m for m in report.warnings)


def test_cold_zone_that_is_heated_does_not_warn():
    report = run_zones(zone_solution([3] * 3 + [1] * 21, [18.9] + [20.0] * 23))
    assert not any("startet mit" in m for m in report.warnings)


def test_missing_zone_columns_warn():
    report = run_zones(solution([1] * 24))
    assert any("keine Zonenspalten" in m for m in report.warnings)


# ----------------------------------------------------------------------
# Minimum hold time (RL29 fork)
# ----------------------------------------------------------------------


def hold_config(minutes: float, levels: dict | None = None) -> dict:
    config = json.loads(json.dumps(CONFIG))
    opt: dict = {"min_hold_minutes": minutes}
    if levels is not None:
        opt["sg_ready_of_mode"] = levels
    config["devices"]["heat_pumps"]["wbb12"]["optimization"] = opt
    return config


def test_hold_kept_is_info_only():
    q = timedelta(minutes=15)
    modes = [1] * 4 + [0] * 4 + [1] * 4 + [2] * 4 + [1] * 8  # changes 1 h apart
    report = script.check(solution(modes, step=q), hold_config(55), "wbb12", NOW)
    text = "\n".join(report.lines())
    assert "Haltezeit 55 min eingehalten" in text
    assert "verletzt" not in text


def test_hold_violation_is_a_warning():
    q = timedelta(minutes=15)
    # REDUCED 11:00, NORMAL 11:15 (return, allowed), RAISED 11:30: 15 min after the return
    modes = [1] * 4 + [0, 1, 2, 2] + [1] * 16
    report = script.check(solution(modes, step=q), hold_config(55), "wbb12", NOW)
    assert report.exit_code == 1, report.lines()
    assert any("Haltezeit 55 min verletzt: 11:30 nach 15 min" in w for w in report.warnings)


def test_same_level_change_is_no_violation():
    q = timedelta(minutes=15)
    modes = [1] * 4 + [3, 2, 3, 2] + [1] * 16  # PREHEAT <-> RAISED: both SG 3
    report = script.check(solution(modes, step=q), hold_config(55), "wbb12", NOW)
    assert not any("Haltezeit" in w for w in report.warnings), report.lines()
    # with RAISED on its own level the same plan violates the hold
    report = script.check(
        solution(modes, step=q), hold_config(55, {"raised": 2}), "wbb12", NOW
    )
    assert any("Haltezeit" in w for w in report.warnings)


def test_no_hold_configured_no_line():
    report = run(solution([1] * 24))
    assert not any("Haltezeit" in line for line in report.lines())
