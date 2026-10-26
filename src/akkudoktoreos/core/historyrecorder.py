"""Glue between the energy management run and the long-term history (RL29 fork).

Called by ``EnergyManagement.run`` after a solution was published. Everything happens
in a background task and a worker thread, so a slow or unreachable database never
delays the control dispatch. Once an hour the hourly actuals of the last hours are
derived from the EOS measurements and the forecast accuracy is evaluated.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger

from akkudoktoreos.core.history import (
    AccuracyResult,
    HistorySettings,
    HistoryStore,
    HistoryUnavailable,
    _finite,
    _utc,
    baseline_self_consumption_cost,
    frame_rows,
)

ACTUALS_REFRESH = timedelta(hours=1)
ACTUALS_BACKFILL_HOURS = 6
"""Every hourly refresh rewrites the last few hours (late measurements, meter catch-up)."""


class HistoryRecorder:
    """Process-wide recorder; keeps the store and the refresh timestamps."""

    _instance: Optional["HistoryRecorder"] = None

    def __init__(self) -> None:
        self._store: Optional[HistoryStore] = None
        self._store_key: Optional[tuple[str, int]] = None
        self._last_run_stored: Optional[datetime] = None
        self._last_actuals: Optional[datetime] = None
        self._tasks: set[asyncio.Task] = set()
        self.last_accuracy: list[dict[str, Any]] = []
        self.last_error: Optional[str] = None

    @classmethod
    def instance(cls) -> "HistoryRecorder":
        if cls._instance is None:
            cls._instance = HistoryRecorder()
        return cls._instance

    # ---- store --------------------------------------------------------------------

    def store(self, settings: HistorySettings) -> Optional[HistoryStore]:
        if not settings.enabled or not settings.dsn:
            return None
        key = (settings.dsn, settings.retention_days)
        if self._store is None or self._store_key != key:
            try:
                self._store = HistoryStore(settings.dsn, settings.retention_days)
                self._store_key = key
            except HistoryUnavailable as exc:
                self._note_error(f"history disabled: {exc}")
                return None
        return self._store

    def _note_error(self, message: str) -> None:
        if message != self.last_error:
            logger.warning("History: {}", message)
        self.last_error = message

    # ---- entry point from the EMS -------------------------------------------------

    def schedule(
        self, config: Any, measurement: Any, solution: Any, algorithm: str, duration_s: float
    ) -> None:
        """Start the recording as a background task (never raises)."""
        try:
            settings: HistorySettings = config.database.history
            if not settings.enabled:
                return
            task = asyncio.get_running_loop().create_task(
                self.record(config, measurement, solution, algorithm, duration_s)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as exc:  # pragma: no cover - defensive
            self._note_error(f"cannot schedule recording: {exc}")

    async def record(
        self, config: Any, measurement: Any, solution: Any, algorithm: str, duration_s: float
    ) -> None:
        settings: HistorySettings = config.database.history
        store = self.store(settings)
        if store is None:
            return
        try:
            issued = _utc(solution.generated_at)
            interval = timedelta(minutes=settings.min_store_interval_minutes)
            if self._last_run_stored is not None and issued - self._last_run_stored < interval:
                pass
            else:
                payload = run_payload(
                    config, solution, algorithm, duration_s, settings.forecast_hours
                )
                await asyncio.to_thread(store.write_run, **payload)
                self._last_run_stored = issued
            now = datetime.now(timezone.utc)
            if self._last_actuals is None or now - self._last_actuals >= ACTUALS_REFRESH:
                rows = await derive_actuals(settings, measurement, now)
                await asyncio.to_thread(store.write_actuals, rows)
                results: list[AccuracyResult] = []
                if settings.accuracy:
                    results = await asyncio.to_thread(
                        store.evaluate,
                        settings.accuracy,
                        settings.lead_bands_h,
                        now,
                        settings.accuracy_days,
                    )
                    await asyncio.to_thread(
                        store.write_accuracy, now, settings.accuracy_days, results
                    )
                    for result in results:
                        if result.n:
                            logger.info(
                                "History accuracy {} lead {:g} h: n={} MAE={} bias={} ({} days)",
                                result.name,
                                result.lead_h,
                                result.n,
                                result.mae,
                                result.bias,
                                settings.accuracy_days,
                            )
                self.last_accuracy = [r.as_dict() for r in results]
                await asyncio.to_thread(store.prune)
                self._last_actuals = now
            self.last_error = None
        except Exception as exc:
            self._note_error(str(exc))

    async def drain(self) -> None:
        """Wait for pending recordings (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


# --------------------------------------------------------------------------------------
# Building the rows
# --------------------------------------------------------------------------------------


def _column(frame: Any, name: str) -> list[float]:
    if frame is None or name not in getattr(frame, "columns", []):
        return []
    return [(_finite(v) or 0.0) for v in frame[name].tolist()]


def battery_baseline_parameters(config: Any, solution_frame: Any) -> dict[str, float]:
    """Battery figures for the baseline from the first configured battery."""
    batteries = getattr(config.devices, "batteries", None) or {}
    for battery in batteries.values() if isinstance(batteries, dict) else batteries:
        capacity = float(getattr(battery, "capacity_wh", 0) or 0)
        if capacity <= 0:
            continue
        soc_col = f"{battery.device_id}_soc_factor"
        soc_values = _column(solution_frame, soc_col)
        soc_factor = soc_values[0] if soc_values else 0.0
        min_soc = float(getattr(battery, "min_soc_percentage", 0) or 0) / 100.0
        charge_w = float(getattr(battery, "max_charge_power_w", 0) or 0)
        discharge_w = float(getattr(battery, "max_discharge_power_w", None) or charge_w)
        return {
            "battery_capacity_wh": capacity,
            "battery_soc_wh": soc_factor * capacity,
            "max_charge_w": charge_w,
            "max_discharge_w": discharge_w,
            "charge_eff": float(getattr(battery, "charging_efficiency", 0.95) or 0.95),
            "discharge_eff": float(getattr(battery, "discharging_efficiency", 0.95) or 0.95),
            "min_soc_wh": min_soc * capacity,
        }
    return {}


def run_payload(
    config: Any, solution: Any, algorithm: str, duration_s: float, horizon_hours: int
) -> dict[str, Any]:
    """Arguments for ``HistoryStore.write_run`` from an ``OptimizationSolution``."""
    prediction = solution.prediction.to_dataframe()
    plan = solution.solution.to_dataframe()
    issued = _utc(solution.generated_at)
    forecast_rows = frame_rows(prediction, issued, horizon_hours)
    plan_rows = frame_rows(plan, issued, horizon_hours)

    # Baseline on the same horizon: the plan's own consumption (so only the battery /
    # grid strategy differs), PV and prices as forecast.
    baseline = None
    pv = _column(prediction, "pvforecast_ac_energy_wh") or _column(prediction, "pv_energy_wh")
    load = _column(plan, "load_energy_wh")
    price = _column(prediction, "elec_price_amt_kwh")
    feed_in = _column(prediction, "feed_in_tariff_amt_kwh")
    n = min(len(pv), len(load), len(price), len(feed_in), len(plan))
    if n:
        slot_h = 1.0
        if len(plan.index) > 1:
            slot_h = (plan.index[1] - plan.index[0]).total_seconds() / 3600.0
        battery = battery_baseline_parameters(config, plan)
        baseline = baseline_self_consumption_cost(
            pv[:n],
            load[:n],
            price[:n],
            feed_in[:n],
            battery_capacity_wh=battery.get("battery_capacity_wh", 0.0),
            battery_soc_wh=battery.get("battery_soc_wh", 0.0),
            max_charge_wh=battery.get("max_charge_w", 0.0) * slot_h,
            max_discharge_wh=battery.get("max_discharge_w", 0.0) * slot_h,
            charge_eff=battery.get("charge_eff", 0.95),
            discharge_eff=battery.get("discharge_eff", 0.95),
            min_soc_wh=battery.get("min_soc_wh", 0.0),
        )
    plan_costs = sum(_column(plan, "costs_amt")[:n]) if n else None
    plan_revenue = sum(_column(plan, "revenue_amt")[:n]) if n else None
    meta: dict[str, Any] = {
        "id": solution.id,
        "fitness": sorted(solution.fitness_score) if solution.fitness_score else [],
        "slots": len(plan),
    }
    if baseline is not None and plan_costs is not None and plan_revenue is not None:
        meta["baseline"] = baseline
        meta["plan_net_amt"] = round(plan_costs - plan_revenue, 4)
        meta["saving_vs_baseline_amt"] = round(baseline["net_amt"] - (plan_costs - plan_revenue), 4)
    return {
        "issued_at": issued,
        "algorithm": algorithm,
        "duration_s": duration_s,
        "costs_amt": solution.total_costs_amt,
        "revenues_amt": solution.total_revenues_amt,
        "losses_wh": solution.total_losses_energy_wh,
        "baseline_net_amt": baseline["net_amt"] if baseline else None,
        "meta": meta,
        "forecast_rows": forecast_rows,
        "plan_rows": plan_rows,
    }


async def derive_actuals(
    settings: HistorySettings, measurement: Any, now: datetime
) -> list[tuple[datetime, str, float]]:
    """Hourly actuals of the complete hours in the backfill window."""
    from akkudoktoreos.utils.datetimeutil import to_datetime, to_duration

    end_utc = _utc(now).replace(minute=0, second=0, microsecond=0)
    start_utc = end_utc - timedelta(hours=ACTUALS_BACKFILL_HOURS)
    start = to_datetime(start_utc)
    end = to_datetime(end_utc)
    hour = to_duration("1 hour")
    hours = [start_utc + timedelta(hours=i) for i in range(ACTUALS_BACKFILL_HOURS)]
    derived: dict[str, list[Optional[float]]] = {}
    rows: list[tuple[datetime, str, float]] = []
    for source in settings.actuals:
        values: list[Optional[float]] = [0.0] * len(hours)
        try:
            for key in source.keys:
                if source.kind == "meter":
                    array = await measurement._energy_from_meter_readings(key, start, end, hour)
                    readings = await measurement.key_to_lists(key, start, end)
                    if not readings[0]:
                        values = [None] * len(hours)
                        break
                else:
                    array = await measurement.key_to_array(
                        key=key,
                        start_datetime=start,
                        end_datetime=end,
                        interval=hour,
                        fill_method="time",
                        boundary="context",
                    )
                for i in range(len(hours)):
                    raw = _finite(array[i]) if i < len(array) else None
                    if source.kind == "meter" and raw is not None and raw < 0:
                        raw = None  # meter reset or replaced
                    if raw is None or values[i] is None:
                        values[i] = None
                    else:
                        values[i] = (values[i] or 0.0) + raw
        except Exception as exc:
            logger.debug("History: actual '{}' not derived: {}", source.name, exc)
            values = [None] * len(hours)
        scaled: list[Optional[float]] = []
        for i, value in enumerate(values):
            if value is None:
                scaled.append(None)
                continue
            value *= source.scale
            for other in source.subtract:
                other_values = derived.get(other)
                if not other_values or other_values[i] is None:
                    value = None
                    break
                value -= other_values[i]  # type: ignore[operator]
            scaled.append(value)
        derived[source.name] = scaled
        for stamp, value in zip(hours, scaled):
            if value is not None:
                rows.append((stamp, source.name, round(value, 3)))
    return rows
