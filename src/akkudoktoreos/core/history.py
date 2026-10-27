"""Long-term history of forecasts, plans and measurements (eos-plus fork).

EOS keeps its own data only as long as the optimizer needs it. To judge how good a
forecast or a plan was, and to train forecast models on more than a few weeks, every
run is also written to PostgreSQL (TimescaleDB when the extension is available):

- ``eos_run``          one row per optimization run (algorithm, duration, costs, baseline)
- ``eos_forecast``     every forecast series as issued, per target slot, with lead time
- ``eos_plan``         every plan series (solution columns) per target slot
- ``eos_actual``       hourly actual values (energy per hour, mean temperature, ...)
- ``eos_accuracy``     forecast/plan accuracy per series, lead time band and day

The writer never blocks or breaks an optimization run: every database access runs in a
worker thread, all errors are logged and swallowed, and a missing driver or an empty
``dsn`` simply disables the history.

Without TimescaleDB the same tables are plain PostgreSQL tables with indexes; with it
they become hypertables with compression and a retention policy.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from loguru import logger
from pydantic import Field, field_validator

from akkudoktoreos.config.configabc import SettingsBaseModel

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


class HistoryAccuracyPair(SettingsBaseModel):
    """A forecast or plan series and the actual series it is judged against."""

    name: str = Field(
        json_schema_extra={"description": "Name of the pair in reports.", "examples": ["pv"]}
    )
    predicted: str = Field(
        json_schema_extra={
            "description": (
                "Series in eos_forecast (prediction columns) or eos_plan (solution columns)."
            ),
            "examples": ["pvforecast_ac_energy_wh", "loadforecast_energy_wh"],
        }
    )
    source: str = Field(
        default="forecast",
        json_schema_extra={
            "description": "Where the predicted series lives [forecast | plan].",
            "examples": ["forecast", "plan"],
        },
    )
    actual: str = Field(
        json_schema_extra={
            "description": "Series in eos_actual (hourly).",
            "examples": ["pv_wh", "load_wh"],
        }
    )
    kind: str = Field(
        default="energy",
        json_schema_extra={
            "description": (
                "energy: predicted slot values are summed per hour; "
                "level: predicted slot values are averaged per hour."
            ),
            "examples": ["energy", "level"],
        },
    )

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        if value not in ("forecast", "plan"):
            raise ValueError("source must be 'forecast' or 'plan'")
        return value

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in ("energy", "level"):
            raise ValueError("kind must be 'energy' or 'level'")
        return value


class HistoryActualSource(SettingsBaseModel):
    """How an hourly actual series is derived from EOS measurements."""

    name: str = Field(
        json_schema_extra={"description": "Series name in eos_actual.", "examples": ["pv_wh"]}
    )
    keys: list[str] = Field(
        json_schema_extra={
            "description": (
                "Measurement keys. kind=meter: cumulative energy meters in kWh (summed); "
                "kind=mean: values averaged per hour (summed over keys)."
            ),
            "examples": [["sensor.energie_pv_erzeugung_gesamt"]],
        }
    )
    kind: str = Field(
        default="meter",
        json_schema_extra={"description": "meter | mean | household", "examples": ["meter"]},
    )
    subtract: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "description": (
                "Other actual series (by name, derived before this one) subtracted per hour, "
                "e.g. heat pump energy from the household meter to get the base load."
            ),
            "examples": [["heatpump_wh"]],
        },
    )
    scale: float = Field(
        default=1000.0,
        json_schema_extra={
            "description": "Factor applied to the derived value (meter kWh → Wh: 1000).",
            "examples": [1000.0, 1.0],
        },
    )


def _default_actuals() -> list[HistoryActualSource]:
    return []


def _default_pairs() -> list[HistoryAccuracyPair]:
    return []


class HistorySettings(SettingsBaseModel):
    """Long-term history in PostgreSQL / TimescaleDB (eos-plus fork)."""

    enabled: bool = Field(
        default=False,
        json_schema_extra={
            "description": "Write runs, forecasts, plans and actuals.",
            "examples": [True],
        },
    )
    dsn: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "libpq connection string, e.g. postgresql://eos:secret@eos-history:5432/eos. "
                "Prefer the environment variable EOS_DATABASE__HISTORY__DSN for the password."
            ),
            "examples": ["postgresql://eos@eos-history:5432/eos"],
        },
    )
    retention_days: int = Field(
        default=1095,
        ge=30,
        json_schema_extra={"description": "Rows older than this are dropped.", "examples": [1095]},
    )
    forecast_hours: int = Field(
        default=48,
        ge=1,
        json_schema_extra={
            "description": "Hours of every forecast and plan that are stored.",
            "examples": [48],
        },
    )
    min_store_interval_minutes: int = Field(
        default=14,
        ge=0,
        json_schema_extra={
            "description": (
                "A run is stored at most this often (EOS may optimize every few minutes; "
                "the lead time analysis needs one plan per slot length, not every run)."
            ),
            "examples": [14],
        },
    )
    actuals: list[HistoryActualSource] = Field(
        default_factory=_default_actuals,
        json_schema_extra={"description": "Hourly actual series derived from measurements."},
    )
    accuracy: list[HistoryAccuracyPair] = Field(
        default_factory=_default_pairs,
        json_schema_extra={"description": "Forecast/plan series judged against actuals."},
    )
    lead_bands_h: list[float] = Field(
        default_factory=lambda: [1.0, 6.0, 24.0],
        json_schema_extra={
            "description": (
                "Lead time bands for the accuracy: for each band the newest prediction "
                "issued at least this many hours before the target hour is used."
            ),
            "examples": [[1.0, 6.0, 24.0]],
        },
    )
    accuracy_days: int = Field(
        default=14,
        ge=1,
        json_schema_extra={
            "description": "Days evaluated for the published accuracy.",
            "examples": [14],
        },
    )

    @field_validator("dsn")
    @classmethod
    def _dsn(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.startswith(("postgresql://", "postgres://", "host=", "dbname=")):
            raise ValueError("dsn must be a postgresql:// URL or a libpq key/value string")
        return value


# --------------------------------------------------------------------------------------
# Pure helpers (tested without a database)
# --------------------------------------------------------------------------------------


def _utc(value: Any) -> datetime:
    """Any datetime-like (pendulum, pandas, datetime, ISO string) as aware UTC datetime."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise TypeError(f"Not a datetime: {value!r}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(value.timestamp(), tz=timezone.utc)


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def frame_rows(
    frame: Any,
    issued_at: datetime,
    horizon_hours: float,
    columns: Optional[Iterable[str]] = None,
) -> list[tuple[datetime, str, float, float]]:
    """(target_time, series, value, lead_h) rows of a DataFrame with a datetime index.

    Non-numeric and non-finite values are skipped; only targets within
    ``horizon_hours`` after ``issued_at`` (and not before the slot that contains it)
    are kept.
    """
    if frame is None or len(frame) == 0:
        return []
    issued = _utc(issued_at)
    end = issued + timedelta(hours=horizon_hours)
    wanted = set(columns) if columns is not None else None
    rows: list[tuple[datetime, str, float, float]] = []
    for column in frame.columns:
        name = str(column)
        if wanted is not None and name not in wanted:
            continue
        series = frame[column]
        for stamp, raw in series.items():
            value = _finite(raw)
            if value is None:
                continue
            target = _utc(stamp)
            # Keep the slot that is currently running (target up to one hour back).
            if target < issued - timedelta(hours=1) or target >= end:
                continue
            lead_h = (target - issued).total_seconds() / 3600.0
            rows.append((target, name, value, round(lead_h, 4)))
    return rows


def hourly_predicted(
    rows: Sequence[tuple[datetime, datetime, float]], kind: str
) -> dict[tuple[datetime, datetime], float]:
    """Aggregate (issued_at, target_time, value) slot rows per (issued_at, target hour)."""
    sums: dict[tuple[datetime, datetime], float] = {}
    counts: dict[tuple[datetime, datetime], int] = {}
    for issued, target, value in rows:
        hour = _utc(target).replace(minute=0, second=0, microsecond=0)
        key = (_utc(issued), hour)
        sums[key] = sums.get(key, 0.0) + value
        counts[key] = counts.get(key, 0) + 1
    if kind == "level":
        return {key: sums[key] / counts[key] for key in sums}
    return sums


def pick_for_lead(
    predicted: dict[tuple[datetime, datetime], float], lead_h: float
) -> dict[datetime, float]:
    """Per target hour, the newest prediction issued at least ``lead_h`` before it."""
    best: dict[datetime, tuple[datetime, float]] = {}
    for (issued, hour), value in predicted.items():
        if (hour - issued).total_seconds() / 3600.0 < lead_h - 1e-9:
            continue
        current = best.get(hour)
        if current is None or issued > current[0]:
            best[hour] = (issued, value)
    return {hour: value for hour, (_, value) in best.items()}


@dataclass
class AccuracyResult:
    """Accuracy of one series for one lead time band."""

    name: str
    lead_h: float
    n: int
    mae: Optional[float]
    bias: Optional[float]
    rmse: Optional[float]
    actual_mean: Optional[float]
    mape_of_mean: Optional[float] = None
    per_day: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "lead_h": self.lead_h,
            "n": self.n,
            "mae": self.mae,
            "bias": self.bias,
            "rmse": self.rmse,
            "actual_mean": self.actual_mean,
            "mae_rel": self.mape_of_mean,
            "per_day": self.per_day,
        }


def accuracy(
    name: str,
    lead_h: float,
    predicted: dict[datetime, float],
    actual: dict[datetime, float],
) -> AccuracyResult:
    """Bias (predicted - actual), MAE and RMSE over the hours both series have."""
    errors: list[float] = []
    actuals: list[float] = []
    per_day: dict[str, list[float]] = {}
    for hour in sorted(set(predicted) & set(actual)):
        error = predicted[hour] - actual[hour]
        errors.append(error)
        actuals.append(actual[hour])
        per_day.setdefault(hour.date().isoformat(), []).append(error)
    if not errors:
        return AccuracyResult(name, lead_h, 0, None, None, None, None)
    n = len(errors)
    mae = sum(abs(e) for e in errors) / n
    bias = sum(errors) / n
    rmse = math.sqrt(sum(e * e for e in errors) / n)
    actual_mean = sum(actuals) / n
    rel = mae / abs(actual_mean) if abs(actual_mean) > 1e-9 else None
    days = {
        day: {
            "n": float(len(values)),
            "mae": sum(abs(v) for v in values) / len(values),
            "bias": sum(values) / len(values),
        }
        for day, values in per_day.items()
    }
    return AccuracyResult(
        name,
        lead_h,
        n,
        round(mae, 3),
        round(bias, 3),
        round(rmse, 3),
        round(actual_mean, 3),
        round(rel, 4) if rel is not None else None,
        days,
    )


def baseline_self_consumption_cost(
    pv_wh: Sequence[float],
    load_wh: Sequence[float],
    price_amt_kwh: Sequence[float],
    feed_in_amt_kwh: Sequence[float],
    battery_capacity_wh: float = 0.0,
    battery_soc_wh: float = 0.0,
    max_charge_wh: float = 0.0,
    max_discharge_wh: float = 0.0,
    charge_eff: float = 0.95,
    discharge_eff: float = 0.95,
    min_soc_wh: float = 0.0,
) -> dict[str, float]:
    """Costs of the plain self-consumption strategy for the same forecasts.

    PV covers the load first, the surplus charges the battery, the rest is fed in;
    a deficit is taken from the battery, then from the grid. No grid charging, no
    shifting of controllable loads. This is the reference the optimizer must beat.
    """
    soc = max(min(battery_soc_wh, battery_capacity_wh), 0.0)
    costs = revenue = grid_import = grid_export = 0.0
    for i in range(min(len(pv_wh), len(load_wh), len(price_amt_kwh), len(feed_in_amt_kwh))):
        pv = max(_finite(pv_wh[i]) or 0.0, 0.0)
        load = max(_finite(load_wh[i]) or 0.0, 0.0)
        price = _finite(price_amt_kwh[i]) or 0.0
        tariff = _finite(feed_in_amt_kwh[i]) or 0.0
        net = pv - load
        if net >= 0:
            room = max(battery_capacity_wh - soc, 0.0) / max(charge_eff, 1e-9)
            into_battery = min(net, max_charge_wh, room)
            soc += into_battery * charge_eff
            export = net - into_battery
            grid_export += export
            revenue += export / 1000.0 * tariff
        else:
            deficit = -net
            available = max(soc - min_soc_wh, 0.0) * discharge_eff
            from_battery = min(deficit, max_discharge_wh, available)
            soc -= from_battery / max(discharge_eff, 1e-9)
            imported = deficit - from_battery
            grid_import += imported
            costs += imported / 1000.0 * price
    return {
        "costs_amt": round(costs, 4),
        "revenue_amt": round(revenue, 4),
        "net_amt": round(costs - revenue, 4),
        "grid_import_wh": round(grid_import, 1),
        "grid_export_wh": round(grid_export, 1),
    }


# --------------------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------------------

SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS eos_run (
        run_id       BIGSERIAL,
        issued_at    TIMESTAMPTZ NOT NULL,
        algorithm    TEXT NOT NULL,
        duration_s   DOUBLE PRECISION,
        costs_amt    DOUBLE PRECISION,
        revenues_amt DOUBLE PRECISION,
        losses_wh    DOUBLE PRECISION,
        baseline_net_amt DOUBLE PRECISION,
        meta         JSONB,
        PRIMARY KEY (run_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS eos_run_issued ON eos_run (issued_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS eos_forecast (
        target_time TIMESTAMPTZ NOT NULL,
        issued_at   TIMESTAMPTZ NOT NULL,
        series      TEXT NOT NULL,
        value       DOUBLE PRECISION NOT NULL,
        lead_h      REAL NOT NULL,
        run_id      BIGINT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS eos_forecast_key ON eos_forecast (series, target_time, issued_at)",
    """
    CREATE TABLE IF NOT EXISTS eos_plan (
        target_time TIMESTAMPTZ NOT NULL,
        issued_at   TIMESTAMPTZ NOT NULL,
        series      TEXT NOT NULL,
        value       DOUBLE PRECISION NOT NULL,
        lead_h      REAL NOT NULL,
        run_id      BIGINT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS eos_plan_key ON eos_plan (series, target_time, issued_at)",
    """
    CREATE TABLE IF NOT EXISTS eos_actual (
        hour   TIMESTAMPTZ NOT NULL,
        series TEXT NOT NULL,
        value  DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (series, hour)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eos_accuracy (
        evaluated_at TIMESTAMPTZ NOT NULL,
        name   TEXT NOT NULL,
        lead_h REAL NOT NULL,
        days   INTEGER NOT NULL,
        n      INTEGER NOT NULL,
        mae    DOUBLE PRECISION,
        bias   DOUBLE PRECISION,
        rmse   DOUBLE PRECISION,
        actual_mean DOUBLE PRECISION,
        PRIMARY KEY (name, lead_h, evaluated_at)
    )
    """,
]

HYPERTABLES = {
    "eos_forecast": "target_time",
    "eos_plan": "target_time",
    "eos_actual": "hour",
}

SERIES_TABLES = {"forecast": "eos_forecast", "plan": "eos_plan"}

DAILY_COSTS_SQL = """
WITH hours AS (
    SELECT a.hour,
           sum(CASE WHEN a.series = 'grid_import_wh' THEN a.value END) AS imp,
           sum(CASE WHEN a.series = 'grid_export_wh' THEN a.value END) AS exp
    FROM eos_actual a
    WHERE a.series IN ('grid_import_wh', 'grid_export_wh')
      AND a.hour >= %(start)s AND a.hour < %(end)s
    GROUP BY a.hour
), priced AS (
    SELECT h.hour, h.imp, h.exp,
           (SELECT avg(f.value) FROM eos_forecast f
             WHERE f.series = 'elec_price_amt_kwh'
               AND f.target_time >= h.hour AND f.target_time < h.hour + interval '1 hour'
               AND f.issued_at = (SELECT max(f2.issued_at) FROM eos_forecast f2
                                  WHERE f2.series = 'elec_price_amt_kwh'
                                    AND f2.target_time = f.target_time
                                    AND f2.issued_at <= h.hour)) AS price,
           (SELECT avg(f.value) FROM eos_forecast f
             WHERE f.series = 'feed_in_tariff_amt_kwh'
               AND f.target_time >= h.hour AND f.target_time < h.hour + interval '1 hour'
               AND f.issued_at = (SELECT max(f2.issued_at) FROM eos_forecast f2
                                  WHERE f2.series = 'feed_in_tariff_amt_kwh'
                                    AND f2.target_time = f.target_time
                                    AND f2.issued_at <= h.hour)) AS tariff
    FROM hours h
), algo AS (
    SELECT DISTINCT ON (day) day, algorithm FROM (
        SELECT (issued_at AT TIME ZONE %(tz)s)::date AS day, algorithm, count(*) AS n
        FROM eos_run WHERE issued_at >= %(start)s AND issued_at < %(end)s
        GROUP BY 1, 2) c
    ORDER BY day, n DESC
)
SELECT (p.hour AT TIME ZONE %(tz)s)::date AS day,
       algo.algorithm,
       count(*) AS hours,
       sum(coalesce(p.imp, 0)) AS import_wh,
       sum(coalesce(p.exp, 0)) AS export_wh,
       sum(coalesce(p.imp, 0) / 1000 * coalesce(p.price, 0)
           - coalesce(p.exp, 0) / 1000 * coalesce(p.tariff, 0)) AS net_amt
FROM priced p
LEFT JOIN algo ON algo.day = (p.hour AT TIME ZONE %(tz)s)::date
GROUP BY 1, 2 ORDER BY 1
"""
"""Realised net grid costs per local day and algorithm."""


class HistoryUnavailable(RuntimeError):
    """The history database cannot be used (no driver, no dsn, connection failed)."""


class HistoryStore:
    """Thin synchronous access layer; callers run it in a worker thread."""

    def __init__(self, dsn: str, retention_days: int = 1095):
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dependency is installed in the image
            raise HistoryUnavailable("psycopg is not installed") from exc
        self.dsn = dsn
        self.retention_days = retention_days
        self.timescale = False
        self._schema_ready = False
        self._lock = threading.Lock()

    # ---- connection / schema ------------------------------------------------------

    def connect(self) -> Any:
        import psycopg

        try:
            return psycopg.connect(self.dsn, connect_timeout=5, autocommit=False)
        except Exception as exc:
            raise HistoryUnavailable(f"cannot connect: {exc}") from exc

    def ensure_schema(self) -> None:
        """Create tables (idempotent); hypertables and policies when TimescaleDB exists.

        Every TimescaleDB step runs on its own (autocommit), so a step that fails on an
        existing database (e.g. compression already configured) leaves the others in
        place. Whether the tables are hypertables is read back afterwards.
        """
        if self._schema_ready:
            return
        import psycopg

        with self._lock:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    for statement in SCHEMA_SQL:
                        cur.execute(statement)
                conn.commit()
            try:
                conn = psycopg.connect(self.dsn, connect_timeout=5, autocommit=True)
            except Exception as exc:
                raise HistoryUnavailable(f"cannot connect: {exc}") from exc
            with conn:
                available = (
                    conn.execute(
                        "SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"
                    ).fetchone()
                    is not None
                )
                if available:
                    steps: list[tuple[str, tuple[Any, ...]]] = [
                        ("CREATE EXTENSION IF NOT EXISTS timescaledb", ())
                    ]
                    for table, column in HYPERTABLES.items():
                        steps += [
                            (
                                "SELECT create_hypertable(%s, %s, if_not_exists => TRUE, "
                                "migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days')",
                                (table, column),
                            ),
                            (
                                "SELECT add_retention_policy(%s, %s::interval, "
                                "if_not_exists => TRUE)",
                                (table, f"{self.retention_days} days"),
                            ),
                            (
                                f"ALTER TABLE {table} SET (timescaledb.compress, "
                                "timescaledb.compress_segmentby = 'series')",
                                (),
                            ),
                            (
                                "SELECT add_compression_policy(%s, INTERVAL '30 days', "
                                "if_not_exists => TRUE)",
                                (table,),
                            ),
                        ]
                    for statement, args in steps:
                        try:
                            conn.execute(statement, args or None)
                        except Exception as exc:  # already configured, missing rights, ...
                            logger.debug(
                                "History: TimescaleDB step skipped ({}): {}", statement[:40], exc
                            )
                try:
                    row = conn.execute(
                        "SELECT count(*) FROM timescaledb_information.hypertables "
                        "WHERE hypertable_schema = current_schema() AND hypertable_name = ANY(%s)",
                        (list(HYPERTABLES),),
                    ).fetchone()
                    self.timescale = bool(row and row[0] == len(HYPERTABLES))
                except Exception:
                    self.timescale = False
        self._schema_ready = True
        logger.info(
            "History: schema ready ({}).", "TimescaleDB" if self.timescale else "PostgreSQL"
        )

    # ---- writes -------------------------------------------------------------------

    def last_run_issued_at(self) -> Optional[datetime]:
        self.ensure_schema()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT max(issued_at) FROM eos_run")
            row = cur.fetchone()
        return row[0] if row and row[0] else None

    def write_run(
        self,
        issued_at: datetime,
        algorithm: str,
        duration_s: Optional[float],
        costs_amt: Optional[float],
        revenues_amt: Optional[float],
        losses_wh: Optional[float],
        baseline_net_amt: Optional[float],
        meta: Optional[dict[str, Any]],
        forecast_rows: Sequence[tuple[datetime, str, float, float]],
        plan_rows: Sequence[tuple[datetime, str, float, float]],
    ) -> int:
        """Insert a run with its forecast and plan rows; returns the run id."""
        import json

        self.ensure_schema()
        issued = _utc(issued_at)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO eos_run (issued_at, algorithm, duration_s, costs_amt, "
                    "revenues_amt, losses_wh, baseline_net_amt, meta) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id",
                    (
                        issued,
                        algorithm,
                        _finite(duration_s),
                        _finite(costs_amt),
                        _finite(revenues_amt),
                        _finite(losses_wh),
                        _finite(baseline_net_amt),
                        json.dumps(meta or {}, default=str),
                    ),
                )
                run_id = int(cur.fetchone()[0])
                for table, rows in (("eos_forecast", forecast_rows), ("eos_plan", plan_rows)):
                    if not rows:
                        continue
                    with cur.copy(
                        f"COPY {table} (target_time, issued_at, series, value, lead_h, run_id) "
                        "FROM STDIN"
                    ) as copy:
                        for target, series, value, lead in rows:
                            copy.write_row((target, issued, series, value, lead, run_id))
            conn.commit()
        return run_id

    def write_actuals(self, rows: Sequence[tuple[datetime, str, float]]) -> int:
        """Upsert hourly actuals (hour, series, value)."""
        if not rows:
            return 0
        self.ensure_schema()
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO eos_actual (hour, series, value) VALUES (%s, %s, %s) "
                    "ON CONFLICT (series, hour) DO UPDATE SET value = EXCLUDED.value",
                    [(_utc(h), s, v) for h, s, v in rows],
                )
            conn.commit()
        return len(rows)

    def write_accuracy(
        self, evaluated_at: datetime, days: int, results: Sequence[AccuracyResult]
    ) -> None:
        if not results:
            return
        self.ensure_schema()
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO eos_accuracy (evaluated_at, name, lead_h, days, n, mae, bias, "
                    "rmse, actual_mean) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    [
                        (
                            _utc(evaluated_at),
                            r.name,
                            r.lead_h,
                            days,
                            r.n,
                            r.mae,
                            r.bias,
                            r.rmse,
                            r.actual_mean,
                        )
                        for r in results
                    ],
                )
            conn.commit()

    def prune(self) -> None:
        """Retention for plain PostgreSQL (TimescaleDB does it with its policy)."""
        self.ensure_schema()
        if self.timescale:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        with self.connect() as conn:
            with conn.cursor() as cur:
                for table, column in (*HYPERTABLES.items(), ("eos_run", "issued_at")):
                    # Table and column names are module constants, not input.
                    cur.execute(f"DELETE FROM {table} WHERE {column} < %s", (cutoff,))  # noqa: S608
            conn.commit()

    # ---- reads --------------------------------------------------------------------

    def predicted_rows(
        self, source: str, series: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime, float]]:
        """(issued_at, target_time, value) of a forecast or plan series in [start, end)."""
        table = SERIES_TABLES[source]
        self.ensure_schema()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT issued_at, target_time, value FROM {table} "  # noqa: S608 (constant)
                "WHERE series = %s AND target_time >= %s AND target_time < %s",
                (series, _utc(start), _utc(end)),
            )
            return [(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    def actual_series(self, series: str, start: datetime, end: datetime) -> dict[datetime, float]:
        self.ensure_schema()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT hour, value FROM eos_actual WHERE series = %s AND hour >= %s AND hour < %s",
                (series, _utc(start), _utc(end)),
            )
            return {_utc(r[0]): float(r[1]) for r in cur.fetchall()}

    def latest_accuracy(self) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (name, lead_h) evaluated_at, name, lead_h, days, n, mae, bias, "
                "rmse, actual_mean FROM eos_accuracy ORDER BY name, lead_h, evaluated_at DESC"
            )
            keys = (
                "evaluated_at",
                "name",
                "lead_h",
                "days",
                "n",
                "mae",
                "bias",
                "rmse",
                "actual_mean",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def runs(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, issued_at, algorithm, duration_s, costs_amt, revenues_amt, "
                "losses_wh, baseline_net_amt, meta FROM eos_run "
                "WHERE issued_at >= %s AND issued_at < %s "
                "ORDER BY issued_at",
                (_utc(start), _utc(end)),
            )
            keys = (
                "run_id",
                "issued_at",
                "algorithm",
                "duration_s",
                "costs_amt",
                "revenues_amt",
                "losses_wh",
                "baseline_net_amt",
                "meta",
            )
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def daily_costs(
        self, start: datetime, end: datetime, timezone_name: str = "UTC"
    ) -> list[dict[str, Any]]:
        """Realised net grid costs per local day and the algorithm that planned it.

        Import and export energy come from the actuals ``grid_import_wh`` and
        ``grid_export_wh``; each hour is priced with the latest price / feed-in
        forecast issued before the hour (as the plan saw it). The algorithm of a day is
        the one of most stored runs that day (A/B operation).
        """
        self.ensure_schema()
        sql = DAILY_COSTS_SQL
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, {"start": _utc(start), "end": _utc(end), "tz": timezone_name})
            keys = ("day", "algorithm", "hours", "import_wh", "export_wh", "net_amt")
            return [
                {k: (round(float(v), 4) if isinstance(v, float) else v) for k, v in zip(keys, row)}
                for row in cur.fetchall()
            ]

    def hourly_comparison(
        self,
        pairs: Sequence[HistoryAccuracyPair],
        start: datetime,
        end: datetime,
        forecast_lead_h: float,
        plan_lead_h: float,
    ) -> list[dict[str, Any]]:
        """Predicted and actual value per hour for every pair (for charts).

        Forecast pairs use the newest forecast issued at least ``forecast_lead_h``
        before the hour, plan pairs the newest plan issued ``plan_lead_h`` before.
        Rows: ``{"hour": ISO, "<name>_actual": v, "<name>_predicted": v, ...}``.
        """
        rows: dict[datetime, dict[str, Any]] = {}
        for pair in pairs:
            lead = forecast_lead_h if pair.source == "forecast" else plan_lead_h
            predicted = pick_for_lead(
                hourly_predicted(
                    self.predicted_rows(
                        pair.source, pair.predicted, start - timedelta(days=3), end
                    ),
                    pair.kind,
                ),
                lead,
            )
            actual = self.actual_series(pair.actual, start, end)
            for hour in set(predicted) | set(actual):
                if not (start <= hour < end):
                    continue
                row = rows.setdefault(hour, {"hour": hour.isoformat()})
                if hour in actual:
                    row[f"{pair.name}_actual"] = round(actual[hour], 3)
                if hour in predicted:
                    row[f"{pair.name}_predicted"] = round(predicted[hour], 3)
        return [rows[h] for h in sorted(rows)]

    def evaluate(
        self,
        pairs: Sequence[HistoryAccuracyPair],
        lead_bands_h: Sequence[float],
        now: datetime,
        days: int,
    ) -> list[AccuracyResult]:
        """Accuracy of every pair and lead band over the last ``days`` complete hours."""
        end = _utc(now).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        results: list[AccuracyResult] = []
        for pair in pairs:
            rows = self.predicted_rows(pair.source, pair.predicted, start, end)
            hourly = hourly_predicted(rows, pair.kind)
            actual = self.actual_series(pair.actual, start, end)
            for lead in lead_bands_h:
                results.append(
                    accuracy(pair.name, float(lead), pick_for_lead(hourly, lead), actual)
                )
        return results


def _iso(value: Any) -> Any:
    """ISO text of a date/datetime, anything else unchanged."""
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def summary_from(
    accuracy_rows: Sequence[dict[str, Any]],
    runs: Sequence[dict[str, Any]],
    days: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Flat key figures for dashboards (Home Assistant REST sensor attributes).

    - ``kpi``: ``<name>_mae_<lead>h`` and ``<name>_bias_<lead>h`` per accuracy row,
    - ``plan``: the latest run (algorithm, duration, planned net costs, baseline, saving),
    - ``days``: realised net grid costs per local day with the planning algorithm,
    - ``ab``: number of days and mean daily net costs per algorithm (A/B comparison).
    """
    kpi: dict[str, Any] = {}
    for row in accuracy_rows:
        lead = float(row.get("lead_h") or 0)
        label = f"{row['name']}_%s_{lead:g}h"
        kpi[label % "mae"] = row.get("mae")
        kpi[label % "bias"] = row.get("bias")
    plan: dict[str, Any] = {}
    if runs:
        last = runs[-1]
        meta = last.get("meta") or {}
        issued = last.get("issued_at")
        plan = {
            "issued_at": _iso(issued),
            "algorithm": last.get("algorithm"),
            "duration_s": last.get("duration_s"),
            "baseline_net_amt": last.get("baseline_net_amt"),
            "plan_net_amt": meta.get("plan_net_amt"),
            "saving_vs_baseline_amt": meta.get("saving_vs_baseline_amt"),
        }
    day_rows = []
    ab: dict[str, dict[str, Any]] = {}
    for row in days:
        day = row.get("day")
        day_rows.append({**row, "day": _iso(day)})
        algorithm = row.get("algorithm")
        net = row.get("net_amt")
        if algorithm and net is not None and (row.get("hours") or 0) >= 20:
            entry = ab.setdefault(str(algorithm), {"days": 0, "sum": 0.0})
            entry["days"] += 1
            entry["sum"] += float(net)
    for entry in ab.values():
        entry["mean_net_amt"] = round(entry.pop("sum") / entry["days"], 4)
    return {"kpi": kpi, "plan": plan, "days": day_rows, "ab": ab}
