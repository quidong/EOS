"""Multi-zone space heating on one heat pump circuit (eos-plus fork).

A house often has zones that react differently: a heavy wet screed upstairs, a
light dry screed downstairs, rooms that are colder than the house mean. One
two-node model per zone captures that; the zones exchange heat through ceilings
and stairs.

Model, per zone ``i`` (temperatures degC, heat W, capacities Wh/K, time h)::

    C_s,i dT_s,i/dt = Q_i - H_sr,i (T_s,i - T_r,i)
    C_r,i dT_r,i/dt = H_sr,i (T_s,i - T_r,i) - H_loss,i (T_r,i - T_out) + G_i
                      - sum_j H_ij (T_r,i - T_r,j)

All 2n nodes are discretised together and exactly (zero-order hold), like the
single-zone ``BuildingModel``.

Control, per slot:

- Every zone has its own mode (``HeatingMode``). REDUCED, NORMAL, RAISED and
  PREHEAT set the room target of the zone; the room controllers of the zone
  regulate towards it and close the valves once the target is reached. With
  fixed room setpoints (``ComfortParameters.fixed_room_setpoints``) nothing
  changes the controllers' setpoint: RAISED keeps the setpoint, and REDUCED is
  only possible for all zones together (SG Ready "reduced"); a single REDUCED
  zone runs NORMAL. ``MultiZoneResult.zone_modes`` reports what each zone ran.
  FORCED opens all valves of the zone (forced position) and takes what the
  heat pump can deliver; with ``forced_flow_offset_k`` (RL29 fork) what the shared
  heating curve plus offset pushes into the zone's screed.
- The zones share one heating circuit, hence one flow temperature and one heat
  pump. The heat pump mode is derived from the zone modes
  (``aggregate_heating_mode``): FORCED if any zone is forced, else the
  strongest raise, REDUCED only if every zone is reduced.
- Hot water, standby, §14a and the cooling season come from the single-zone
  ``HeatPumpSystem`` run on the aggregate of all zones. Space heating is then
  distributed: each zone asks for the heat its room target needs, limited by its
  emitter at the highest flow temperature; if the heat pump cannot deliver the
  sum, every zone gets the same share of its demand.

The module has no EOS dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from akkudoktoreos.devices.thermal.building import BuildingModel, BuildingParameters
from akkudoktoreos.devices.thermal.heatpump import (
    REPAIR_MARGIN_K,
    DhwMode,
    HeatingMode,
    HeatPumpSystem,
    SlotInputs,
    ThermalState,
)


@dataclass(frozen=True)
class ZoneParameters:
    """One zone: its own two-node building model and its weight in the comfort cost."""

    zone_id: str
    building: BuildingParameters
    weight: float = 1.0
    """Share of the comfort cost (e.g. floor area share); weights are normalised."""


@dataclass(frozen=True)
class ZoneCoupling:
    """Heat exchange between the rooms of two zones (ceiling, stairs) [W/K]."""

    a: int
    b: int
    w_per_k: float


class MultiZoneBuildingModel:
    """Exact discrete-time model of n coupled two-node zones for a fixed slot length."""

    def __init__(
        self,
        zones: Sequence[ZoneParameters],
        couplings: Sequence[ZoneCoupling] = (),
        slot_hours: float = 0.25,
    ) -> None:
        if not zones:
            raise ValueError("At least one zone is required.")
        if slot_hours <= 0:
            raise ValueError("slot_hours must be positive.")
        for zone in zones:
            zone.building.validate()
        n = len(zones)
        for c in couplings:
            if not (0 <= c.a < n and 0 <= c.b < n) or c.a == c.b:
                raise ValueError(f"Invalid zone coupling {c}.")
            if c.w_per_k < 0:
                raise ValueError("Coupling must not be negative.")
        self.zones = list(zones)
        self.n = n
        self.slot_hours = float(slot_hours)
        # State x = [T_s,0..T_s,n-1, T_r,0..T_r,n-1]; inputs u = [Q_0..Q_n-1, T_out, G_0..G_n-1].
        a = np.zeros((2 * n, 2 * n))
        b = np.zeros((2 * n, 2 * n + 1))
        for i, zone in enumerate(zones):
            p = zone.building
            cs, cr = p.slab_capacity_wh_per_k, p.room_capacity_wh_per_k
            s, r = i, n + i
            a[s, s] = -p.slab_room_w_per_k / cs
            a[s, r] = p.slab_room_w_per_k / cs
            a[r, s] = p.slab_room_w_per_k / cr
            a[r, r] = -(p.slab_room_w_per_k + p.loss_w_per_k) / cr
            b[s, i] = 1.0 / cs
            b[r, n] = p.loss_w_per_k / cr
            b[r, n + 1 + i] = 1.0 / cr
        for c in couplings:
            for i, j in ((c.a, c.b), (c.b, c.a)):
                cr = zones[i].building.room_capacity_wh_per_k
                a[n + i, n + i] -= c.w_per_k / cr
                a[n + i, n + j] += c.w_per_k / cr
        size = a.shape[0] + b.shape[1]
        aug = np.zeros((size, size))
        aug[: 2 * n, : 2 * n] = a * self.slot_hours
        aug[: 2 * n, 2 * n :] = b * self.slot_hours
        from scipy.linalg import expm

        phi = expm(aug)
        self.ad = phi[: 2 * n, : 2 * n]
        self.bd = phi[: 2 * n, 2 * n :]

    def step(
        self,
        slabs: Sequence[float],
        rooms: Sequence[float],
        heats_w: Sequence[float],
        outdoor_c: float,
        gains_w: Sequence[float],
    ) -> tuple[list[float], list[float]]:
        """Slab and room temperatures of every zone at the end of one slot."""
        x = np.asarray(list(slabs) + list(rooms), dtype=float)
        u = np.asarray(list(heats_w) + [outdoor_c] + list(gains_w), dtype=float)
        y = self.ad @ x + self.bd @ u
        return [float(v) for v in y[: self.n]], [float(v) for v in y[self.n :]]


def aggregate_heating_mode(zone_modes: Sequence[HeatingMode]) -> HeatingMode:
    """Heat pump mode (SG Ready) that serves the zone modes of a slot.

    FORCED when any zone is forced open; otherwise the strongest raise; REDUCED
    only when every zone is reduced. A zone on a lower target than the heat pump
    mode closes its valves through its room controllers.
    """
    modes = [HeatingMode(m) for m in zone_modes]
    if not modes:
        return HeatingMode.NORMAL
    if HeatingMode.COOL in modes:
        return HeatingMode.COOL
    if HeatingMode.FORCED in modes:
        return HeatingMode.FORCED
    if HeatingMode.PREHEAT in modes:
        return HeatingMode.PREHEAT
    if HeatingMode.RAISED in modes:
        return HeatingMode.RAISED
    if all(m == HeatingMode.REDUCED for m in modes):
        return HeatingMode.REDUCED
    return HeatingMode.NORMAL


@dataclass
class MultiZoneState:
    """State of all zones plus the shared hot water and heat pump state."""

    slabs: list[float]
    rooms: list[float]
    shared: ThermalState
    """Hot water and heat pump flags; its slab/room hold the aggregate."""

    def copy(self) -> "MultiZoneState":
        return MultiZoneState(list(self.slabs), list(self.rooms), self.shared.copy())


@dataclass
class MultiZoneResult:
    """Outcome of one slot for all zones."""

    state: MultiZoneState
    heating_mode: HeatingMode
    """Heat pump mode derived from the zone modes."""
    zone_heat_wh: list[float]
    hp_electric_wh: float
    rod_electric_wh: float
    heat_dhw_wh: float
    flow_c: float
    cop_space: float
    forced_space_wh: float
    comfort_violation_kh: float
    """Weighted over the zones."""
    overheat_kh: float
    """Weighted over the zones."""
    dhw_violation_kh: float
    zone_comfort_violation_kh: list[float] = field(default_factory=list)
    zone_overheat_kh: list[float] = field(default_factory=list)
    zone_modes: list[HeatingMode] = field(default_factory=list)
    """Mode each zone actually runs (after reduction lock and fixed room setpoints)."""

    @property
    def electric_wh(self) -> float:
        return self.hp_electric_wh + self.rod_electric_wh

    @property
    def heat_space_wh(self) -> float:
        return float(sum(self.zone_heat_wh))


class MultiZoneHeatPump:
    """Space heating of n zones on one heat pump circuit (see module docstring)."""

    def __init__(
        self,
        system: HeatPumpSystem,
        zones: Sequence[ZoneParameters],
        couplings: Sequence[ZoneCoupling] = (),
    ) -> None:
        self.system = system
        self.zones = list(zones)
        self.n = len(self.zones)
        self.model = MultiZoneBuildingModel(self.zones, couplings, system.dt)
        # Single-zone models of each zone, for the demand of its room controllers.
        self.zone_models = [BuildingModel(z.building, system.dt) for z in self.zones]
        total = sum(max(0.0, z.weight) for z in self.zones)
        self.weights = [
            (max(0.0, z.weight) / total) if total > 0 else 1.0 / self.n for z in self.zones
        ]
        loss = sum(z.building.loss_w_per_k for z in self.zones)
        self.loss_shares = [z.building.loss_w_per_k / loss for z in self.zones]

    # ------------------------------------------------------------------

    def initial_state(
        self,
        rooms: Sequence[float],
        dhw_c: float,
        outdoor_c: float,
        slabs: Optional[Sequence[Optional[float]]] = None,
    ) -> MultiZoneState:
        """State from measured zone rooms; a missing slab is its steady state."""
        slab_list: list[float] = []
        for i, room in enumerate(rooms):
            given = slabs[i] if slabs is not None and i < len(slabs) else None
            slab_list.append(
                float(given)
                if given is not None
                else self.zone_models[i].steady_slab_c(room, outdoor_c, 0.0)
            )
        shared = ThermalState(
            slab_c=self._mean_slab(slab_list), room_c=self._mean_room(rooms), dhw_c=dhw_c
        )
        return MultiZoneState(slab_list, list(rooms), shared)

    def _mean_room(self, rooms: Sequence[float]) -> float:
        caps = [z.building.room_capacity_wh_per_k for z in self.zones]
        return float(sum(c * t for c, t in zip(caps, rooms)) / sum(caps))

    def _mean_slab(self, slabs: Sequence[float]) -> float:
        caps = [z.building.slab_capacity_wh_per_k for z in self.zones]
        return float(sum(c * t for c, t in zip(caps, slabs)) / sum(caps))

    def _demand_w(
        self,
        i: int,
        slab_c: float,
        room_c: float,
        outdoor_c: float,
        gains_w: float,
        target_c: float,
    ) -> float:
        """Heat the room controllers of zone ``i`` let in to reach ``target_c``.

        Same law as the single-zone simulator: steady demand plus a screed
        correction within ``slab_time_constant_h``.
        """
        model = self.zone_models[i]
        hp = self.system.hp
        steady = model.steady_heat_w(target_c, outdoor_c, gains_w)
        slab_star = model.steady_slab_c(target_c, outdoor_c, gains_w)
        slab_star += hp.room_gain_k_per_k * (target_c - room_c)
        correction = (
            (slab_star - slab_c) * model.params.slab_capacity_wh_per_k / hp.slab_time_constant_h
        )
        return max(0.0, steady + correction)

    def _zone_gains_w(self, i: int, inputs: SlotInputs, room_c: float) -> float:
        p = self.zones[i].building
        gains = p.internal_gains_w * max(0.0, inputs.internal_gains_factor)
        gains += p.solar_aperture_m2 * max(0.0, inputs.ghi_w_per_m2)
        if inputs.ventilation_w_per_k > 0:
            gains -= inputs.ventilation_w_per_k * self.loss_shares[i] * (room_c - inputs.outdoor_c)
        return gains

    def _flow_c(self, slab_c: float, heat_w: float, emitter_w_per_k: float) -> float:
        return slab_c + max(0.0, heat_w) / emitter_w_per_k + self.system.hp.flow_spread_k / 2.0

    # ------------------------------------------------------------------

    def step(
        self,
        state: MultiZoneState,
        inputs: SlotInputs,
        zone_modes: Sequence[HeatingMode],
        dhw_mode: DhwMode = DhwMode.HOLD,
        rod_target_c: Optional[float] = None,
        zone_inputs: Optional[Sequence[SlotInputs]] = None,
    ) -> MultiZoneResult:
        """Advance one slot.

        Args:
            state: State at the start of the slot.
            inputs: Inputs of the slot (outdoor, prices-free inputs, hot water).
            zone_modes: Heating mode per zone.
            dhw_mode: Hot water mode.
            rod_target_c: Rod target in ``DhwMode.ROD``.
            zone_inputs: Comfort band per zone (e.g. a set-back zone); defaults to ``inputs``.
        """
        if len(zone_modes) != self.n:
            raise ValueError(f"Expected {self.n} zone modes, got {len(zone_modes)}.")
        system = self.system
        hp = system.hp
        dt = system.dt
        per_zone = list(zone_inputs) if zone_inputs is not None else [inputs] * self.n
        modes = [HeatingMode(m) for m in zone_modes]
        for i, mode in enumerate(modes):
            if per_zone[i].reduce_locked and mode == HeatingMode.REDUCED:
                modes[i] = HeatingMode.NORMAL
        if system.comfort.fixed_room_setpoints and aggregate_heating_mode(modes) != (
            HeatingMode.REDUCED
        ):
            # Fixed room setpoints: a zone is only reduced through SG Ready "reduced"
            # for the whole circuit; otherwise its controllers keep their setpoint.
            modes = [HeatingMode.NORMAL if m == HeatingMode.REDUCED else m for m in modes]
        agg_mode = aggregate_heating_mode(modes)

        # Hot water, standby, §14a and cooling from the aggregate system.
        aggregate = state.shared.copy()
        aggregate.slab_c = self._mean_slab(state.slabs)
        aggregate.room_c = self._mean_room(state.rooms)
        agg = system.step(aggregate, inputs, agg_mode, dhw_mode, rod_target_c)
        dhw_heat_w = agg.heat_dhw_hp_wh / dt
        dhw_fraction = min(1.0, dhw_heat_w / hp.dhw_heat_w) if hp.dhw_heat_w > 0 else 0.0
        agg_space_w = agg.heat_space_wh / dt
        other_el_w = agg.hp_electric_wh / dt
        if agg_space_w > 0 and agg.cop_space > 0:
            other_el_w -= agg_space_w / agg.cop_space  # hot water, standby, cooling

        heats = [0.0] * self.n
        forced_w = 0.0
        flow = 0.0
        cop_space = 0.0
        if inputs.cooling_season or agg.state.standby:
            # Cooling and standby are not distributed per zone: the aggregate decides;
            # its space heat (negative when cooling) is shared by emitter size.
            emit = [z.building.emitter_w_per_k for z in self.zones]
            heats = [agg_space_w * e / sum(emit) for e in emit]
            flow, cop_space = agg.flow_c, agg.cop_space
            space_el_w = 0.0  # already in the aggregate electric figure
            other_el_w = agg.hp_electric_wh / dt
        else:
            max_mean = hp.max_flow_c - hp.flow_spread_k / 2.0
            # One heating curve for the shared circuit (RL29 fork, flow offsets).
            curve_mean = system.curve_mean_flow_c(inputs, 0.0)
            wanted: list[float] = []
            minimum: list[float] = []
            limit: list[float] = []
            for i, zone in enumerate(self.zones):
                zi = per_zone[i]
                p = zone.building
                gains = self._zone_gains_w(i, zi, state.rooms[i])
                low, setpoint, high = system.comfort_band(zi)
                target = system._room_target(modes[i], zi)
                w, room_max = system.heat_request_w(
                    modes[i],
                    zi,
                    self._demand_w(i, state.slabs[i], state.rooms[i], zi.outdoor_c, gains, target),
                    state.slabs[i],
                    state.rooms[i],
                    target,
                    p.emitter_w_per_k,
                    curve_mean_c=curve_mean,
                )
                m = self._demand_w(
                    i,
                    state.slabs[i],
                    state.rooms[i],
                    zi.outdoor_c,
                    gains,
                    min(low + REPAIR_MARGIN_K, setpoint),
                )
                if state.rooms[i] >= room_max:
                    w = m = 0.0
                cap = max(0.0, (max_mean - state.slabs[i]) * p.emitter_w_per_k)
                limit.append(cap)
                wanted.append(min(w, cap))
                minimum.append(min(m, cap))
            # Shared heat pump: thermal and electric limit at the common flow temperature.
            q = [max(w, m) for w, m in zip(wanted, minimum)]
            forced_w = sum(max(0.0, m - w) for w, m in zip(wanted, minimum))
            for _ in range(3):
                active = [i for i in range(self.n) if q[i] > 0]
                flow = max(
                    (
                        self._flow_c(state.slabs[i], q[i], self.zones[i].building.emitter_w_per_k)
                        for i in active
                    ),
                    default=0.0,
                )
                cop_space = system.cop.cop(inputs.outdoor_c, flow) if active else 0.0
                capacity = hp.max_heat_w * max(0.0, 1.0 - dhw_fraction)
                if cop_space > 0:
                    capacity = min(capacity, max(0.0, hp.max_electric_w - other_el_w) * cop_space)
                total = sum(q)
                if total > capacity > 0:
                    scale = capacity / total
                    q = [v * scale for v in q]
                    forced_w = min(forced_w, sum(q))
                elif capacity <= 0:
                    q = [0.0] * self.n
            # §14a dimming: the electric limit of the slot caps space heating too.
            if inputs.max_electric_w is not None and cop_space > 0:
                available = max(0.0, inputs.max_electric_w - other_el_w - hp.standby_w)
                total = sum(q)
                if total / cop_space > available:
                    scale = available * cop_space / total if total > 0 else 0.0
                    q = [v * scale for v in q]
                    forced_w = min(forced_w, sum(q))
            heats = q
            space_el_w = sum(q) / cop_space if cop_space > 0 and sum(q) > 0 else 0.0

        gains_list = [self._zone_gains_w(i, per_zone[i], state.rooms[i]) for i in range(self.n)]
        slabs, rooms = self.model.step(
            state.slabs, state.rooms, heats, inputs.outdoor_c, gains_list
        )

        comfort = []
        overheat = []
        for i in range(self.n):
            low, _, high = system.comfort_band(per_zone[i])
            comfort.append(max(0.0, low - rooms[i]) * dt)
            overheat.append(max(0.0, rooms[i] - high) * dt)
        shared = agg.state.copy()
        shared.slab_c = self._mean_slab(slabs)
        shared.room_c = self._mean_room(rooms)
        return MultiZoneResult(
            state=MultiZoneState(slabs, rooms, shared),
            heating_mode=agg_mode,
            zone_heat_wh=[h * dt for h in heats],
            hp_electric_wh=(other_el_w + space_el_w) * dt,
            rod_electric_wh=agg.rod_electric_wh,
            heat_dhw_wh=agg.heat_dhw_hp_wh + agg.heat_dhw_rod_wh,
            flow_c=flow,
            cop_space=cop_space,
            forced_space_wh=forced_w * dt,
            comfort_violation_kh=float(sum(w * c for w, c in zip(self.weights, comfort))),
            overheat_kh=float(sum(w * o for w, o in zip(self.weights, overheat))),
            dhw_violation_kh=agg.dhw_violation_kh,
            zone_comfort_violation_kh=comfort,
            zone_overheat_kh=overheat,
            zone_modes=modes,
        )
