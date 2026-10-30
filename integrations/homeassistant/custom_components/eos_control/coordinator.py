"""Coordinator: reads EOS and Home Assistant states and decides."""

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
    CONF_EOS_DEVICE,
    CONF_KIND,
    CONF_MEASUREMENT_INTERVAL,
    CONF_MEASUREMENTS,
    CONF_PLAN_ENTITIES,
    CONF_SEND_MEASUREMENTS,
    ISSUE_AFTER_MIN,
    CONF_SG_ACTUAL_ENTITY,
    CONF_SG_FORCED,
    CONF_SG_NORMAL,
    CONF_SG_PREHEAT,
    CONF_SG_RAISED,
    CONF_SG_REDUCED,
    CONF_TEMPERATURE_ENTITY,
    CONF_UPPER,
    CONF_URL,
    CONF_WINDOWS,
    CONF_ZONE_ALLOWED_ENTITY,
    DEFAULTS,
    DOMAIN,
    SUBENTRY_DEVICE,
    SUBENTRY_ROOM,
)
from .devices import (
    KIND_HEAT_PUMP,
    DeviceState,
    EosDevice,
    Hold,
    device_state,
    find,
    parse_devices,
)
from .charts import Charts, PlanMemory, build_charts, parse_time
from .instructions import active_instructions
from .manual import ManualState
from .measurements import heat_pump_values, payload, state_value
from .plan_statistics import PLAN_STATISTICS, completed_hours, statistic_id
from .repairs import set_issue
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
DEVICES_PATH = "/v1/config/devices"
PLAN_PATH = "/v1/energy-management/plan"
MEASUREMENT_PATH = "/v1/measurement/data"
HEALTH_PATH = "/v1/health"


@dataclass
class ControlData:
    """Result of one poll."""

    decision: Decision
    plan: Optional[Plan]
    eos_error: Optional[str]
    suggestions: list[Suggestion] = field(default_factory=list)
    comfort_events: int = 0
    charts: Charts = field(default_factory=Charts)
    history: Optional[dict[str, Any]] = None
    """``/history/summary`` of EOS (key figures); None = not available (e.g. upstream EOS)."""
    history_hours: Optional[dict[str, Any]] = None
    devices: dict[str, Optional[DeviceState]] = field(default_factory=dict)
    """Planned state per added EOS device (subentry); None = not in a valid plan."""
    eos_devices: list[EosDevice] = field(default_factory=list)
    rooms: dict[str, RoomState] = field(default_factory=dict)
    """Rooms by subentry id, as the decision saw them."""
    instructions: Optional[dict[str, dict[str, Any]]] = None
    """Active EOS instructions per resource; None = no valid plan."""
    columns: Optional[dict[str, Any]] = None
    """Plan columns of the current slot (``<device>_heating_mode`` ...); None = no valid plan."""
    holiday: bool = False


class EosControlCoordinator(DataUpdateCoordinator[ControlData]):
    """Polls EOS and evaluates the rules."""

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
        self.learning = ComfortLearning()
        self.memory = PlanMemory()
        self.settle = Settle(seconds=float(self.option(CONF_SETTLE)))
        self._settle_unsub: Optional[Callable[[], None]] = None
        self._last_plan: Optional[Plan] = None
        self._last_solution: Optional[dict[str, Any]] = None
        self._ems_plan: Optional[dict[str, Any]] = None
        self.hold = Hold(seconds=float(self.option(CONF_SETTLE)))
        self.eos_devices: list[EosDevice] = []
        self._devices_at: Optional[datetime] = None
        self.manual = ManualState()
        self.last_input: Optional[ControlInput] = None
        self._measured_at: Optional[datetime] = None
        self._eos_ok_at: Optional[datetime] = None
        self._push_ok_at: Optional[datetime] = None
        self._push_error: Optional[str] = None
        self._stats_until: Optional[datetime] = None
        self.eos_version: Optional[str] = None
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
            self.learning = ComfortLearning.from_list(stored.get("comfort_events"))
            self.memory = PlanMemory.from_list(stored.get("plan_memory"))
            self.memory.generated_at = parse_time(stored.get("plan_generated_at"))
            self.manual = ManualState.from_dict(stored.get("manual"))
            self._stats_until = parse_time(stored.get("statistics_until"))
            self._loaded = True
        now = dt_util.now()
        manual_changed = self.manual.expire(now)
        plan, solution, error = await self._fetch_plan()
        if plan is not None:
            self._last_plan = plan
        elif solution is None:
            # EOS busy or unreachable: the last plan stays valid until it is too old
            # (plan_max_age_min decides), instead of NORMAL for every short outage.
            plan = self._last_plan
        if solution is not None:
            self._last_solution = solution
            self._eos_ok_at = now
            await self._update_devices(now)
            if self.option(CONF_PLAN_ENTITIES):
                ems_plan, _ = await self._get_json(PLAN_PATH)
                if ems_plan is not None:
                    self._ems_plan = ems_plan
        heat_pump = self.heat_pump_id
        if heat_pump and self.eos_devices and find(self.eos_devices, heat_pump) is None:
            error = f"heat pump '{heat_pump}' not in the EOS configuration"
        charts = build_charts(
            solution,
            heat_pump,
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
            holiday=self._is_on(self.option(CONF_HOLIDAY_ENTITY))
            or self.manual.holiday_active(now),
            holiday_return=(
                self.manual.holiday_end
                if self.manual.holiday_active(now)
                else self._datetime(self.option(CONF_HOLIDAY_RETURN_ENTITY))
            ),
            reduced_setpoint_c=self._float(self.option(CONF_REDUCED_SETPOINT_ENTITY)),
            zone_setback_allowed=(
                self._is_on(self.option(CONF_ZONE_ALLOWED_ENTITY))
                if self.option(CONF_ZONE_ALLOWED_ENTITY)
                else True
            ),
            sg_actual=self._int(self.option(CONF_SG_ACTUAL_ENTITY)),
            reduction_locked_until=self._lock_until(now),
        )
        self.last_input = data
        raw = decide(data, self.settings())
        if error and not raw.reason.startswith("NORMAL: EOS"):
            raw.reason = f"{raw.reason} ({error})"
        decision, wait = self.settle.apply(raw, now)
        devices, device_wait = self._device_states(now)
        waits = [w for w in (wait, device_wait) if w is not None]
        self._schedule_settle(min(waits) if waits else None)
        learned = self._learn(now, data)
        if solution is not None:
            await self._send_measurements(now, data, raw)
        self._write_statistics(now, charts)
        self._update_issues(now, charts)
        if learned or memory_changed or manual_changed:
            self._store.async_delay_save(self._stored, 30)
        return ControlData(
            decision=decision,
            plan=plan,
            eos_error=error,
            suggestions=self.learning.suggestions(self._lower_limits()),
            comfort_events=len(self.learning.events),
            charts=charts,
            history=self._history,
            history_hours=self._history_hours,
            devices=devices,
            eos_devices=self.eos_devices,
            rooms={r.key: r for r in data.rooms},
            holiday=data.holiday,
            columns=self._columns(now),
            instructions=self._instructions(now),
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
            "comfort_events": self.learning.as_list(),
            "plan_memory": self.memory.as_list(),
            "plan_generated_at": (
                self.memory.generated_at.isoformat() if self.memory.generated_at else None
            ),
            "manual": self.manual.as_dict(),
            "statistics_until": self._stats_until.isoformat() if self._stats_until else None,
        }

    # ------------------------------------------------------------------
    # Actions (services)
    # ------------------------------------------------------------------

    async def async_manual_changed(self) -> None:
        """An action changed the manual state: store, send at once, re-evaluate."""
        self._measured_at = None
        await self._store.async_save(self._stored())
        await self.async_refresh()

    def _lock_until(self, now: datetime) -> Optional[datetime]:
        locks = [
            t
            for t in (
                self._datetime(self.option(CONF_REDUCTION_LOCK_ENTITY)),
                self.manual.lock_active(now),
            )
            if t is not None
        ]
        return max(locks) if locks else None

    # ------------------------------------------------------------------
    # Measurements to EOS
    # ------------------------------------------------------------------

    def measurement_values(
        self, now: datetime, data: ControlInput, decision: Decision
    ) -> dict[str, Optional[float]]:
        """All values for EOS: mapped entities, computed heat pump values, run requests."""
        values: dict[str, Optional[float]] = {}
        mappings = [self.entry.options.get(CONF_MEASUREMENTS) or {}]
        mappings += [s.data.get(CONF_MEASUREMENTS) or {} for s in self.device_subentries()]
        for mapping in mappings:
            for key, entity_id in mapping.items():
                state = self.hass.states.get(entity_id) if entity_id else None
                values[key] = state_value(key, state.state if state is not None else None)
        heat_pump = self.heat_pump_id
        if heat_pump:
            values.update(
                heat_pump_values(
                    heat_pump,
                    room_mean_c=decision.room_mean_c,
                    min_margin_k=decision.room_min_margin_k,
                    max_margin_k=decision.room_max_margin_k,
                    humidity_warning=decision.humidity_warning,
                    window_open_share=decision.window_open_share,
                    holiday=data.holiday,
                    holiday_return=data.holiday_return,
                    reduced_setpoint_c=data.reduced_setpoint_c,
                    holiday_floor_c=float(self.option(CONF_HOLIDAY_FLOOR)),
                    reduction_locked_until=data.reduction_locked_until,
                    now=now,
                )
            )
        values.update(self.manual.run_measurements())
        return values

    async def _send_measurements(
        self, now: datetime, data: ControlInput, decision: Decision
    ) -> None:
        if not self.option(CONF_SEND_MEASUREMENTS):
            return
        interval = timedelta(minutes=float(self.option(CONF_MEASUREMENT_INTERVAL)))
        if self._measured_at is not None and now - self._measured_at < interval:
            return
        self._measured_at = now
        body = payload(self.measurement_values(now, data, decision), now)
        if body is None:
            return
        session = async_get_clientsession(self.hass)
        try:
            async with session.put(
                f"{self.entry.data[CONF_URL]}{MEASUREMENT_PATH}",
                json=body,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status >= 300:
                    self._push_error = f"EOS HTTP {resp.status}"
                    return
        except (aiohttp.ClientError, TimeoutError) as exc:
            self._push_error = f"EOS unreachable: {type(exc).__name__}"
            return
        self._push_ok_at = now
        self._push_error = None
        if self.manual.ended_runs:
            self.manual.sent()
            self._store.async_delay_save(self._stored, 30)

    # ------------------------------------------------------------------
    # Long-term statistics of the plan
    # ------------------------------------------------------------------

    def _write_statistics(self, now: datetime, charts: Charts) -> None:
        if "recorder" not in self.hass.config.components:
            return
        hours = completed_hours(
            self.memory.series(),
            dt_util.get_default_time_zone(),
            charts.slot_minutes,
            now,
            self._stats_until,
        )
        if not hours:
            return
        from homeassistant.components.recorder.models import (
            StatisticData,
            StatisticMeanType,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import async_add_external_statistics

        heat_pump = self.heat_pump_id
        for stat in PLAN_STATISTICS:
            if stat.heat_pump and not heat_pump:
                continue
            rows = [
                StatisticData(
                    start=h["start"],
                    mean=h[stat.column],
                    min=h[stat.column],
                    max=h[stat.column],
                )
                for h in hours
                if isinstance(h.get(stat.column), (int, float))
            ]
            if not rows:
                continue
            name = f"EOS {heat_pump} {stat.name}" if stat.heat_pump else f"EOS {stat.name}"
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.ARITHMETIC,
                has_sum=False,
                name=name,
                source=DOMAIN,
                statistic_id=statistic_id(DOMAIN, stat, heat_pump),
                unit_class=stat.unit_class,
                unit_of_measurement=stat.unit,
            )
            async_add_external_statistics(self.hass, metadata, rows)
        self._stats_until = max(h["start"] for h in hours)
        self._store.async_delay_save(self._stored, 30)

    # ------------------------------------------------------------------
    # Repairs
    # ------------------------------------------------------------------

    def _update_issues(self, now: datetime, charts: Charts) -> None:
        limit = timedelta(minutes=ISSUE_AFTER_MIN)
        since = self._eos_ok_at or self.started
        set_issue(
            self.hass,
            self.entry,
            "eos_unreachable",
            now - since > limit,
            {"url": self.entry.data[CONF_URL], "minutes": str(ISSUE_AFTER_MIN)},
        )
        max_age = timedelta(minutes=float(self.option(CONF_PLAN_MAX_AGE)))
        stale = (
            self._eos_ok_at is not None
            and now - self._eos_ok_at <= limit
            and charts.generated_at is not None
            and now - charts.generated_at > max_age
        )
        set_issue(
            self.hass,
            self.entry,
            "plan_stale",
            stale,
            {"minutes": str(int(max_age.total_seconds() // 60))},
        )
        known = {d.device_id for d in self.eos_devices}
        for subentry in self.device_subentries():
            device_id = str(subentry.data[CONF_EOS_DEVICE])
            set_issue(
                self.hass,
                self.entry,
                f"device_missing_{device_id}",
                bool(self.eos_devices) and device_id not in known,
                {"device": device_id},
                translation_key="device_missing",
            )
        push_since = self._push_ok_at or self.started
        set_issue(
            self.hass,
            self.entry,
            "measurements_failed",
            bool(self.option(CONF_SEND_MEASUREMENTS))
            and self._push_error is not None
            and now - push_since > limit,
            {"error": self._push_error or "", "minutes": str(ISSUE_AFTER_MIN)},
        )

    # ------------------------------------------------------------------
    # EOS devices (batteries, electric vehicles, home appliances)
    # ------------------------------------------------------------------

    def device_subentries(self, kind: Optional[str] = None) -> list[Any]:
        """EOS devices added to the entry (optionally only one kind)."""
        return [
            s
            for s in self.entry.subentries.values()
            if s.subentry_type == SUBENTRY_DEVICE
            and (kind is None or s.data.get(CONF_KIND) == kind)
        ]

    def room_subentries(self) -> list[Any]:
        return [s for s in self.entry.subentries.values() if s.subentry_type == SUBENTRY_ROOM]

    @property
    def heat_pump_id(self) -> str:
        """The heat pump of the entry ('' = none); one heat pump per EOS entry."""
        subs = self.device_subentries(KIND_HEAT_PUMP)
        return str(subs[0].data[CONF_EOS_DEVICE]) if subs else ""

    async def _update_devices(self, now: datetime) -> None:
        """Device list of the EOS configuration, every ``HISTORY_INTERVAL``."""
        if self._devices_at is not None and now - self._devices_at < HISTORY_INTERVAL:
            return
        self._devices_at = now
        config, _ = await self._get_json(DEVICES_PATH)
        if config is not None:
            self.eos_devices = parse_devices(config)
        health, _ = await self._get_json(HEALTH_PATH)
        if health is not None and health.get("version"):
            self.eos_version = str(health["version"])

    def _plan_solution(self, now: datetime) -> Optional[dict[str, Any]]:
        """Latest solution while it is younger than the plan age limit."""
        solution = self._last_solution
        if solution is None:
            return None
        generated = parse_time(solution.get("generated_at"))
        max_age = timedelta(minutes=float(self.option(CONF_PLAN_MAX_AGE)))
        if generated is None or now - generated > max_age:
            return None
        return solution

    def _instructions(self, now: datetime) -> Optional[dict[str, dict[str, Any]]]:
        """Active instructions of the energy management plan while it is valid."""
        plan = self._ems_plan
        if plan is None:
            return None
        generated = parse_time(plan.get("generated_at"))
        max_age = timedelta(minutes=float(self.option(CONF_PLAN_MAX_AGE)))
        if generated is None or now - generated > max_age:
            return None
        return active_instructions(plan, now)

    def _columns(self, now: datetime) -> Optional[dict[str, Any]]:
        """Values of all plan columns in the slot that contains ``now``."""
        solution = self._plan_solution(now)
        frame = (solution or {}).get("solution") or {}
        rows = frame.get("data") if isinstance(frame, dict) else None
        if not isinstance(rows, dict):
            return None
        current: Optional[dict[str, Any]] = None
        for stamp in sorted(rows, key=lambda t: parse_time(t) or now):
            start = parse_time(stamp)
            if start is not None and start <= now and isinstance(rows[stamp], dict):
                current = rows[stamp]
        if current is None:
            return None
        return {k: v for k, v in current.items() if k != "date_time"}

    def _device_states(
        self, now: datetime
    ) -> tuple[dict[str, Optional[DeviceState]], Optional[float]]:
        solution = self._plan_solution(now)
        states: dict[str, Optional[DeviceState]] = {}
        waits: list[float] = []
        for subentry in self.device_subentries():
            if subentry.data.get(CONF_KIND) == KIND_HEAT_PUMP:
                continue  # the heat pump has its own decision
            device_id = str(subentry.data[CONF_EOS_DEVICE])
            device = find(self.eos_devices, device_id) or EosDevice(
                device_id, str(subentry.data.get(CONF_KIND))
            )
            state = device_state(solution, device, now)
            if state is None:
                self.hold.forget(device_id)
                states[device_id] = None
                continue
            state, wait = self.hold.apply(state, now)
            if wait is not None:
                waits.append(wait)
            states[device_id] = state
        return states, (min(waits) if waits else None)

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
        if not self.heat_pump_id:
            return None, solution, None  # no heat pump: only charts and devices
        try:
            return parse_plan(solution, self.heat_pump_id), solution, None
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
                    key=subentry.subentry_id,
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

    async def async_save(self) -> None:
        """Write the stored state now (unload, restart)."""
        if self._loaded:
            await self._store.async_save(self._stored())

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
