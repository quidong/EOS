"""Coordinator: reads EOS and Home Assistant states, decides, compares (shadow mode)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AIRING_RUN_ON,
    CONF_DEVICE_ID,
    CONF_HEATING_SEASON_ENTITY,
    CONF_HEATING_SEASON_STATES,
    CONF_HOLIDAY_DHW_LEAD,
    CONF_HOLIDAY_ENTITY,
    CONF_HOLIDAY_FLOOR,
    CONF_HOLIDAY_RESTART,
    CONF_HOLIDAY_RETURN_ENTITY,
    CONF_HUMIDITY_DELAY,
    CONF_HUMIDITY_ENTITY,
    CONF_HUMIDITY_MAX,
    CONF_HYSTERESIS,
    CONF_IN_ZONE,
    CONF_LOWER,
    CONF_MAX_ROOM,
    CONF_MIN_VALID_ROOMS,
    CONF_NAME,
    CONF_PLAN_MAX_AGE,
    CONF_REDUCED_SETPOINT_ENTITY,
    CONF_REDUCTION_LOCK_ENTITY,
    CONF_SCAN_INTERVAL,
    CONF_SETPOINT_ENTITY,
    CONF_SETTLE,
    CONF_SG_ACTUAL_ENTITY,
    CONF_SG_FORCED,
    CONF_SG_NORMAL,
    CONF_SG_PREHEAT,
    CONF_SG_RAISED,
    CONF_SG_REDUCED,
    CONF_SG_REFERENCE_ENTITY,
    CONF_STANDBY_REFERENCE_ENTITY,
    CONF_STANDBY_REFERENCE_STATE,
    CONF_TEMPERATURE_ENTITY,
    CONF_UPPER,
    CONF_URL,
    CONF_WINDOWS,
    CONF_ZONE_ALLOWED_ENTITY,
    DEFAULTS,
    DOMAIN,
    MISMATCH_CONFIRM_POLLS,
    MISMATCH_LOG_SIZE,
    SUBENTRY_ROOM,
)
from .charts import Charts, PlanMemory, build_charts, parse_time
from .learning import (
    KIND_LOCK,
    KIND_SETPOINT,
    ComfortEvent,
    ComfortLearning,
    Suggestion,
    lock_room,
    lock_set,
    setpoint_raised,
)
from .logic import (
    ControlInput,
    Decision,
    Plan,
    RoomState,
    Settings,
    Settle,
    decide,
    parse_plan,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
RESTART_STAMP_S = 60.0
HISTORY_INTERVAL = timedelta(minutes=15)
HISTORY_SUMMARY_PATH = "/v1/energy-management/history/summary"
HISTORY_HOURS_PATH = "/v1/energy-management/history/hours"


@dataclass
class ShadowStats:
    """Comparison of the integration's decision with the existing controller."""

    compared: int = 0
    mismatches: int = 0
    pending: dict[str, int] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"compared": self.compared, "mismatches": self.mismatches, "log": self.log}

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "ShadowStats":
        data = data or {}
        return cls(
            compared=int(data.get("compared", 0)),
            mismatches=int(data.get("mismatches", 0)),
            log=list(data.get("log", []))[-MISMATCH_LOG_SIZE:],
        )


@dataclass
class ControlData:
    """Result of one poll."""

    decision: Decision
    plan: Optional[Plan]
    eos_error: Optional[str]
    shadow: ShadowStats
    reference_sg: Optional[int] = None
    reference_standby: Optional[bool] = None
    suggestions: list[Suggestion] = field(default_factory=list)
    comfort_events: int = 0
    charts: Charts = field(default_factory=Charts)
    history: Optional[dict[str, Any]] = None
    """``/history/summary`` of EOS (key figures); None = not available (e.g. upstream EOS)."""
    history_hours: Optional[dict[str, Any]] = None


class EosControlCoordinator(DataUpdateCoordinator[ControlData]):
    """Polls EOS, evaluates the rules and compares them with a reference (shadow mode)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.data.get(CONF_SCAN_INTERVAL, DEFAULTS[CONF_SCAN_INTERVAL])
            ),
            config_entry=entry,
        )
        self.entry = entry
        self.started = dt_util.utcnow()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.shadow = ShadowStats()
        self.learning = ComfortLearning()
        self.memory = PlanMemory()
        self.settle = Settle(seconds=float(self.option(CONF_SETTLE)))
        self._settle_unsub: Optional[Callable[[], None]] = None
        self._last_plan: Optional[Plan] = None
        self._memory_generated: Optional[datetime] = None
        self._history: Optional[dict[str, Any]] = None
        self._history_hours: Optional[dict[str, Any]] = None
        self._history_at: Optional[datetime] = None
        self._history_supported = True
        self._humidity_since: dict[str, datetime] = {}
        self._setpoints: dict[str, Optional[float]] = {}
        self._presets: dict[str, Optional[str]] = {}
        self._holiday_until: Optional[datetime] = None
        self._lock: Optional[datetime] = None
        self._lock_seen = False
        self._loaded = False

    # ------------------------------------------------------------------

    def option(self, key: str) -> Any:
        return self.entry.options.get(key, DEFAULTS.get(key))

    def settings(self) -> Settings:
        return Settings(
            sg_by_mode={
                0: int(self.option(CONF_SG_REDUCED)),
                1: int(self.option(CONF_SG_NORMAL)),
                2: int(self.option(CONF_SG_RAISED)),
                3: int(self.option(CONF_SG_PREHEAT)),
                4: int(self.option(CONF_SG_FORCED)),
            },
            plan_max_age_min=float(self.option(CONF_PLAN_MAX_AGE)),
            reduction_hysteresis_k=float(self.option(CONF_HYSTERESIS)),
            min_valid_rooms=int(self.option(CONF_MIN_VALID_ROOMS)),
            max_room_c=float(self.option(CONF_MAX_ROOM)),
            holiday_floor_c=float(self.option(CONF_HOLIDAY_FLOOR)),
            holiday_restart_hours=float(self.option(CONF_HOLIDAY_RESTART)),
            holiday_dhw_lead_hours=float(self.option(CONF_HOLIDAY_DHW_LEAD)),
        )

    async def _async_update_data(self) -> ControlData:
        if not self._loaded:
            stored = await self._store.async_load() or {}
            self.shadow = ShadowStats.from_dict(stored)
            self.learning = ComfortLearning.from_list(stored.get("comfort_events"))
            self.memory = PlanMemory.from_list(stored.get("plan_memory"))
            self.memory.generated_at = parse_time(stored.get("plan_generated_at"))
            self._loaded = True
        now = dt_util.now()
        plan, solution, error = await self._fetch_plan()
        if plan is not None:
            self._last_plan = plan
        elif solution is None:
            # EOS busy or unreachable: the last plan stays valid until it is too old
            # (plan_max_age_min decides), instead of NORMAL for every short outage.
            plan = self._last_plan
        charts = build_charts(
            solution,
            self.entry.data[CONF_DEVICE_ID],
            self.memory,
            now,
            dt_util.get_default_time_zone(),
        )
        memory_changed = charts.generated_at is not None and charts.generated_at != (
            self._memory_generated
        )
        if memory_changed:
            self._memory_generated = charts.generated_at
        if solution is not None:  # EOS down: no further requests that would time out
            await self._update_history(now)
        data = ControlInput(
            now=now,
            plan=plan,
            rooms=self._rooms(now),
            heating_season=self._heating_season(),
            holiday=self._is_on(self.option(CONF_HOLIDAY_ENTITY)),
            holiday_return=self._datetime(self.option(CONF_HOLIDAY_RETURN_ENTITY)),
            reduced_setpoint_c=self._float(self.option(CONF_REDUCED_SETPOINT_ENTITY)),
            zone_setback_allowed=(
                self._is_on(self.option(CONF_ZONE_ALLOWED_ENTITY))
                if self.option(CONF_ZONE_ALLOWED_ENTITY)
                else True
            ),
            sg_actual=self._int(self.option(CONF_SG_ACTUAL_ENTITY)),
            reduction_locked_until=self._datetime(self.option(CONF_REDUCTION_LOCK_ENTITY)),
        )
        raw = decide(data, self.settings())
        if error and not raw.reason.startswith("NORMAL: EOS"):
            raw.reason = f"{raw.reason} ({error})"
        decision, wait = self.settle.apply(raw, now)
        self._schedule_settle(wait)
        learned = self._learn(now, data)
        reference_sg = self._int(self.option(CONF_SG_REFERENCE_ENTITY))
        reference_standby = self._standby_reference()
        compared = self._compare(now, decision, reference_sg, reference_standby, data.holiday)
        if compared or learned or memory_changed:
            self._store.async_delay_save(self._stored, 30)
        return ControlData(
            decision=decision,
            plan=plan,
            eos_error=error,
            shadow=self.shadow,
            reference_sg=reference_sg,
            reference_standby=reference_standby,
            suggestions=self.learning.suggestions(self._lower_limits()),
            comfort_events=len(self.learning.events),
            charts=charts,
            history=self._history,
            history_hours=self._history_hours,
        )

    def cancel_settle(self) -> None:
        if self._settle_unsub is not None:
            self._settle_unsub()
            self._settle_unsub = None

    def _schedule_settle(self, wait: Optional[float]) -> None:
        """Evaluate again when a pending change can be accepted (not at the next poll)."""
        self.cancel_settle()
        if wait is None:
            return

        async def _recheck(_now: datetime) -> None:
            self._settle_unsub = None
            await self.async_refresh()

        self._settle_unsub = async_call_later(self.hass, wait + 0.5, _recheck)

    async def async_shutdown(self) -> None:
        self.cancel_settle()
        await super().async_shutdown()

    def _stored(self) -> dict[str, Any]:
        return {
            **self.shadow.as_dict(),
            "comfort_events": self.learning.as_list(),
            "plan_memory": self.memory.as_list(),
            "plan_generated_at": (
                self.memory.generated_at.isoformat() if self.memory.generated_at else None
            ),
        }

    async def _fetch_plan(
        self,
    ) -> tuple[Optional[Plan], Optional[dict[str, Any]], Optional[str]]:
        url = f"{self.entry.data[CONF_URL]}/v1/energy-management/optimization/solution"
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return None, None, f"EOS HTTP {resp.status}"
                solution = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            return None, None, f"EOS unreachable: {type(exc).__name__}"
        if not isinstance(solution, dict):
            return None, None, "EOS solution is no object"
        try:
            return parse_plan(solution, self.entry.data[CONF_DEVICE_ID]), solution, None
        except ValueError as exc:
            return None, solution, str(exc)

    # ------------------------------------------------------------------
    # History (EOS RL29 fork: /v1/energy-management/history/...)
    # ------------------------------------------------------------------

    async def _update_history(self, now: datetime) -> None:
        """Fetch the history key figures every ``HISTORY_INTERVAL``.

        An EOS without the history endpoints (404) is asked once; the history sensors
        then stay unavailable. Other errors keep the last values until the next try.
        """
        if not self._history_supported:
            return
        if self._history_at is not None and now - self._history_at < HISTORY_INTERVAL:
            return
        self._history_at = now
        summary, status = await self._get_json(HISTORY_SUMMARY_PATH)
        if status == 404:
            self._history_supported = False
            self._history = self._history_hours = None
            return
        if summary is not None:
            self._history = summary
        hours, _ = await self._get_json(HISTORY_HOURS_PATH)
        if hours is not None:
            self._history_hours = hours

    async def _get_json(self, path: str) -> tuple[Optional[dict[str, Any]], Optional[int]]:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{self.entry.data[CONF_URL]}{path}", timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    return None, resp.status
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            _LOGGER.debug("EOS %s: %s", path, exc)
            return None, None
        return (data if isinstance(data, dict) else None), 200

    # ------------------------------------------------------------------
    # Rooms
    # ------------------------------------------------------------------

    def _rooms(self, now: datetime) -> list[RoomState]:
        run_on = timedelta(minutes=float(self.option(CONF_AIRING_RUN_ON)))
        rooms = []
        for subentry in self.entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_ROOM:
                continue
            cfg = subentry.data
            name = cfg.get(CONF_NAME, subentry.title)
            temperature = self._temperature(cfg.get(CONF_TEMPERATURE_ENTITY))
            windows = [self.hass.states.get(e) for e in cfg.get(CONF_WINDOWS, [])]
            window_open = any(s is not None and s.state == STATE_ON for s in windows)
            airing = window_open or any(
                self._recently_closed(s, now, run_on) for s in windows if s is not None
            )
            humidity = self._float(cfg.get(CONF_HUMIDITY_ENTITY))
            rooms.append(
                RoomState(
                    name=name,
                    temperature=temperature,
                    lower_c=float(cfg.get(CONF_LOWER, DEFAULTS[CONF_LOWER])),
                    upper_c=float(cfg.get(CONF_UPPER, DEFAULTS[CONF_UPPER])),
                    airing=airing,
                    has_windows=bool(windows),
                    window_open=window_open,
                    in_zone=bool(cfg.get(CONF_IN_ZONE, False)),
                    humidity_pct=humidity,
                    humidity_high=self._humidity_high(subentry.subentry_id, cfg, humidity, now),
                )
            )
        return rooms

    def _recently_closed(self, state: State, now: datetime, run_on: timedelta) -> bool:
        if state.state in (STATE_ON, STATE_UNKNOWN, STATE_UNAVAILABLE):
            return False
        changed = state.last_changed
        # A restart stamps every state anew: that is no closing of a window.
        if (changed - self.started).total_seconds() <= RESTART_STAMP_S:
            return False
        return now - changed < run_on

    def _humidity_high(
        self, key: str, cfg: Mapping[str, Any], humidity: Optional[float], now: datetime
    ) -> bool:
        limit = float(cfg.get(CONF_HUMIDITY_MAX, DEFAULTS[CONF_HUMIDITY_MAX]))
        if humidity is None or humidity < limit:
            self._humidity_since.pop(key, None)
            return False
        since = self._humidity_since.setdefault(key, now)
        delay = timedelta(
            minutes=float(cfg.get(CONF_HUMIDITY_DELAY, DEFAULTS[CONF_HUMIDITY_DELAY]))
        )
        return now - since >= delay

    # ------------------------------------------------------------------
    # Shadow comparison
    # ------------------------------------------------------------------

    def _compare(
        self,
        now: datetime,
        decision: Decision,
        reference_sg: Optional[int],
        reference_standby: Optional[bool],
        holiday: bool,
    ) -> bool:
        checks: dict[str, tuple[Any, Any]] = {}
        if reference_sg is not None:
            checks["sg"] = (decision.sg_target, reference_sg)
        if reference_standby is not None and holiday:
            checks["standby"] = (decision.standby, reference_standby)
        if not checks:
            return False
        stats = self.shadow
        stats.compared += 1
        changed = True
        for name, (ours, theirs) in checks.items():
            if ours == theirs:
                stats.pending.pop(name, None)
                continue
            count = stats.pending.get(name, 0) + 1
            stats.pending[name] = count
            if count == MISMATCH_CONFIRM_POLLS:
                stats.mismatches += 1
                stats.log.append(
                    {
                        "time": now.isoformat(timespec="minutes"),
                        "check": name,
                        "integration": ours,
                        "reference": theirs,
                        "reason": decision.standby_reason if name == "standby" else decision.reason,
                    }
                )
                stats.log = stats.log[-MISMATCH_LOG_SIZE:]
                changed = True
        return changed

    async def async_save_shadow(self) -> None:
        """Write the comparison and the comfort events now (unload, restart)."""
        if self._loaded:
            await self._store.async_save(self._stored())

    async def async_reset_shadow(self) -> None:
        self.shadow = ShadowStats()
        await self._store.async_save(self._stored())
        await self.async_request_refresh()

    async def async_reset_comfort(self) -> None:
        self.learning = ComfortLearning()
        await self._store.async_save(self._stored())
        await self.async_request_refresh()

    # ------------------------------------------------------------------
    # Comfort learning
    # ------------------------------------------------------------------

    def _room_configs(self) -> list[Mapping[str, Any]]:
        return [s.data for s in self.entry.subentries.values() if s.subentry_type == SUBENTRY_ROOM]

    def _lower_limits(self) -> dict[str, float]:
        return {
            str(cfg.get(CONF_NAME)): float(cfg.get(CONF_LOWER, DEFAULTS[CONF_LOWER]))
            for cfg in self._room_configs()
        }

    def _learn(self, now: datetime, data: ControlInput) -> bool:
        """Record manual setpoint raises and newly set locks; True = something recorded."""
        recorded = False
        if data.holiday:
            self._holiday_until = now
        # The return from holiday raises setpoints and presets everywhere: no event.
        settled = self._holiday_until is None or now - self._holiday_until > timedelta(hours=2)
        temps = {r.name: r.temperature for r in data.rooms}
        lowers = {r.name: r.lower_c for r in data.rooms}
        for cfg in self._room_configs():
            name = str(cfg.get(CONF_NAME))
            state = self._state(cfg.get(CONF_SETPOINT_ENTITY))
            current = self._setpoint(state)
            previous = self._setpoints.get(name)
            self._setpoints[name] = current
            preset = state.attributes.get("preset_mode") if state is not None else None
            preset_before = self._presets.get(name, preset)
            self._presets[name] = preset
            temperature = temps.get(name)
            if (
                setpoint_raised(previous, current)
                and preset == preset_before  # a mode change (economy -> comfort) is no event
                and not data.holiday
                and settled
                and state is not None
                and state.context.parent_id is None  # not changed by an automation
                and temperature is not None
            ):
                self.learning.record(
                    ComfortEvent(now, name, KIND_SETPOINT, temperature, lowers[name])
                )
                recorded = True
        lock = data.reduction_locked_until
        if self._lock_seen and not data.holiday and settled and lock_set(self._lock, lock, now):
            room = lock_room(
                [(r.name, r.temperature, r.lower_c) for r in data.rooms if not r.airing]
            )
            if room is not None:
                self.learning.record(ComfortEvent(now, room[0], KIND_LOCK, room[1], room[2]))
                recorded = True
        self._lock = lock
        self._lock_seen = True
        return recorded

    @staticmethod
    def _setpoint(state: Optional[State]) -> Optional[float]:
        if state is None:
            return None
        if state.domain == "climate":
            value = state.attributes.get("temperature")
            return float(value) if isinstance(value, (int, float)) else None
        try:
            return float(state.state)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _state(self, entity_id: Optional[str]) -> Optional[State]:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return state

    def _is_on(self, entity_id: Optional[str]) -> bool:
        state = self._state(entity_id)
        return state is not None and state.state == STATE_ON

    def _float(self, entity_id: Optional[str]) -> Optional[float]:
        state = self._state(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _int(self, entity_id: Optional[str]) -> Optional[int]:
        value = self._float(entity_id)
        return int(round(value)) if value is not None else None

    def _temperature(self, entity_id: Optional[str]) -> Optional[float]:
        state = self._state(entity_id)
        if state is None:
            return None
        if state.domain == "climate":
            value = state.attributes.get("current_temperature")
            return float(value) if isinstance(value, (int, float)) else None
        return self._float(entity_id)

    def _datetime(self, entity_id: Optional[str]) -> Optional[datetime]:
        state = self._state(entity_id)
        if state is None:
            return None
        timestamp = state.attributes.get("timestamp")
        if state.domain == "input_datetime" and isinstance(timestamp, (int, float)):
            if state.attributes.get("has_date", True):
                return dt_util.as_local(dt_util.utc_from_timestamp(timestamp))
        parsed = dt_util.parse_datetime(state.state)
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
        return parsed

    def _heating_season(self) -> bool:
        entity_id = self.option(CONF_HEATING_SEASON_ENTITY)
        if not entity_id:
            return True
        state = self._state(entity_id)
        if state is None:
            return True
        allowed = {
            s.strip().lower() for s in str(self.option(CONF_HEATING_SEASON_STATES)).split(",")
        }
        return state.state.lower() in allowed

    def _standby_reference(self) -> Optional[bool]:
        state = self._state(self.option(CONF_STANDBY_REFERENCE_ENTITY))
        if state is None:
            return None
        return state.state == str(self.option(CONF_STANDBY_REFERENCE_STATE))
