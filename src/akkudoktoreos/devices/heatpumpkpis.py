"""Plan-versus-actual and calibration figures of a heat pump as solution columns (RL29 fork).

The figures are already in ``<data folder>/rl29/heatpump_plan_actual.json`` and
``heatpump_calibration.json`` and in the log. Published as constant columns of
the optimization solution, the Home Assistant adapter can push them like every
other solution value (``sensor.eos_<device>_kpi_<name>``), so drift shows up on
a dashboard and in the long-term statistics.

A figure stays empty (``unknown`` in Home Assistant) until enough data exists:
plan-versus-actual needs ``MIN_SLOTS`` compared slots, a calibration figure the
corresponding fit. Nothing has to be switched on later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from akkudoktoreos.devices.heatpumpcalibration import HeatPumpCalibrationStore
from akkudoktoreos.devices.heatpumpplanactual import PlanActualStore
from akkudoktoreos.devices.thermal.calibration import CalibrationResult

MIN_SLOTS = 12
"""Compared slots before a plan-versus-actual figure is published."""


def _lead(evaluation: Optional[dict[str, Any]], lead: str, part: str, key: str) -> Optional[float]:
    if not evaluation:
        return None
    values = evaluation.get("leads", {}).get(lead, {}).get(part, {})
    if (values.get("slots") or 0) < MIN_SLOTS:
        return None
    value = values.get(key)
    return float(value) if value is not None else None


def _fit(
    result: Optional[CalibrationResult], fit: str, key: str, factor: float = 1.0
) -> Optional[float]:
    part = getattr(result, fit, None) if result is not None else None
    value = getattr(part, key, None) if part is not None else None  # None: not fitted yet
    return float(value) * factor if value is not None else None


# (column suffix, getter(evaluation, calibration result))
KPI_COLUMNS: list[tuple[str, Callable[[Any, Any], Optional[float]]]] = [
    ("kpi_room_bias_1h_k", lambda e, r: _lead(e, "1h", "room", "bias_k")),
    ("kpi_room_mae_1h_k", lambda e, r: _lead(e, "1h", "room", "mae_k")),
    ("kpi_room_bias_6h_k", lambda e, r: _lead(e, "6h", "room", "bias_k")),
    ("kpi_room_mae_6h_k", lambda e, r: _lead(e, "6h", "room", "mae_k")),
    ("kpi_dhw_bias_6h_k", lambda e, r: _lead(e, "6h", "dhw", "bias_k")),
    ("kpi_energy_ratio_6h", lambda e, r: _lead(e, "6h", "energy", "ratio")),
    ("kpi_loss_w_per_k", lambda e, r: _fit(r, "building", "loss_w_per_k")),
    ("kpi_solar_aperture_m2", lambda e, r: _fit(r, "building", "solar_aperture_m2")),
    ("kpi_internal_gains_w", lambda e, r: _fit(r, "building", "internal_gains_w")),
    ("kpi_building_days", lambda e, r: _fit(r, "building", "days")),
    ("kpi_cop_scale", lambda e, r: _fit(r, "cop", "cop_scale")),
    ("kpi_measured_cop", lambda e, r: _fit(r, "cop", "measured_cop")),
    ("kpi_emitter_w_per_k", lambda e, r: _fit(r, "emitter", "emitter_w_per_k")),
    ("kpi_dhw_loss_w_per_k", lambda e, r: _fit(r, "dhw", "loss_w_per_k")),
    ("kpi_draw_kwh_per_day", lambda e, r: _fit(r, "draw", "daily_wh", 0.001)),
    ("kpi_draw_days", lambda e, r: _fit(r, "draw", "days")),
]


def device_kpis(device_id: str, data_folder: Path) -> dict[str, Optional[float]]:
    """All KPI columns of a device (suffix -> value or None)."""
    folder = Path(data_folder) / "rl29"
    evaluation = PlanActualStore(folder / "heatpump_plan_actual.json").evaluation(device_id)
    result = HeatPumpCalibrationStore(folder / "heatpump_calibration.json").result(device_id)
    kpis = {name: getter(evaluation, result) for name, getter in KPI_COLUMNS}
    fitted = result is not None and any(
        (result.building, result.cop, result.emitter, result.dhw, result.draw)
    )
    kpis["kpi_calibrated"] = 1.0 if fitted else 0.0
    return kpis
