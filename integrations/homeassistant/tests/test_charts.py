"""Plan and forecast series for charts (no Home Assistant needed)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CHARTS = Path(__file__).resolve().parents[1] / "custom_components" / "eos_control" / "charts.py"
spec = importlib.util.spec_from_file_location("eos_control_charts", CHARTS)
assert spec and spec.loader
charts = importlib.util.module_from_spec(spec)
sys.modules["eos_control_charts"] = charts
spec.loader.exec_module(charts)

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 2, 10, 7, tzinfo=TZ)


def solution(start: datetime, n: int = 8, load: float = 100.0, hp: float = 250.0, **extra):
    """EOS solution with ``n`` 15-min slots from ``start``."""
    plan, prediction = {}, {}
    for i in range(n):
        t = (start + timedelta(minutes=15 * i)).isoformat()
        plan[t] = {
            "date_time": t,
            "load_energy_wh": load + i,
            "grid_consumption_energy_wh": load + i,
            "grid_feedin_energy_wh": 0.0,
            "costs_amt": 0.0312345,
            "hp1_energy_wh": hp,
            "hp1_rod_energy_wh": 0.0,
            "hp1_heating_mode": 1.0,
            "hp1_dhw_mode": 0.0,
            "hp1_standby": 0.0,
            "hp1_room_temperature_c": 21.0 + i / 10,
            "hp1_slab_temperature_c": 23.456,
            "hp1_dhw_temperature_c": 48.0,
            "hp1_kpi_room_mae_1h_k": 0.06,  # not a chart column
            "bat1_soc_factor": 0.5,
            "other_energy_wh": 999.0,
            **extra,
        }
        prediction[t] = {
            "date_time": t,
            "pvforecast_ac_energy_wh": 400.0,
            "loadforecast_energy_wh": 90.0,
            "elec_price_amt_kwh": 0.30 + i / 100,
            "feed_in_tariff_amt_kwh": 0.0721,
            "weather_air_temp_celcius": None,
        }
    return {
        "generated_at": (start + timedelta(minutes=3)).isoformat(),
        "solution": {"data": plan},
        "prediction": {"data": prediction},
    }


def test_plan_rows_use_short_keys_and_skip_other_columns():
    rows = charts.plan_rows(solution(NOW.replace(minute=0)), "hp1")
    assert len(rows) == 8
    first = rows[0]
    assert first["t"] == "2026-11-02T10:00:00+01:00"
    assert first["load_wh"] == 100.0
    assert first["heat_pump_wh"] == 250.0
    assert first["room_c"] == 21.0
    assert first["slab_c"] == 23.46  # two decimals
    assert first["costs_amt"] == 0.0312
    assert first["bat1_soc_pct"] == 50.0
    assert "other_wh" not in first and "date_time" not in first
    assert not any("kpi" in key for key in first)


def test_rows_are_sorted_and_invalid_values_dropped():
    sol = solution(NOW.replace(minute=0), n=3)
    data = sol["solution"]["data"]
    data["no time"] = {"load_energy_wh": 1.0}
    data["2026-11-02T09:45:00+01:00"] = {"load_energy_wh": float("nan"), "hp1_energy_wh": "x"}
    rows = charts.plan_rows(sol, "hp1")
    assert [r["t"][11:16] for r in rows] == ["09:45", "10:00", "10:15", "10:30"]
    assert rows[0] == {"t": "2026-11-02T09:45:00+01:00"}


def test_forecast_rows():
    rows = charts.forecast_rows(solution(NOW.replace(minute=0), n=2))
    assert rows[1] == {
        "t": "2026-11-02T10:15:00+01:00",
        "pv_wh": 400.0,
        "load_wh": 90.0,
        "price_amt_kwh": 0.31,
        "feed_in_amt_kwh": 0.0721,
    }
    assert charts.forecast_rows({"generated_at": "x"}) == []


def test_hourly_sums_energy_and_averages_prices():
    rows = charts.forecast_rows(solution(NOW.replace(minute=30), n=6))  # 10:30 .. 11:45
    hours = charts.hourly(rows, TZ, mean_keys=("price_amt_kwh",))
    assert [h["t"][11:16] for h in hours] == ["10:00", "11:00"]
    assert hours[0]["slots"] == 2 and hours[1]["slots"] == 4
    assert hours[1]["pv_kwh"] == pytest.approx(1.6)
    assert hours[1]["price_amt_kwh"] == pytest.approx((0.32 + 0.33 + 0.34 + 0.35) / 4)
    assert "pv_wh" not in hours[1]


def test_slot_minutes():
    rows = charts.plan_rows(solution(NOW.replace(minute=0), n=2), "hp1")
    assert charts.slot_minutes(rows) == 15
    assert charts.slot_minutes(rows[:1]) == charts.DEFAULT_SLOT_MINUTES


def test_memory_keeps_the_values_planned_for_passed_slots():
    memory = charts.PlanMemory()
    start = NOW.replace(minute=0)
    memory.update(charts.plan_rows(solution(start, n=8, hp=250.0), "hp1"), NOW)
    # new plan 30 min later with other values: the first two slots keep the old plan
    later = start + timedelta(minutes=30)
    memory.update(charts.plan_rows(solution(later, n=4, hp=500.0), "hp1"), NOW)
    series = memory.series()
    assert [r["heat_pump_wh"] for r in series] == [250.0, 250.0, 500.0, 500.0, 500.0, 500.0]
    # the old plan's slots beyond the new horizon are gone (horizon end 11:30)
    assert series[-1]["t"] == "2026-11-02T11:15:00+01:00"
    # old slots expire after MEMORY_HOURS
    memory.update([], NOW + timedelta(hours=charts.MEMORY_HOURS, minutes=40))
    assert [r["t"][11:16] for r in memory.series()] == ["11:00", "11:15"]


def test_memory_round_trip():
    memory = charts.PlanMemory()
    memory.update(charts.plan_rows(solution(NOW.replace(minute=0), n=3), "hp1"), NOW)
    restored = charts.PlanMemory.from_list(memory.as_list() + [{"t": "bad"}, "x"])
    assert restored.series() == memory.series()


def test_build_charts_current_values_and_window():
    memory = charts.PlanMemory()
    # yesterday's plan for 09:00-10:00 is remembered, the current plan starts at 10:00
    memory.update(charts.plan_rows(solution(NOW.replace(hour=9, minute=0), n=4), "hp1"), NOW)
    result = charts.build_charts(solution(NOW.replace(minute=0), n=8), "hp1", memory, NOW, TZ)
    assert result.generated_at == NOW.replace(minute=3)
    assert result.slot_minutes == 15
    assert [r["t"][11:16] for r in result.plan][:5] == ["09:00", "09:15", "09:30", "09:45", "10:00"]
    assert len(result.plan) == 12
    assert [h["t"][11:16] for h in result.plan_hourly] == ["09:00", "10:00", "11:00"]
    # 10:00 hour: loads 100..103 Wh, heat pump 4 x 250 Wh
    assert result.load_hour_kwh == pytest.approx(0.406)
    assert result.heat_pump_hour_kwh == pytest.approx(1.0)
    assert result.plan_hourly[1]["heat_pump_kwh"] == pytest.approx(1.0)
    assert result.room_c == 21.0  # 10:07 is in the 10:00 slot
    assert result.dhw_c == 48.0
    assert len(result.forecast) == 8 and result.forecast_hourly[0]["pv_kwh"] == 1.6


def test_build_charts_window_is_tied_to_the_plan_not_to_now():
    memory = charts.PlanMemory()
    sol = solution(NOW.replace(minute=0), n=8)
    first = charts.build_charts(sol, "hp1", memory, NOW, TZ)
    second = charts.build_charts(sol, "hp1", memory, NOW + timedelta(minutes=10), TZ)
    assert first.plan == second.plan and first.plan_hourly == second.plan_hourly


def test_build_charts_without_eos_uses_the_memory():
    memory = charts.PlanMemory()
    charts.build_charts(solution(NOW.replace(minute=0), n=8), "hp1", memory, NOW, TZ)
    offline = charts.build_charts(None, "hp1", memory, NOW + timedelta(minutes=20), TZ)
    assert offline.generated_at == NOW.replace(minute=3)  # last plan, not "no plan"
    assert len(offline.plan) == 8 and offline.forecast == []
    assert offline.room_c == 21.1  # 10:27 is in the 10:15 slot
    empty = charts.build_charts(None, "hp1", charts.PlanMemory(), NOW, TZ)
    assert empty.plan == [] and empty.load_hour_kwh is None and empty.room_c is None
    assert empty.generated_at is None


def test_hour_energy_uses_the_local_hour():
    utc_rows = [
        {"t": "2026-11-02T09:00:00+00:00", "load_wh": 100.0},  # 10:00 local
        {"t": "2026-11-02T08:45:00+00:00", "load_wh": 50.0},  # 09:45 local
    ]
    assert charts.hour_energy_kwh(utc_rows, NOW, TZ, "load_wh") == 0.1
    assert charts.hour_energy_kwh(utc_rows, NOW, TZ, "heat_pump_wh") is None
