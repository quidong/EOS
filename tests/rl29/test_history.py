"""History in PostgreSQL / TimescaleDB (RL29 fork).

The pure helpers run everywhere. The database tests need a PostgreSQL server; set
``RL29_TEST_PG_DSN`` (the CI job starts a TimescaleDB service and sets it), otherwise
they are skipped.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from akkudoktoreos.core.history import (
    HistoryAccuracyPair,
    HistorySettings,
    HistoryStore,
    accuracy,
    baseline_self_consumption_cost,
    frame_rows,
    hourly_predicted,
    pick_for_lead,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


def quarter_frame(start: datetime, hours: int, columns: dict[str, float]) -> pd.DataFrame:
    index = pd.date_range(start, periods=hours * 4, freq="15min", tz="UTC")
    return pd.DataFrame(
        {name: [value] * len(index) for name, value in columns.items()}, index=index
    )


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def test_frame_rows_horizon_lead_and_non_finite():
    frame = quarter_frame(T0, 3, {"pv": 100.0, "txt": float("nan")})
    frame["mode"] = "x"  # non-numeric column is skipped value by value
    rows = frame_rows(frame, T0 + timedelta(minutes=7), horizon_hours=2)
    names = {r[1] for r in rows}
    assert names == {"pv"}
    targets = sorted(r[0] for r in rows)
    # The running slot (10:00, issued 10:07) is kept, the horizon ends before 12:07.
    assert targets[0] == T0
    assert targets[-1] == T0 + timedelta(hours=2)
    lead = {r[0]: r[3] for r in rows}
    assert lead[T0] == pytest.approx(-7 / 60, abs=1e-3)


def test_frame_rows_column_filter_and_empty():
    frame = quarter_frame(T0, 1, {"a": 1.0, "b": 2.0})
    assert {r[1] for r in frame_rows(frame, T0, 1, columns=["b"])} == {"b"}
    assert frame_rows(None, T0, 1) == []
    assert frame_rows(frame.iloc[0:0], T0, 1) == []


def test_hourly_predicted_energy_sums_level_averages():
    rows = [(T0, T0 + timedelta(minutes=15 * i), float(i)) for i in range(4)]
    assert hourly_predicted(rows, "energy") == {(T0, T0): 6.0}
    assert hourly_predicted(rows, "level") == {(T0, T0): 1.5}


def test_pick_for_lead_uses_newest_issue_with_enough_lead():
    hour = T0 + timedelta(hours=6)
    predicted = {
        (T0, hour): 10.0,  # lead 6 h
        (T0 + timedelta(hours=3), hour): 20.0,  # lead 3 h
        (T0 + timedelta(hours=5, minutes=30), hour): 30.0,  # lead 0.5 h
    }
    assert pick_for_lead(predicted, 1.0) == {hour: 20.0}
    assert pick_for_lead(predicted, 6.0) == {hour: 10.0}
    assert pick_for_lead(predicted, 0.0) == {hour: 30.0}
    assert pick_for_lead(predicted, 7.0) == {}


def test_accuracy_metrics():
    hours = [T0 + timedelta(hours=i) for i in range(4)]
    predicted = dict(zip(hours, [110.0, 90.0, 100.0, 120.0]))
    actual = dict(zip(hours, [100.0, 100.0, 100.0, 100.0]))
    result = accuracy("pv", 1.0, predicted, actual)
    assert result.n == 4
    assert result.bias == pytest.approx(5.0)
    assert result.mae == pytest.approx(10.0)
    assert result.rmse == pytest.approx(((100 + 100 + 0 + 400) / 4) ** 0.5, abs=1e-3)
    assert result.mape_of_mean == pytest.approx(0.1)
    assert result.per_day[T0.date().isoformat()]["n"] == 4
    empty = accuracy("pv", 1.0, {}, actual)
    assert empty.n == 0 and empty.mae is None


def test_baseline_self_consumption_without_battery():
    result = baseline_self_consumption_cost(
        pv_wh=[1000, 0], load_wh=[400, 500], price_amt_kwh=[0.3, 0.3], feed_in_amt_kwh=[0.08, 0.08]
    )
    assert result["grid_export_wh"] == 600
    assert result["grid_import_wh"] == 500
    assert result["costs_amt"] == pytest.approx(0.15)
    assert result["revenue_amt"] == pytest.approx(0.048)


def test_baseline_self_consumption_battery_shifts_surplus():
    result = baseline_self_consumption_cost(
        pv_wh=[1000, 0],
        load_wh=[400, 500],
        price_amt_kwh=[0.3, 0.3],
        feed_in_amt_kwh=[0.08, 0.08],
        battery_capacity_wh=10000,
        battery_soc_wh=0,
        max_charge_wh=5000,
        max_discharge_wh=5000,
        charge_eff=1.0,
        discharge_eff=1.0,
    )
    assert result["grid_export_wh"] == 0
    assert result["grid_import_wh"] == 0
    assert result["net_amt"] == 0


def test_settings_validation():
    assert HistorySettings(dsn="  ").dsn is None
    assert HistorySettings(dsn="postgresql://eos@db/eos").dsn == "postgresql://eos@db/eos"
    with pytest.raises(ValueError):
        HistorySettings(dsn="mysql://x")
    with pytest.raises(ValueError):
        HistoryAccuracyPair(name="x", predicted="a", actual="b", source="other")


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------

PG_DSN = os.environ.get("RL29_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="RL29_TEST_PG_DSN not set")


@pytest.fixture
def store():
    import psycopg

    schema = f"t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in PG_DSN else "?"
    dsn = f"{PG_DSN}{sep}options=-csearch_path%3D{schema},public"
    yield HistoryStore(dsn, retention_days=30)
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@needs_pg
def test_store_roundtrip_and_evaluation(store):
    store.ensure_schema()
    store.ensure_schema()  # idempotent
    # Two runs: one 6 h before the evaluated hours, one 1.5 h before.
    target_start = T0 + timedelta(hours=6)
    early = quarter_frame(target_start, 2, {"pv": 250.0})  # 1000 Wh per hour
    late = quarter_frame(target_start, 2, {"pv": 300.0})  # 1200 Wh per hour
    run1 = store.write_run(
        T0,
        "GENETIC",
        12.0,
        1.0,
        0.5,
        10.0,
        1.2,
        {"x": 1},
        frame_rows(early, T0, 48),
        frame_rows(early.rename(columns={"pv": "plan_pv"}), T0, 48),
    )
    run2 = store.write_run(
        target_start - timedelta(hours=1, minutes=30),
        "MILP",
        0.4,
        0.9,
        0.5,
        10.0,
        1.2,
        None,
        frame_rows(late, target_start - timedelta(hours=1, minutes=30), 48),
        [],
    )
    assert run2 > run1
    assert store.last_run_issued_at() == target_start - timedelta(hours=1, minutes=30)
    store.write_actuals(
        [(target_start, "pv_wh", 1100.0), (target_start + timedelta(hours=1), "pv_wh", 1100.0)]
    )
    store.write_actuals([(target_start, "pv_wh", 1100.0)])  # upsert
    pairs = [HistoryAccuracyPair(name="pv", predicted="pv", actual="pv_wh")]
    results = store.evaluate(pairs, [1.0, 6.0], now=target_start + timedelta(hours=3), days=2)
    by_lead = {r.lead_h: r for r in results}
    assert by_lead[1.0].n == 2 and by_lead[1.0].bias == pytest.approx(100.0)
    # The early run (T0) is 6 h and 7 h ahead of the two hours.
    assert by_lead[6.0].n == 2 and by_lead[6.0].bias == pytest.approx(-100.0)
    store.write_accuracy(target_start, 2, results)
    latest = store.latest_accuracy()
    assert {(r["name"], r["lead_h"]) for r in latest} == {("pv", 1.0), ("pv", 6.0)}
    runs = store.runs(T0 - timedelta(hours=1), T0 + timedelta(days=1))
    assert [r["algorithm"] for r in runs] == ["GENETIC", "MILP"]
    plan = store.predicted_rows("plan", "plan_pv", T0, T0 + timedelta(days=1))
    assert len(plan) == 8
    store.prune()


@needs_pg
def test_store_duplicate_forecast_rows_rejected_only_per_issue(store):
    rows = frame_rows(quarter_frame(T0, 1, {"pv": 1.0}), T0, 48)
    store.write_run(T0, "GENETIC", None, None, None, None, None, None, rows, [])
    store.write_run(
        T0 + timedelta(minutes=15), "GENETIC", None, None, None, None, None, None, rows, []
    )
    assert len(store.predicted_rows("forecast", "pv", T0, T0 + timedelta(hours=1))) == 8


# --------------------------------------------------------------------------------------
# Recorder (EMS glue)
# --------------------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402

from akkudoktoreos.core.historyrecorder import (  # noqa: E402
    HistoryRecorder,
    derive_actuals,
    run_payload,
)


def fake_solution(issued: datetime):
    index = pd.date_range(issued, periods=8, freq="15min", tz="UTC")
    prediction = pd.DataFrame(
        {
            "pvforecast_ac_energy_wh": [500.0] * 8,
            "elec_price_amt_kwh": [0.30] * 8,
            "feed_in_tariff_amt_kwh": [0.08] * 8,
            "loadforecast_energy_wh": [100.0] * 8,
        },
        index=index,
    )
    plan = pd.DataFrame(
        {
            "load_energy_wh": [200.0] * 8,
            "costs_amt": [0.0] * 8,
            "revenue_amt": [0.024] * 8,
            "battery1_soc_factor": [0.5] * 8,
        },
        index=index,
    )
    return SimpleNamespace(
        id="s1",
        generated_at=issued,
        prediction=SimpleNamespace(to_dataframe=lambda: prediction),
        solution=SimpleNamespace(to_dataframe=lambda: plan),
        total_costs_amt=0.0,
        total_revenues_amt=0.192,
        total_losses_energy_wh=5.0,
        fitness_score={1.5},
    )


def fake_config(history: HistorySettings, battery: bool = False):
    batteries = {}
    if battery:
        batteries = {
            "battery1": SimpleNamespace(
                device_id="battery1",
                capacity_wh=10000,
                min_soc_percentage=10,
                max_charge_power_w=5000,
                charging_efficiency=1.0,
                discharging_efficiency=1.0,
            )
        }
    return SimpleNamespace(
        database=SimpleNamespace(history=history), devices=SimpleNamespace(batteries=batteries)
    )


def test_run_payload_rows_and_baseline():
    issued = T0
    payload = run_payload(
        fake_config(HistorySettings(), battery=True), fake_solution(issued), "GENETIC", 3.0, 48
    )
    assert payload["algorithm"] == "GENETIC"
    assert {r[1] for r in payload["forecast_rows"]} == {
        "pvforecast_ac_energy_wh",
        "elec_price_amt_kwh",
        "feed_in_tariff_amt_kwh",
        "loadforecast_energy_wh",
    }
    assert len(payload["plan_rows"]) == 8 * 4
    meta = payload["meta"]
    # Baseline: 300 Wh surplus per slot go into the half-full battery, nothing fed in.
    assert meta["baseline"]["grid_export_wh"] == 0
    assert payload["baseline_net_amt"] == 0
    assert meta["plan_net_amt"] == pytest.approx(-0.192)
    assert meta["saving_vs_baseline_amt"] == pytest.approx(0.192)


class FakeMeasurement:
    """Cumulative kWh meters and a temperature, one value per hour."""

    def __init__(self, start: datetime, hours: int):
        self.start = start
        self.hours = hours

    async def key_to_lists(self, key, start, end):
        if key == "missing":
            return [], []
        return [start], [1.0]

    async def _energy_from_meter_readings(self, key, start, end, interval):
        per_hour = {"house": 1.5, "hp": 0.5, "reset": -3.0}[key]
        return np.array([per_hour] * self.hours)

    async def key_to_array(
        self, key, start_datetime, end_datetime, interval, fill_method, boundary
    ):
        return np.array([21.0] * self.hours)


@pytest.mark.asyncio
async def test_derive_actuals_meter_mean_subtract_and_gaps():
    from akkudoktoreos.core.history import HistoryActualSource

    settings = HistorySettings(
        actuals=[
            HistoryActualSource(name="hp_wh", keys=["hp"]),
            HistoryActualSource(name="base_wh", keys=["house"], subtract=["hp_wh"]),
            HistoryActualSource(name="room_c", keys=["room"], kind="mean", scale=1.0),
            HistoryActualSource(name="gone", keys=["missing"]),
            HistoryActualSource(name="reset", keys=["reset"]),
        ]
    )
    rows = await derive_actuals(
        settings, FakeMeasurement(T0, 6), T0 + timedelta(hours=6, minutes=20)
    )
    by_series: dict[str, list[float]] = {}
    for hour, series, value in rows:
        by_series.setdefault(series, []).append(value)
        assert hour.minute == 0 and hour < T0 + timedelta(hours=6)
    assert by_series["hp_wh"] == [500.0] * 6
    assert by_series["base_wh"] == [1000.0] * 6
    assert by_series["room_c"] == [21.0] * 6
    assert "gone" not in by_series and "reset" not in by_series


@pytest.mark.asyncio
async def test_recorder_disabled_does_nothing():
    recorder = HistoryRecorder()
    await recorder.record(fake_config(HistorySettings()), None, fake_solution(T0), "GENETIC", 1.0)
    assert recorder.last_error is None


@pytest.mark.asyncio
async def test_recorder_unreachable_database_is_logged_not_raised():
    recorder = HistoryRecorder()
    settings = HistorySettings(enabled=True, dsn="postgresql://eos@127.0.0.1:1/eos")
    await recorder.record(
        fake_config(settings), FakeMeasurement(T0, 6), fake_solution(T0), "GENETIC", 1.0
    )
    assert recorder.last_error and "connect" in recorder.last_error


@needs_pg
@pytest.mark.asyncio
async def test_recorder_end_to_end(store):
    from akkudoktoreos.core.history import HistoryActualSource

    settings = HistorySettings(
        enabled=True,
        dsn=store.dsn,
        actuals=[HistoryActualSource(name="load_wh", keys=["house"])],
        accuracy=[
            HistoryAccuracyPair(
                name="load", predicted="load_energy_wh", source="plan", actual="load_wh"
            )
        ],
        lead_bands_h=[0.0],
        min_store_interval_minutes=14,
    )
    recorder = HistoryRecorder()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    issued = now - timedelta(hours=3)
    config = fake_config(settings)
    await recorder.record(
        config, FakeMeasurement(now - timedelta(hours=6), 6), fake_solution(issued), "GENETIC", 2.0
    )
    # A second run within 14 minutes is not stored again.
    await recorder.record(
        config,
        FakeMeasurement(now - timedelta(hours=6), 6),
        fake_solution(issued + timedelta(minutes=5)),
        "GENETIC",
        2.0,
    )
    assert recorder.last_error is None, recorder.last_error
    runs = store.runs(issued - timedelta(hours=1), now + timedelta(hours=1))
    assert len(runs) == 1
    # Plan: 4 x 200 Wh per hour for 2 hours; actual 1500 Wh per hour.
    load = [r for r in recorder.last_accuracy if r["name"] == "load"][0]
    assert load["n"] == 2
    assert load["bias"] == pytest.approx(800.0 - 1500.0)


@needs_pg
def test_daily_costs_per_algorithm(store):
    day1 = datetime(2026, 10, 20, 8, tzinfo=UTC)
    day2 = datetime(2026, 10, 21, 8, tzinfo=UTC)
    for day, algorithm, price in ((day1, "GENETIC", 0.30), (day2, "MILP", 0.20)):
        prices = quarter_frame(
            day, 2, {"elec_price_amt_kwh": price, "feed_in_tariff_amt_kwh": 0.08}
        )
        store.write_run(
            day - timedelta(hours=1),
            algorithm,
            None,
            None,
            None,
            None,
            None,
            None,
            frame_rows(prices, day - timedelta(hours=1), 48),
            [],
        )
        store.write_actuals(
            [(day, "grid_import_wh", 1000.0), (day + timedelta(hours=1), "grid_export_wh", 2000.0)]
        )
    rows = store.daily_costs(day1 - timedelta(days=1), day2 + timedelta(days=1), "Europe/Berlin")
    by_algorithm = {r["algorithm"]: r for r in rows}
    assert by_algorithm["GENETIC"]["net_amt"] == pytest.approx(1.0 * 0.30 - 2.0 * 0.08)
    assert by_algorithm["MILP"]["net_amt"] == pytest.approx(1.0 * 0.20 - 2.0 * 0.08)
    assert by_algorithm["MILP"]["hours"] == 2


def test_active_algorithm_alternates_by_day():
    from types import SimpleNamespace

    from akkudoktoreos.optimization.optimization import OptimizationAlgorithm, active_algorithm

    settings = SimpleNamespace(
        algorithm=OptimizationAlgorithm.GENETIC, alternate_algorithms=["GENETIC", "MILP"]
    )
    monday = datetime(2026, 10, 26, 12)
    picked = {active_algorithm(settings, monday + timedelta(days=i)) for i in range(4)}
    assert picked == {OptimizationAlgorithm.GENETIC, OptimizationAlgorithm.MILP}
    assert active_algorithm(settings, monday) != active_algorithm(
        settings, monday + timedelta(days=1)
    )
    assert active_algorithm(settings, monday) == active_algorithm(
        settings, monday + timedelta(hours=8)
    )
    settings.alternate_algorithms = []
    assert active_algorithm(settings, monday) == OptimizationAlgorithm.GENETIC


@needs_pg
def test_grafana_dashboard_queries_run_on_the_schema(store):
    import json
    import re
    from pathlib import Path

    import psycopg

    store.ensure_schema()
    path = (
        Path(__file__).parents[2]
        / "scripts"
        / "rl29"
        / "grafana"
        / "dashboards"
        / "eos-history.json"
    )
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    assert dashboard["uid"] == "eos-history" and len(dashboard["panels"]) >= 8
    with psycopg.connect(store.dsn) as conn:
        for panel in dashboard["panels"]:
            sql = panel["targets"][0]["rawSql"]
            sql = re.sub(r"\$__timeFilter\(([^)]+)\)", r"\1 > now() - interval '7 days'", sql)
            sql = sql.replace("$__timeFrom()", "now() - interval '7 days'").replace(
                "$__timeTo()", "now()"
            )
            conn.execute(sql).fetchall()
            conn.rollback()
