"""Heat pump system simulation: building, hot water tank, heat pump and heating rod.

The simulator advances the thermal state by one slot for given operating modes.
It is deliberately independent of any optimizer: GENETIC calls ``step`` per slot
from its simulation loop, other solvers (dynamic programming, MPC) can use the
same function as transition model.

Operating modes per slot
------------------------

Space heating (``HeatingMode``):

- ``REDUCED``: the room may cool down to the comfort minimum of the slot.
- ``NORMAL``: room target = setpoint of the slot; the heat pump covers what the
  building needs, like its own heating curve with room thermostats.
- ``RAISED``: room target = setpoint + ``raised_offset_k`` (SG Ready "increased
  operation"), at most the comfort maximum.
- ``PREHEAT``: room target = comfort maximum with all valves forced open;
  charges the screed for later hours.
- ``FORCED``: SG Ready "forced on" (state 4): the heat pump runs at full
  capacity until the room reaches comfort maximum + ``forced_overshoot_k``.

``RAISED`` and ``FORCED`` can also raise the hot water setpoint, as many heat
pumps do on SG Ready 3/4 (``DhwControlParameters.raised_setpoint_c`` /
``forced_setpoint_c``; None = the mode leaves hot water alone).

Hot water (``DhwMode``):

- ``HOLD``: no active charging; the heat pump only reheats when the tank falls
  below its switch-on temperature (setpoint minus hysteresis), as the heat pump
  controller would do on its own.
- ``HEAT_PUMP``: charge to the heat pump setpoint now (hot water boost).
- ``ROD``: the heating rod charges to a target temperature given per slot
  (``rod_target_c``). The heat pump itself never charges above
  ``HeatPumpParameters.max_dhw_c``.

Comfort repair: if a mode would let the room fall below its comfort minimum or
the tank below its minimum, the simulator raises the heat delivery as the
real controllers would. The forced energy is reported, so the optimizer pays for
it and learns to avoid it.

Power limit: an optional electric limit per slot (e.g. a §14a EnWG dimming
window) caps heat pump plus heating rod.

Units: temperatures degC, power W, energy Wh per slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from akkudoktoreos.devices.thermal.building import BuildingModel, BuildingParameters
from akkudoktoreos.devices.thermal.cop import CopModel
from akkudoktoreos.devices.thermal.dhw import DhwTank, DhwTankParameters


# The controllers aim a little above the comfort minimum: the screed reacts
# with a delay, a target exactly at the minimum undershoots it.
REPAIR_MARGIN_K = 0.3


class HeatingMode(IntEnum):
    """Space heating operating mode of a slot."""

    REDUCED = 0
    NORMAL = 1
    RAISED = 2
    PREHEAT = 3
    FORCED = 4


class DhwMode(IntEnum):
    """Hot water operating mode of a slot."""

    HOLD = 0
    HEAT_PUMP = 1
    ROD = 2


@dataclass(frozen=True)
class HeatPumpParameters:
    """Heat pump unit."""

    max_heat_w: float = 10000.0
    """Maximum thermal output [W] (WBB 12: 3-10 kW at A2/W35)."""

    max_electric_w: float = 5500.0
    """Maximum compressor electric power [W]."""

    standby_w: float = 22.0
    """Electric standby power of controller and outdoor unit [W]."""

    max_flow_c: float = 55.0
    """Highest flow temperature the optimizer may request for space heating [degC]."""

    flow_spread_k: float = 3.0
    """Temperature difference flow - return in heating operation [K]."""

    dhw_heat_w: float = 5000.0
    """Thermal power while charging the hot water tank [W]."""

    dhw_flow_overshoot_k: float = 7.0
    """Flow temperature above tank temperature while charging hot water [K]."""

    room_gain_k_per_k: float = 2.0
    """Screed target raise per K room deficit (controller gain)."""

    slab_time_constant_h: float = 1.0
    """Time constant of the screed temperature correction [h]."""

    max_dhw_c: float = 60.0
    """Highest hot water temperature the heat pump reaches by itself [degC]."""

    forced_overshoot_k: float = 1.0
    """FORCED mode heats until comfort maximum + this offset [K]."""


@dataclass(frozen=True)
class ComfortParameters:
    """Operating offsets for space heating."""

    raised_offset_k: float = 1.0
    """Room target above the setpoint in RAISED mode [K]."""


@dataclass(frozen=True)
class DhwControlParameters:
    """Hot water set points."""

    setpoint_c: float = 45.0
    """Heat pump hot water setpoint ("WW Normal")."""

    hysteresis_k: float = 5.0
    """Heat pump reheats below setpoint - hysteresis."""

    min_c: float = 40.0
    """Comfort minimum of the tank."""

    rod_power_w: float = 3000.0
    """Heating rod power [W]; 0 disables the rod."""

    rod_max_c: float = 70.0
    """Highest tank temperature the rod may reach [degC]."""

    raised_setpoint_c: Optional[float] = None
    """Hot water setpoint while heating is RAISED (SG Ready 3); None = unchanged."""

    forced_setpoint_c: Optional[float] = None
    """Hot water setpoint while heating is FORCED (SG Ready 4); None = unchanged."""


@dataclass
class ThermalState:
    """State carried from slot to slot."""

    slab_c: float
    room_c: float
    dhw_c: float
    hp_dhw_charging: bool = False
    """Heat pump is in an active hot water charge (hysteresis memory)."""

    def copy(self) -> "ThermalState":
        return ThermalState(self.slab_c, self.room_c, self.dhw_c, self.hp_dhw_charging)


@dataclass
class SlotInputs:
    """Exogenous inputs of one slot."""

    outdoor_c: float
    comfort_min_c: float
    """Lowest acceptable room temperature in this slot."""
    setpoint_c: Optional[float] = None
    """Room setpoint in NORMAL mode; defaults to the comfort minimum."""
    comfort_max_c: Optional[float] = None
    """Highest room temperature the optimizer may heat to; defaults to setpoint + 1 K."""
    ghi_w_per_m2: float = 0.0
    draw_wh: float = 0.0
    """Hot water tapped in this slot [Wh]."""
    max_electric_w: Optional[float] = None
    """Electric limit for heat pump plus rod in this slot (§14a dimming), None = no limit."""


@dataclass
class SlotResult:
    """Outcome of one slot."""

    state: ThermalState
    hp_electric_wh: float
    rod_electric_wh: float
    heat_space_wh: float
    heat_dhw_hp_wh: float
    heat_dhw_rod_wh: float
    cop_space: float
    cop_dhw: float
    flow_c: float
    forced_space_wh: float = 0.0
    """Heat delivered beyond the chosen mode to keep the comfort minimum [Wh]."""
    forced_dhw_wh: float = 0.0
    comfort_violation_kh: float = 0.0
    """Room temperature below the comfort minimum, integrated [K*h]."""
    dhw_violation_kh: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def electric_wh(self) -> float:
        return self.hp_electric_wh + self.rod_electric_wh


class HeatPumpSystem:
    """Simulates building, tank and generators for a fixed slot length."""

    def __init__(
        self,
        building: BuildingParameters,
        tank: DhwTankParameters,
        cop: CopModel,
        heat_pump: HeatPumpParameters = HeatPumpParameters(),
        comfort: ComfortParameters = ComfortParameters(),
        dhw: DhwControlParameters = DhwControlParameters(),
        slot_hours: float = 0.25,
    ) -> None:
        self.building = BuildingModel(building, slot_hours)
        self.tank = DhwTank(tank, slot_hours)
        self.cop = cop
        self.hp = heat_pump
        self.comfort = comfort
        self.dhw = dhw
        self.dt = float(slot_hours)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def comfort_band(inputs: "SlotInputs") -> tuple[float, float, float]:
        """(minimum, setpoint, maximum) of the slot with defaults filled in."""
        low = inputs.comfort_min_c
        setpoint = inputs.setpoint_c if inputs.setpoint_c is not None else low
        setpoint = max(setpoint, low)
        high = inputs.comfort_max_c if inputs.comfort_max_c is not None else setpoint + 1.0
        return low, setpoint, max(high, setpoint)

    def _room_target(self, mode: HeatingMode, inputs: "SlotInputs") -> float:
        low, setpoint, high = self.comfort_band(inputs)
        if mode == HeatingMode.REDUCED:
            return min(low + REPAIR_MARGIN_K, setpoint)
        if mode == HeatingMode.NORMAL:
            return setpoint
        if mode == HeatingMode.RAISED:
            return min(setpoint + self.comfort.raised_offset_k, high)
        if mode == HeatingMode.FORCED:
            return high + self.hp.forced_overshoot_k
        return high

    def dhw_setpoint_c(self, heating_mode: HeatingMode) -> float:
        """Heat pump hot water setpoint in effect for the heating mode."""
        setpoint = self.dhw.setpoint_c
        if heating_mode == HeatingMode.RAISED and self.dhw.raised_setpoint_c is not None:
            setpoint = max(setpoint, self.dhw.raised_setpoint_c)
        elif heating_mode == HeatingMode.FORCED and self.dhw.forced_setpoint_c is not None:
            setpoint = max(setpoint, self.dhw.forced_setpoint_c)
        return min(setpoint, self.hp.max_dhw_c)

    def reachable_dhw_c(self, heating_modes: "list[int] | tuple[int, ...]") -> float:
        """Highest tank temperature the heat pump reaches with the given heating modes."""
        return max(self.dhw_setpoint_c(HeatingMode(mode)) for mode in heating_modes)

    def _space_heat_capacity_w(self, outdoor_c: float, slab_c: float) -> tuple[float, float, float]:
        """Max space heat [W] within thermal, electric and flow limits; (heat, flow, cop)."""
        heat = self.hp.max_heat_w
        for _ in range(3):  # fixed point: flow temperature depends on heat
            flow = self.building.flow_temperature_c(slab_c, heat, self.hp.flow_spread_k)
            if flow > self.hp.max_flow_c:
                # Heat that keeps the flow at its maximum.
                mean_max = self.hp.max_flow_c - self.hp.flow_spread_k / 2.0
                heat = max(0.0, (mean_max - slab_c) * self.building.params.emitter_w_per_k)
                flow = self.building.flow_temperature_c(slab_c, heat, self.hp.flow_spread_k)
            cop = self.cop.cop(outdoor_c, flow)
            heat = min(heat, self.hp.max_electric_w * cop)
        flow = self.building.flow_temperature_c(slab_c, heat, self.hp.flow_spread_k)
        return heat, flow, self.cop.cop(outdoor_c, flow)

    def _space_heat_demand_w(
        self, state: ThermalState, outdoor_c: float, gains_w: float, room_target_c: float
    ) -> float:
        """Heat [W] the heating system delivers to reach ``room_target_c``.

        Feed-forward of the steady heat demand plus a correction that moves the
        screed towards the temperature that holds the room at the target. Room
        deficits raise the screed target (``room_gain_k_per_k``); the screed is
        corrected within ``slab_time_constant_h``. This mimics a heating curve
        with room thermostats, without the bang-bang of a one-slot deadbeat.
        """
        steady = self.building.steady_heat_w(room_target_c, outdoor_c, gains_w)
        slab_star = self.building.steady_slab_c(room_target_c, outdoor_c, gains_w)
        slab_star += self.hp.room_gain_k_per_k * (room_target_c - state.room_c)
        correction = (
            (slab_star - state.slab_c)
            * self.building.params.slab_capacity_wh_per_k
            / self.hp.slab_time_constant_h
        )
        return max(0.0, steady + correction)

    # ------------------------------------------------------------------
    # Step
    # ------------------------------------------------------------------

    def step(
        self,
        state: ThermalState,
        inputs: SlotInputs,
        heating_mode: HeatingMode = HeatingMode.NORMAL,
        dhw_mode: DhwMode = DhwMode.HOLD,
        rod_target_c: Optional[float] = None,
    ) -> SlotResult:
        """Advance one slot.

        Args:
            state: Thermal state at the start of the slot.
            inputs: Exogenous inputs of the slot.
            heating_mode: Space heating mode.
            dhw_mode: Hot water mode.
            rod_target_c: Rod target temperature in ``DhwMode.ROD``; defaults
                to ``rod_max_c``.
        """
        dt = self.dt
        notes: list[str] = []
        gains = self.building.gains_w(inputs.ghi_w_per_m2)

        # --- Hot water: decide heat pump share of the slot ---------------
        hp_setpoint = self.dhw_setpoint_c(heating_mode)
        switch_on_c = self.dhw.setpoint_c - self.dhw.hysteresis_k
        # The controller checks continuously: a tank that falls below the
        # switch-on temperature within this slot starts a charge in this slot.
        hp_dhw_active = (
            state.hp_dhw_charging
            or state.dhw_c < switch_on_c
            or self.tank.step(state.dhw_c, 0.0, inputs.draw_wh) < switch_on_c
        )
        forced_dhw = False
        raised_dhw = hp_setpoint > self.dhw.setpoint_c
        if (dhw_mode == DhwMode.HEAT_PUMP or raised_dhw) and state.dhw_c < hp_setpoint - 0.2:
            hp_dhw_active = True
        elif hp_dhw_active and dhw_mode != DhwMode.HEAT_PUMP and not state.hp_dhw_charging:
            forced_dhw = True  # own controller reheats below switch-on temperature

        rod_target: Optional[float] = None
        if dhw_mode == DhwMode.ROD:
            rod_target = min(
                rod_target_c if rod_target_c is not None else self.dhw.rod_max_c,
                self.dhw.rod_max_c,
            )

        # Rod first (it can run in parallel to the heat pump on the tank).
        rod_w = 0.0
        if rod_target is not None and self.dhw.rod_power_w > 0 and state.dhw_c < rod_target:
            need = self.tank.heat_to_reach(state.dhw_c, rod_target, inputs.draw_wh)
            rod_w = min(self.dhw.rod_power_w, max(0.0, need))
            # The heat pump controller does not know the rod: below its switch-on
            # temperature it still charges (to its own setpoint, net of the rod).

        hp_dhw_w = 0.0
        dhw_fraction = 0.0
        cop_dhw = 0.0
        if hp_dhw_active:
            need = self.tank.heat_to_reach(state.dhw_c, hp_setpoint, inputs.draw_wh) - rod_w
            if need > 0:
                hp_dhw_w = min(self.hp.dhw_heat_w, need)
                dhw_fraction = hp_dhw_w / self.hp.dhw_heat_w
                flow_dhw = (state.dhw_c + hp_setpoint) / 2 + self.hp.dhw_flow_overshoot_k
                cop_dhw = self.cop.cop(inputs.outdoor_c, flow_dhw)
            else:
                hp_dhw_active = False

        # --- Space heating ------------------------------------------------
        _, _, room_max = self.comfort_band(inputs)
        room_target = self._room_target(heating_mode, inputs)
        wanted = self._space_heat_demand_w(state, inputs.outdoor_c, gains, room_target)
        if heating_mode == HeatingMode.FORCED:
            room_max = room_target  # forced on: heat beyond the comfort maximum
            wanted = float("inf")  # full capacity
        minimum = self._space_heat_demand_w(
            state,
            inputs.outdoor_c,
            gains,
            min(inputs.comfort_min_c + REPAIR_MARGIN_K, self.comfort_band(inputs)[1]),
        )
        if state.room_c >= room_max:
            wanted = 0.0
            minimum = 0.0

        capacity, _, _ = self._space_heat_capacity_w(inputs.outdoor_c, state.slab_c)
        capacity *= max(0.0, 1.0 - dhw_fraction)

        space_w = min(wanted, capacity)
        forced_space_w = 0.0
        if space_w < minimum:
            forced_space_w = min(minimum, capacity) - space_w
            space_w = min(minimum, capacity)
        flow = self.building.flow_temperature_c(state.slab_c, space_w, self.hp.flow_spread_k)
        cop_space = self.cop.cop(inputs.outdoor_c, flow) if space_w > 0 else 0.0

        # --- Electric power and limit --------------------------------------
        def electric(space: float, dhw_hp: float, rod: float) -> float:
            hp_el = (space / cop_space if space > 0 else 0.0) + (
                dhw_hp / cop_dhw if dhw_hp > 0 else 0.0
            )
            return hp_el + rod

        limit = inputs.max_electric_w
        if limit is not None:
            available = max(0.0, limit - self.hp.standby_w)
            total = electric(space_w, hp_dhw_w, rod_w)
            if total > available:
                notes.append("power limit")
                # Reduce the rod first, then space heating, then hot water.
                excess = total - available
                cut = min(rod_w, excess)
                rod_w -= cut
                excess -= cut
                if excess > 0 and space_w > 0:
                    cut_heat = min(space_w, excess * cop_space)
                    space_w -= cut_heat
                    forced_space_w = min(forced_space_w, space_w)
                    excess -= cut_heat / cop_space
                if excess > 0 and hp_dhw_w > 0:
                    hp_dhw_w = max(0.0, hp_dhw_w - excess * cop_dhw)

        hp_el_w = (space_w / cop_space if space_w > 0 else 0.0) + (
            hp_dhw_w / cop_dhw if hp_dhw_w > 0 else 0.0
        )
        hp_el_w += self.hp.standby_w

        # --- Advance states -------------------------------------------------
        slab, room = self.building.step(
            state.slab_c, state.room_c, space_w, inputs.outdoor_c, gains
        )
        dhw = self.tank.step(state.dhw_c, hp_dhw_w + rod_w, inputs.draw_wh)
        still_charging = hp_dhw_active and dhw < hp_setpoint - 0.1

        comfort_violation = max(0.0, inputs.comfort_min_c - room) * dt
        dhw_violation = max(0.0, self.dhw.min_c - dhw) * dt

        return SlotResult(
            state=ThermalState(slab, room, dhw, still_charging),
            hp_electric_wh=hp_el_w * dt,
            rod_electric_wh=rod_w * dt,
            heat_space_wh=space_w * dt,
            heat_dhw_hp_wh=hp_dhw_w * dt,
            heat_dhw_rod_wh=rod_w * dt,
            cop_space=cop_space,
            cop_dhw=cop_dhw,
            flow_c=flow,
            forced_space_wh=forced_space_w * dt,
            forced_dhw_wh=(hp_dhw_w * dt) if forced_dhw else 0.0,
            comfort_violation_kh=comfort_violation,
            dhw_violation_kh=dhw_violation,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Value of stored heat
    # ------------------------------------------------------------------

    def stored_heat_wh(
        self, state: ThermalState, room_reference_c: float, dhw_reference_c: float
    ) -> tuple[float, float]:
        """Heat stored above reference temperatures: (building, tank) [Wh]."""
        building = self.building.stored_heat_wh(state.slab_c, state.room_c, room_reference_c)
        tank = self.tank.stored_heat_wh(state.dhw_c, dhw_reference_c)
        return building, tank
