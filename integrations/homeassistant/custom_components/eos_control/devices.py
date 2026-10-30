"""EOS devices (from the EOS configuration) and their planned state (no HA imports).

EOS lists its devices in ``/v1/config/devices``: heat pumps, batteries, electric
vehicles (charged by a wallbox), home appliances and inverters. The integration offers
them for selection instead of asking for a typed device id, and reads the plan of each
selected device from the optimisation solution:

- batteries and electric vehicles: operation mode (``<id>_<mode>_op_mode`` = 1), its
  factor, the planned state of charge (``<id>_soc_factor``);
- home appliances: running (``<id>_run_op_mode``), next planned start
  (``<id>_next_start_ts``), energy per slot (``<id>_energy_wh``).

The integration publishes these as targets; switching the devices stays with Home
Assistant automations (as for the heat pump).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Optional

KIND_HEAT_PUMP = "heat_pump"
KIND_BATTERY = "battery"
KIND_EV = "electric_vehicle"
KIND_APPLIANCE = "home_appliance"
KIND_INVERTER = "inverter"

# Sections of the EOS device configuration -> kind
CONFIG_SECTIONS: dict[str, str] = {
    "heat_pumps": KIND_HEAT_PUMP,
    "batteries": KIND_BATTERY,
    "electric_vehicles": KIND_EV,
    "home_appliances": KIND_APPLIANCE,
    "inverters": KIND_INVERTER,
}
# Kinds the integration can add as devices (the heat pump is the entry itself)
DEVICE_KINDS = (KIND_BATTERY, KIND_EV, KIND_APPLIANCE)

# EOS battery operation modes (akkudoktoreos.devices.devicesabc.BatteryOperationMode)
OPERATION_MODES = (
    "IDLE",
    "SELF_CONSUMPTION",
    "NON_EXPORT",
    "PEAK_SHAVING",
    "GRID_SUPPORT_EXPORT",
    "GRID_SUPPORT_IMPORT",
    "FREQUENCY_REGULATION",
    "RAMP_RATE_CONTROL",
    "RESERVE_BACKUP",
    "OUTAGE_SUPPLY",
    "FORCED_CHARGE",
    "FORCED_DISCHARGE",
    "FAULT",
)
GRID_CHARGE_MODES = frozenset({"GRID_SUPPORT_IMPORT", "FORCED_CHARGE"})
DISCHARGE_MODES = frozenset(
    {"SELF_CONSUMPTION", "PEAK_SHAVING", "GRID_SUPPORT_EXPORT", "FORCED_DISCHARGE", "OUTAGE_SUPPLY"}
)
EXPORT_MODES = frozenset({"GRID_SUPPORT_EXPORT", "FORCED_DISCHARGE"})


@dataclass(frozen=True)
class EosDevice:
    """A device of the EOS configuration."""

    device_id: str
    kind: str
    max_charge_power_w: Optional[float] = None
    capacity_wh: Optional[float] = None


def _float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def parse_devices(config: Any) -> list[EosDevice]:
    """Devices of ``/v1/config/devices`` (sections as mapping id -> settings or list)."""
    devices: list[EosDevice] = []
    if not isinstance(config, Mapping):
        return devices
    for section, kind in CONFIG_SECTIONS.items():
        entries = config.get(section)
        if isinstance(entries, Mapping):
            items = [(str(key), value) for key, value in entries.items()]
        elif isinstance(entries, list):
            items = [
                (str(value.get("device_id")), value)
                for value in entries
                if isinstance(value, Mapping) and value.get("device_id")
            ]
        else:
            continue
        for device_id, settings in items:
            settings = settings if isinstance(settings, Mapping) else {}
            device_id = str(settings.get("device_id") or device_id)
            devices.append(
                EosDevice(
                    device_id=device_id,
                    kind=kind,
                    max_charge_power_w=_float(settings.get("max_charge_power_w")),
                    capacity_wh=_float(settings.get("capacity_wh")),
                )
            )
    return devices


def find(devices: list[EosDevice], device_id: str) -> Optional[EosDevice]:
    return next((d for d in devices if d.device_id == device_id), None)


@dataclass
class DeviceState:
    """What EOS plans for one device now, plus its series for charts."""

    device_id: str
    kind: str
    mode: Optional[str] = None
    """Battery/EV operation mode now (EOS BatteryOperationMode)."""
    factor: Optional[float] = None
    """Factor of the mode (charge rate, export share), 0..1."""
    soc_pct: Optional[float] = None
    power_w: Optional[float] = None
    """Charge power target (grid charging of a battery, charging of an EV)."""
    running: Optional[bool] = None
    """Home appliance: runs now according to the plan."""
    energy_wh: Optional[float] = None
    next_start: Optional[datetime] = None
    next_change: Optional[datetime] = None
    next_mode: Optional[str] = None
    pending: Optional[str] = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def grid_charge(self) -> Optional[bool]:
        return None if self.mode is None else self.mode in GRID_CHARGE_MODES

    @property
    def discharge_allowed(self) -> Optional[bool]:
        return None if self.mode is None else self.mode in DISCHARGE_MODES

    @property
    def grid_export(self) -> Optional[bool]:
        return None if self.mode is None else self.mode in EXPORT_MODES

    def outputs(self) -> tuple[Any, ...]:
        """What is switched: mode and factor, or running."""
        factor = round(self.factor, 2) if self.factor is not None else None
        return (self.mode, factor, self.running)


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _rows(solution: Mapping[str, Any]) -> list[tuple[datetime, Mapping[str, Any]]]:
    frame = solution.get("solution")
    data = frame.get("data") if isinstance(frame, Mapping) else None
    if not isinstance(data, Mapping):
        return []
    rows = [(_parse_time(stamp), values) for stamp, values in data.items()]
    return sorted(
        ((t, v) for t, v in rows if t is not None and isinstance(v, Mapping)),
        key=lambda item: item[0],
    )


def _mode(values: Mapping[str, Any], device_id: str) -> tuple[Optional[str], Optional[float]]:
    for mode in OPERATION_MODES:
        active = _float(values.get(f"{device_id}_{mode.lower()}_op_mode"))
        if active is not None and active >= 0.5:
            return mode, _float(values.get(f"{device_id}_{mode.lower()}_op_factor"))
    return None, None


def _has_columns(rows: list[tuple[datetime, Mapping[str, Any]]], device_id: str) -> bool:
    prefix = f"{device_id}_"
    return any(key.startswith(prefix) for _, values in rows[:1] for key in values)


def device_state(
    solution: Optional[Mapping[str, Any]], device: EosDevice, now: datetime
) -> Optional[DeviceState]:
    """Planned state of ``device`` at ``now``; None = the plan does not contain it."""
    if not solution:
        return None
    rows = _rows(solution)
    if not rows or not _has_columns(rows, device.device_id):
        return None
    current_index = None
    for index, (start, _) in enumerate(rows):
        if start <= now:
            current_index = index
        else:
            break
    state = DeviceState(device_id=device.device_id, kind=device.kind)
    did = device.device_id
    if device.kind in (KIND_BATTERY, KIND_EV):
        for start, values in rows:
            mode, factor = _mode(values, did)
            soc = _float(values.get(f"{did}_soc_factor"))
            row: dict[str, Any] = {"t": start.isoformat(), "mode": mode}
            if factor is not None:
                row["factor"] = round(factor, 3)
            if soc is not None:
                row["soc_pct"] = round(soc * 100.0, 1)
            state.rows.append(row)
        if current_index is not None:
            now_row = state.rows[current_index]
            state.mode = now_row.get("mode")
            state.factor = now_row.get("factor")
            state.soc_pct = now_row.get("soc_pct")
            charging = state.mode in GRID_CHARGE_MODES
            if charging and state.factor is not None and device.max_charge_power_w:
                state.power_w = round(state.factor * device.max_charge_power_w)
            elif state.mode is not None and not charging:
                state.power_w = 0.0
            for row in state.rows[current_index + 1 :]:
                if row.get("mode") != state.mode:
                    state.next_change = _parse_time(row["t"])
                    state.next_mode = row.get("mode")
                    break
    elif device.kind == KIND_APPLIANCE:
        for start, values in rows:
            run = _float(values.get(f"{did}_run_op_mode"))
            energy = _float(values.get(f"{did}_energy_wh"))
            row = {"t": start.isoformat(), "running": bool(run is not None and run >= 0.5)}
            if energy is not None:
                row["energy_wh"] = round(energy, 1)
            state.rows.append(row)
        if current_index is not None:
            now_row = state.rows[current_index]
            state.running = now_row["running"]
            state.energy_wh = now_row.get("energy_wh")
            values = rows[current_index][1]
            ts = _float(values.get(f"{did}_next_start_ts"))
            if ts:
                state.next_start = datetime.fromtimestamp(ts, tz=timezone.utc)
            for row in state.rows[current_index + 1 :]:
                if row["running"] != state.running:
                    state.next_change = _parse_time(row["t"])
                    break
    else:
        return None
    return state


@dataclass
class Hold:
    """Settle time per device: a changed target is accepted after ``seconds``.

    Same rule as :class:`logic.Settle` for the heat pump: the first value is accepted
    at once, a change is held until it stayed the same for ``seconds``, a change back
    drops it.
    """

    seconds: float = 10.0
    accepted: dict[str, DeviceState] = field(default_factory=dict)
    pending: dict[str, tuple[tuple[Any, ...], datetime]] = field(default_factory=dict)

    def apply(self, state: DeviceState, now: datetime) -> tuple[DeviceState, Optional[float]]:
        key = state.device_id
        accepted = self.accepted.get(key)
        outputs = state.outputs()
        if accepted is None or self.seconds <= 0 or outputs == accepted.outputs():
            return self._accept(state), None
        held = self.pending.get(key)
        if held is None or held[0] != outputs:
            held = (outputs, now)
            self.pending[key] = held
        waited = (now - held[1]).total_seconds()
        if waited >= self.seconds:
            return self._accept(state), None
        remaining = self.seconds - waited
        change = state.mode if state.kind != KIND_APPLIANCE else ("run" if state.running else "off")
        return (
            replace(
                state,
                mode=accepted.mode,
                factor=accepted.factor,
                power_w=accepted.power_w,
                running=accepted.running,
                pending=f"{change} (in {remaining:.0f} s)",
            ),
            remaining,
        )

    def forget(self, device_id: str) -> None:
        self.accepted.pop(device_id, None)
        self.pending.pop(device_id, None)

    def _accept(self, state: DeviceState) -> DeviceState:
        self.accepted[state.device_id] = state
        self.pending.pop(state.device_id, None)
        return state
