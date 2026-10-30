"""Decision logic of EOS Control, free of Home Assistant imports (unit-testable).

The coordinator collects the state (plan, rooms, holiday, ...) into a
``ControlInput`` and calls :func:`decide`. The result says what the heat pump
should do and why. Nothing is written here; Home Assistant automations switch
the devices from the published targets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Optional

# EOS heating modes (optimization.heating_modes)
REDUCED, NORMAL, RAISED, PREHEAT, FORCED, COOL = range(6)
MODE_NAMES = {
    REDUCED: "REDUCED",
    NORMAL: "NORMAL",
    RAISED: "RAISED",
    PREHEAT: "PREHEAT",
    FORCED: "FORCED",
    COOL: "COOL",
}

DEFAULT_SG = {REDUCED: 1, NORMAL: 2, RAISED: 3, PREHEAT: 3, FORCED: 4}


@dataclass
class PlanSlot:
    """One slot of the EOS plan for the configured device."""

    start: datetime
    heating_mode: Optional[int] = None
    dhw_mode: Optional[float] = None
    away: bool = False
    zone_setback: bool = False
    standby: Optional[bool] = None
    """Heat pump in standby according to EOS (``<device>_standby``); None = older EOS
    without the column, the standby is then derived from REDUCED without hot water."""


@dataclass
class Plan:
    """EOS plan of one heat pump."""

    generated_at: Optional[datetime]
    slots: list[PlanSlot] = field(default_factory=list)

    def current(self, now: datetime) -> Optional[PlanSlot]:
        """Slot that contains ``now`` (the last slot starting at or before now)."""
        current = None
        for slot in self.slots:
            if slot.start <= now:
                current = slot
            else:
                break
        return current

    def dhw_charge_within(self, now: datetime, hours: float) -> bool:
        """A hot water charge is planned now or within ``hours``."""
        until = now + timedelta(hours=hours)
        slot_now = self.current(now)
        for slot in self.slots:
            if (slot is slot_now or now <= slot.start <= until) and (slot.dhw_mode or 0) >= 0.5:
                return True
        return False


def parse_plan(solution: dict[str, Any], device_id: str) -> Plan:
    """Plan of ``device_id`` from the EOS solution (``/v1/energy-management/optimization/solution``).

    Raises:
        ValueError: The solution does not contain the device.
    """
    generated = _parse_dt(solution.get("generated_at"))
    rows = (solution.get("solution") or {}).get("data") or {}
    key = f"{device_id}_heating_mode"
    slots: list[PlanSlot] = []
    for stamp in sorted(rows):
        row = rows[stamp] or {}
        start = _parse_dt(stamp)
        if start is None:
            continue
        mode = _number(row.get(key))
        slots.append(
            PlanSlot(
                start=start,
                heating_mode=int(round(mode)) if mode is not None else None,
                dhw_mode=_number(row.get(f"{device_id}_dhw_mode")),
                away=(_number(row.get(f"{device_id}_away")) or 0.0) >= 0.5,
                zone_setback=(_number(row.get(f"{device_id}_zone_setback")) or 0.0) >= 0.5,
                standby=_flag(row.get(f"{device_id}_standby")),
            )
        )
    if slots and all(s.heating_mode is None for s in slots):
        raise ValueError(f"EOS plan contains no heat pump '{device_id}'.")
    slots.sort(key=lambda s: s.start)
    return Plan(generated_at=generated, slots=slots)


@dataclass
class RoomState:
    """One room as seen by the controller."""

    name: str
    temperature: Optional[float]
    lower_c: float
    upper_c: float
    airing: bool = False
    """Window/door open or within the run-on after closing."""
    has_windows: bool = False
    window_open: bool = False
    """A window/door of the room is open now."""
    in_zone: bool = False
    """Room belongs to the set-back zone of EOS (``zone_setback``)."""
    humidity_high: bool = False
    """Humidity at or above the room's limit for longer than its delay."""
    humidity_pct: Optional[float] = None
    key: str = ""
    """Subentry id of the room (for its entities)."""


@dataclass
class Settings:
    """Options of the integration that the decision uses."""

    sg_by_mode: dict[int, int] = field(default_factory=lambda: dict(DEFAULT_SG))
    plan_max_age_min: float = 45.0
    reduction_hysteresis_k: float = 0.5
    min_valid_rooms: int = 1
    max_room_c: float = 23.5
    holiday_floor_c: float = 16.0
    holiday_restart_hours: float = 1.0
    holiday_dhw_lead_hours: float = 3.0


@dataclass
class ControlInput:
    """Everything :func:`decide` needs, collected by the coordinator."""

    now: datetime
    plan: Optional[Plan]
    rooms: list[RoomState]
    heating_season: bool = True
    holiday: bool = False
    holiday_return: Optional[datetime] = None
    reduced_setpoint_c: Optional[float] = None
    """Reduced room setpoint of the heating circuit (holiday temperature while away)."""
    zone_setback_allowed: bool = True
    sg_actual: Optional[int] = None
    reduction_locked_until: Optional[datetime] = None


@dataclass
class Decision:
    """What the heat pump should do, with the reason."""

    sg_target: int
    reason: str
    eos_mode: Optional[int] = None
    eos_fresh: bool = False
    standby: bool = False
    standby_reason: str = ""
    dhw_charge: bool = False
    dhw_setpoints_restore: bool = False
    room_mean_c: Optional[float] = None
    room_min_margin_k: Optional[float] = None
    room_max_margin_k: Optional[float] = None
    coldest_room: Optional[str] = None
    warmest_room: Optional[str] = None
    warmest_temp: Optional[float] = None
    cold_rooms: list[str] = field(default_factory=list)
    window_open_share: Optional[float] = None
    humidity_max_pct: Optional[float] = None
    humidity_warning: bool = False
    valid_rooms: int = 0
    pending: Optional[str] = None
    """A changed decision that is not accepted yet (see :class:`Settle`)."""

    def outputs(self) -> tuple[int, bool, bool, bool]:
        """What is switched: SG target, standby, hot water charge, setpoint restore."""
        return (self.sg_target, self.standby, self.dhw_charge, self.dhw_setpoints_restore)

    def as_attributes(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "pending": self.pending,
            "eos_mode": MODE_NAMES.get(self.eos_mode, None) if self.eos_mode is not None else None,
            "eos_fresh": self.eos_fresh,
            "standby": self.standby,
            "standby_reason": self.standby_reason,
            "dhw_charge": self.dhw_charge,
            "room_mean_c": _round(self.room_mean_c),
            "room_min_margin_k": _round(self.room_min_margin_k),
            "room_max_margin_k": _round(self.room_max_margin_k),
            "coldest_room": self.coldest_room,
            "warmest_room": self.warmest_room,
            "cold_rooms": self.cold_rooms,
            "window_open_share": _round(self.window_open_share, 2),
            "humidity_max_pct": _round(self.humidity_max_pct),
            "humidity_warning": self.humidity_warning,
            "valid_rooms": self.valid_rooms,
        }


@dataclass
class Settle:
    """Accept a changed decision only after it has stayed the same for ``seconds``.

    EOS writes a new plan slot by slot and a replan or a sensor update can flip the
    result for a moment; a switch of the heat pump should not follow such a blip.
    Until the change is accepted the previous outputs (and their reasons) stay in
    force and the new result is shown as ``pending``. The first decision after a
    start is accepted at once; ``seconds`` = 0 accepts every change at once.
    """

    seconds: float = 10.0
    accepted: Optional[Decision] = None
    pending: Optional[tuple[int, bool, bool, bool]] = None
    since: Optional[datetime] = None

    def apply(self, decision: Decision, now: datetime) -> tuple[Decision, Optional[float]]:
        """Decision to publish and the seconds until the pending change can be accepted."""
        accepted = self.accepted
        outputs = decision.outputs()
        if accepted is None or self.seconds <= 0 or outputs == accepted.outputs():
            return self._accept(decision), None
        if self.pending != outputs or self.since is None:
            self.pending, self.since = outputs, now
        waited = (now - self.since).total_seconds()
        if waited >= self.seconds:
            return self._accept(decision), None
        remaining = self.seconds - waited
        held = replace(
            decision,
            sg_target=accepted.sg_target,
            standby=accepted.standby,
            dhw_charge=accepted.dhw_charge,
            dhw_setpoints_restore=accepted.dhw_setpoints_restore,
            reason=accepted.reason,
            standby_reason=accepted.standby_reason,
            pending=f"{_change(decision, accepted)} (in {remaining:.0f} s)",
        )
        return held, remaining

    def _accept(self, decision: Decision) -> Decision:
        self.accepted = decision
        self.pending = self.since = None
        return decision


def _change(new: Decision, old: Decision) -> str:
    parts = []
    if (new.sg_target, new.dhw_charge, new.dhw_setpoints_restore) != (
        old.sg_target,
        old.dhw_charge,
        old.dhw_setpoints_restore,
    ):
        parts.append(new.reason)
    if new.standby != old.standby:
        parts.append(new.standby_reason)
    return "; ".join(p for p in parts if p) or new.reason


def decide(data: ControlInput, settings: Settings) -> Decision:
    """SG Ready target, holiday standby and hot water charge for ``data``."""
    plan = data.plan
    slot = plan.current(data.now) if plan is not None else None
    age_min = (
        (data.now - plan.generated_at).total_seconds() / 60.0
        if plan is not None and plan.generated_at is not None
        else math.inf
    )
    fresh = (
        slot is not None and slot.heating_mode is not None and age_min <= settings.plan_max_age_min
    )
    mode = slot.heating_mode if slot is not None else None
    plan_knows_away = bool(slot is not None and slot.away)

    decision = Decision(sg_target=2, reason="", eos_mode=mode, eos_fresh=fresh)
    _room_figures(data, settings, decision)

    # -- SG Ready target ------------------------------------------------
    if not fresh:
        decision.reason = (
            "NORMAL: no EOS plan"
            if plan is None or slot is None
            else f"NORMAL: EOS plan {age_min:.0f} min old (> {settings.plan_max_age_min:.0f} min)"
        )
    elif not data.heating_season:
        decision.reason = "NORMAL: not in heating operation"
    elif mode is not None:  # always set when fresh
        if data.holiday and mode <= NORMAL:
            sg = 1
        else:
            sg = settings.sg_by_mode.get(mode, 2)
        decision.sg_target = sg
        decision.reason = f"{MODE_NAMES.get(mode, mode)} -> SG {sg}"
        if sg == 1:
            blocked = _reduction_blocked(data, settings, decision)
            if blocked:
                decision.sg_target = 2
                decision.reason = f"NORMAL instead of {MODE_NAMES.get(mode, mode)}: {blocked}"
        elif sg >= 3 and decision.warmest_temp is not None:
            if decision.warmest_temp >= settings.max_room_c:
                decision.sg_target = 2
                decision.reason = (
                    f"NORMAL instead of {MODE_NAMES.get(mode, mode)}: {decision.warmest_room} "
                    f"{decision.warmest_temp:.1f} degC >= {settings.max_room_c:.1f} degC"
                )

    # -- Holiday: standby and hot water ----------------------------------
    if data.holiday:
        restart = data.holiday_return is not None and data.now >= data.holiday_return - timedelta(
            hours=settings.holiday_restart_hours
        )
        temps = [r.temperature for r in data.rooms if r.temperature is not None]
        too_cold = bool(temps) and min(temps) < settings.holiday_floor_c
        eos_ok = fresh and plan_knows_away
        dhw_planned = bool(slot is not None and (slot.dhw_mode or 0) >= 0.5)
        if too_cold:
            decision.standby_reason = f"no standby: coldest room {min(temps):.1f} degC < {settings.holiday_floor_c:.1f} degC"
        elif eos_ok and slot is not None and slot.standby is not None:
            decision.standby = slot.standby
            decision.standby_reason = (
                "standby: EOS plans standby"
                if decision.standby
                else f"no standby: EOS plans {_mode_name(mode)}"
                + (" with hot water" if dhw_planned else "")
            )
        elif eos_ok:
            decision.standby = mode == REDUCED and not dhw_planned
            decision.standby_reason = (
                "standby: EOS plans REDUCED without hot water"
                if decision.standby
                else f"no standby: EOS plans {_mode_name(mode)}"
                + (" with hot water" if dhw_planned else "")
            )
        elif restart:
            decision.standby_reason = "no standby: restart before the return, EOS missing"
        else:
            decision.standby = True
            decision.standby_reason = (
                "standby: EOS plan without holiday ignored" if fresh else "standby: EOS missing"
            )
        decision.dhw_setpoints_restore = restart or (
            eos_ok
            and plan is not None
            and plan.dhw_charge_within(data.now, settings.holiday_dhw_lead_hours)
        )
        decision.dhw_charge = eos_ok and dhw_planned
    else:
        decision.dhw_charge = fresh and slot is not None and (slot.dhw_mode or 0) >= 0.5
    return decision


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _zone_active(data: ControlInput) -> bool:
    slot = data.plan.current(data.now) if data.plan is not None else None
    return bool(data.zone_setback_allowed and slot is not None and slot.zone_setback)


def _lower_limit(
    room: RoomState, data: ControlInput, settings: Settings, zone_active: bool
) -> float:
    """Lower limit of ``room`` valid now (holiday and set-back zone use the reduced setpoint)."""
    if data.holiday:
        return (
            data.reduced_setpoint_c - 1.0
            if data.reduced_setpoint_c is not None
            else settings.holiday_floor_c
        )
    if zone_active and room.in_zone and data.reduced_setpoint_c is not None:
        return data.reduced_setpoint_c - 1.0
    return room.lower_c


def _room_figures(data: ControlInput, settings: Settings, decision: Decision) -> None:
    valid = [r for r in data.rooms if r.temperature is not None]
    decision.valid_rooms = len(valid)
    if not valid:
        return
    decision.room_mean_c = sum(r.temperature for r in valid) / len(valid)  # type: ignore[misc]
    considered = [r for r in valid if not r.airing]
    if considered:
        zone = _zone_active(data)
        # Margins against the limits valid now (EOS applies them until the return).
        # While away the heat pump may cool the house down to the holiday floor
        # (standby), so the margin is taken against that floor.
        decision.room_min_margin_k = min(
            r.temperature  # type: ignore[operator]
            - (settings.holiday_floor_c if data.holiday else _lower_limit(r, data, settings, zone))
            for r in considered
        )
        decision.room_max_margin_k = min(
            r.upper_c - r.temperature  # type: ignore[operator]
            for r in considered
        )
        coldest = min(considered, key=lambda r: r.temperature)  # type: ignore[arg-type,return-value]
        decision.coldest_room = coldest.name
        warmest = max(valid, key=lambda r: r.temperature)  # type: ignore[arg-type,return-value]
        decision.warmest_room = warmest.name
        decision.warmest_temp = warmest.temperature
    windowed = [r for r in data.rooms if r.has_windows]
    decision.window_open_share = (
        sum(1 for r in windowed if r.window_open) / len(windowed) if windowed else None
    )
    humid = [r.humidity_pct for r in data.rooms if r.humidity_pct is not None]
    decision.humidity_max_pct = max(humid) if humid else None
    decision.humidity_warning = any(r.humidity_high for r in data.rooms)


def _reduction_blocked(data: ControlInput, settings: Settings, decision: Decision) -> str:
    """Reason why REDUCED (SG 1) must not run now, empty = allowed."""
    if decision.valid_rooms < settings.min_valid_rooms:
        return f"only {decision.valid_rooms} room values (< {settings.min_valid_rooms})"
    if decision.humidity_warning:
        return "humidity warning"
    if (
        not data.holiday
        and data.reduction_locked_until is not None
        and data.now < data.reduction_locked_until
    ):
        return f"reduction locked until {data.reduction_locked_until:%H:%M}"
    running = data.sg_actual == 1
    zone_active = _zone_active(data)
    cold: list[str] = []
    for room in data.rooms:
        if room.temperature is None or room.airing:
            continue
        limit = _lower_limit(room, data, settings, zone_active)
        if not data.holiday and not (zone_active and room.in_zone):
            limit += 0.0 if running else settings.reduction_hysteresis_k
        if room.temperature < limit:
            cold.append(f"{room.name} {room.temperature:.1f} < {limit:.1f}")
    decision.cold_rooms = cold
    if cold:
        return "too cold: " + ", ".join(cold)
    return ""


def _mode_name(mode: Optional[int]) -> str:
    return MODE_NAMES.get(mode, str(mode)) if mode is not None else "nothing"


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _flag(value: Any) -> Optional[bool]:
    number = _number(value)
    return None if number is None else number >= 0.5


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)
