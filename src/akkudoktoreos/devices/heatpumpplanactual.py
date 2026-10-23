"""Plan versus actual for the heat pump (RL29 fork).

The feedback loop the calibration alone does not give: every optimization run
records the first hours of its heat pump plan (room, screed and tank
temperature, electric energy, heating mode per slot). Once an hour the plans
of the last day are compared with what the measurements say happened:

- **room / tank temperature**: planned value at the end of a slot against the
  measured mean around that moment (bias and mean absolute error),
- **electric energy**: planned Wh against the measured mean power of the slot,

each for two lead times: the plan issued shortly before the slot (how well the
controller follows the model) and the plan issued hours earlier (how well the
model predicts). The result goes to the log and to
``<data folder>/rl29/heatpump_plan_actual.json``; a growing room bias is the
cue to look at the building parameters, a wrong energy ratio at the COP.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from loguru import logger

from akkudoktoreos.devices.heatpumpcalibration import _as_datetime, _measurement_series
from akkudoktoreos.devices.thermal.calibration import hourly_means

RECORD_HOURS = 24
"""Hours of every plan that are kept."""

KEEP_HOURS = 36
"""Plans older than this are dropped (every run records one; 36 h cover the 6 h lead)."""

LEADS_H = (1.0, 6.0)
"""Lead times evaluated: the plan issued at least this many hours before a slot."""

PLAN_KEYS = ("room_c", "slab_c", "dhw_c", "electric_wh", "heating_mode", "dhw_mode")


class PlanActualStore:
    """Recorded plans and the last evaluation per device, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"devices": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {"devices": {}}
        self.data.setdefault("devices", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _device(self, device_id: str) -> dict[str, Any]:
        return self.data["devices"].setdefault(device_id, {"plans": [], "evaluation": None})

    def plans(self, device_id: str) -> list[dict[str, Any]]:
        return list(self._device(device_id).get("plans") or [])

    def evaluation(self, device_id: str) -> Optional[dict[str, Any]]:
        return self._device(device_id).get("evaluation")

    def record(
        self,
        device_id: str,
        issued_at: datetime,
        slot_starts: Sequence[Any],
        slot_hours: float,
        result: dict[str, Any],
    ) -> None:
        """Keep the first ``RECORD_HOURS`` of a plan; drop plans older than ``KEEP_HOURS``."""
        issued = _as_datetime(issued_at)
        slots = min(len(slot_starts), max(1, int(round(RECORD_HOURS / slot_hours))))
        rows: dict[str, list[Any]] = {}
        for key in PLAN_KEYS:
            values = result.get(key)
            if isinstance(values, list):
                rows[key] = [
                    (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                    for v in values[:slots]
                ]
        if not rows or not slot_starts:
            return
        entry = {
            "issued_at": issued.isoformat(),
            "start": _as_datetime(slot_starts[0]).isoformat(),
            "slot_hours": float(slot_hours),
            "rows": rows,
        }
        device = self._device(device_id)
        plans = [
            p
            for p in device.get("plans") or []
            if (issued - _as_datetime(p["issued_at"])).total_seconds() < KEEP_HOURS * 3600
            and p.get("issued_at") != entry["issued_at"]
        ]
        plans.append(entry)
        device["plans"] = plans

    def set_evaluation(self, device_id: str, evaluation: dict[str, Any]) -> None:
        self._device(device_id)["evaluation"] = evaluation


def _plan_value(plan: dict[str, Any], key: str, slot_start: datetime) -> Optional[float]:
    """Planned value of ``key`` for the slot starting at ``slot_start``, if covered."""
    start = _as_datetime(plan["start"])
    slot_hours = float(plan["slot_hours"])
    index = (slot_start - start).total_seconds() / 3600.0 / slot_hours
    if index < -1e-6 or abs(index - round(index)) > 1e-6:
        return None
    values = plan.get("rows", {}).get(key)
    i = int(round(index))
    if values is None or i >= len(values):
        return None
    value = values[i]
    return float(value) if isinstance(value, (int, float)) else None


def _plan_for(
    plans: Sequence[dict[str, Any]], slot_start: datetime, lead_h: float
) -> Optional[dict[str, Any]]:
    """The latest plan issued at least ``lead_h`` before ``slot_start``."""
    latest = None
    for plan in plans:
        issued = _as_datetime(plan["issued_at"])
        if (slot_start - issued).total_seconds() >= lead_h * 3600 - 1:
            if latest is None or issued > _as_datetime(latest["issued_at"]):
                latest = plan
    return latest


def _mean_around(
    series: list[tuple[datetime, float]], moment: datetime, half: timedelta
) -> Optional[float]:
    means = hourly_means(series, moment - half, moment + half, min_coverage_seconds=0)
    if not means:
        return None
    return sum(means.values()) / len(means)


async def evaluate_plans(
    device: Any,
    store: PlanActualStore,
    measurement: Any,
    now: datetime,
    hours: int = RECORD_HOURS,
    leads_h: Sequence[float] = LEADS_H,
) -> Optional[dict[str, Any]]:
    """Compare the recorded plans of the last ``hours`` with the measurements."""
    plans = store.plans(device.device_id)
    if not plans:
        return None
    end = _as_datetime(now)
    start = end - timedelta(hours=hours + 1)
    keys = device.calibration_measurement_keys
    room = await _measurement_series(measurement, keys["room_c"], start, end)
    dhw = await _measurement_series(measurement, keys["dhw_c"], start, end)
    electric = await _measurement_series(measurement, keys["electric_w"], start, end)
    # Airing (RL29 fork): the room air drops and recovers within the hour; the model
    # only knows the expected airing, so slots near open windows are not compared.
    windows = await _measurement_series(
        measurement, keys["window_open"], start - timedelta(hours=1), end
    )
    slot_hours = float(plans[-1]["slot_hours"])
    first_slot = (end - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    half = timedelta(minutes=30)

    evaluation: dict[str, Any] = {"evaluated_at": end.isoformat(), "hours": hours, "leads": {}}
    for lead in leads_h:
        room_err: list[float] = []
        dhw_err: list[float] = []
        planned_wh = 0.0
        measured_wh = 0.0
        energy_slots = 0
        slot = first_slot
        while slot + timedelta(hours=slot_hours) <= end:
            plan = _plan_for(plans, slot, lead)
            if plan is not None:
                slot_end = slot + timedelta(hours=slot_hours)
                p_room = _plan_value(plan, "room_c", slot)
                aired = False
                if windows and p_room is not None:
                    # the slot plus the half hour around its end that the room
                    # comparison averages over
                    opened = hourly_means(
                        windows, slot - half, slot_end + half, min_coverage_seconds=0
                    )
                    aired = any(v > 0.01 for v in opened.values())
                m_room = (
                    _mean_around(room, slot_end, half) if p_room is not None and not aired else None
                )
                if p_room is not None and m_room is not None:
                    room_err.append(p_room - m_room)
                p_dhw = _plan_value(plan, "dhw_c", slot)
                m_dhw = _mean_around(dhw, slot_end, half) if p_dhw is not None else None
                if p_dhw is not None and m_dhw is not None:
                    dhw_err.append(p_dhw - m_dhw)
                p_wh = _plan_value(plan, "electric_wh", slot)
                if p_wh is not None and electric:
                    means = hourly_means(electric, slot, slot_end, min_coverage_seconds=0)
                    if means:
                        measured_wh += sum(means.values()) / len(means) * slot_hours
                        planned_wh += p_wh
                        energy_slots += 1
            slot += timedelta(hours=slot_hours)

        def stats(errors: list[float]) -> dict[str, Any]:
            if not errors:
                return {"slots": 0, "bias_k": None, "mae_k": None}
            return {
                "slots": len(errors),
                "bias_k": round(sum(errors) / len(errors), 2),
                "mae_k": round(sum(abs(e) for e in errors) / len(errors), 2),
            }

        evaluation["leads"][f"{lead:g}h"] = {
            "room": stats(room_err),
            "dhw": stats(dhw_err),
            "energy": {
                "slots": energy_slots,
                "planned_wh": round(planned_wh),
                "measured_wh": round(measured_wh),
                "ratio": round(measured_wh / planned_wh, 3) if planned_wh > 0 else None,
            },
        }
    store.set_evaluation(device.device_id, evaluation)
    return evaluation


def summary(evaluation: dict[str, Any]) -> str:
    """One line for the log."""
    parts = []
    for lead, values in evaluation.get("leads", {}).items():
        room, dhw, energy = values["room"], values["dhw"], values["energy"]
        piece = f"lead {lead}:"
        if room["slots"]:
            piece += f" room bias {room['bias_k']:+.2f} K, MAE {room['mae_k']:.2f} K ({room['slots']} h);"
        if dhw["slots"]:
            piece += f" tank bias {dhw['bias_k']:+.2f} K, MAE {dhw['mae_k']:.2f} K;"
        if energy["slots"]:
            piece += (
                f" energy planned {energy['planned_wh'] / 1000:.1f} kWh, "
                f"measured {energy['measured_wh'] / 1000:.1f} kWh"
            )
            if energy["ratio"] is not None:
                piece += f" (x{energy['ratio']:.2f})"
        parts.append(piece.rstrip(";"))
    return " | ".join(parts) if parts else "no overlapping plans and measurements yet"


async def evaluate_device(
    device: Any,
    measurement: Any,
    observation_time: datetime,
    data_folder: Path,
    every_hours: float = 1.0,
) -> Optional[dict[str, Any]]:
    """Evaluate at most every ``every_hours``; returns the current evaluation."""
    store = PlanActualStore(Path(data_folder) / "rl29" / "heatpump_plan_actual.json")
    previous = store.evaluation(device.device_id)
    now = _as_datetime(observation_time)
    if previous is not None:
        age_h = (now - _as_datetime(previous["evaluated_at"])).total_seconds() / 3600.0
        if 0 <= age_h < every_hours:
            return previous
    try:
        evaluation = await evaluate_plans(device, store, measurement, now)
    except (OSError, ValueError, ArithmeticError) as exc:
        logger.warning("Heat pump {}: plan/actual evaluation failed: {}", device.device_id, exc)
        return previous
    if evaluation is None:
        return previous
    store.save()
    logger.info(
        "Heat pump {} plan vs actual (last {} h): {}",
        device.device_id,
        evaluation["hours"],
        summary(evaluation),
    )
    return evaluation


def record_plan(
    device_id: str,
    data_folder: Path,
    issued_at: datetime,
    slot_starts: Sequence[Any],
    slot_hours: float,
    result: dict[str, Any],
) -> None:
    """Record a plan from the optimizer; never raises."""
    try:
        store = PlanActualStore(Path(data_folder) / "rl29" / "heatpump_plan_actual.json")
        store.record(device_id, issued_at, slot_starts, slot_hours, result)
        store.save()
    except Exception as exc:  # recording is diagnostics, never part of the run
        logger.debug("Heat pump {}: could not record the plan: {}", device_id, exc)
