"""Linear heat pump model for the dispatch MILP (RL29 fork).

The exact heat pump simulation (``devices/thermal/heatpump.py``) works with operating
modes and controller logic; the MILP instead plans the *physics*: how much heat goes
into the building and the hot water tank in every slot. Building and tank are linear
(two-node building model and single-node tank, both discretised exactly), the only
non-linearity is the COP:

- space heating: the electric power ``f(q) = q / COP(T_out, T_flow(q))`` is convex in
  the heat ``q`` (a higher heat needs a higher flow temperature and gets a lower COP).
  It is interpolated between a few breakpoints (convex combination, exact at the
  breakpoints, no binaries). An equality, not a lower bound: at negative prices the
  model must not "burn" electricity the heat pump cannot take;
- hot water: COP per slot at the charging flow temperature.

Together with battery, grid and appliances the solver then finds the cost-optimal heat
schedule of the whole horizon in one solve: when to store heat in the screed, when to
charge the tank, how far the room may cool down, what the stored heat is worth at the
end. ``project_modes`` turns that heat schedule into operating modes (the only thing
Home Assistant can execute) by following it with the exact simulation; the result is
scored and improved like every other candidate.

Not modelled (left to the exact simulation and the search): cooling, mode change and
active mode penalties, the heat pump's own hot water hysteresis beyond a lower bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Sequence

from akkudoktoreos.devices.thermal.heatpump import (
    REPAIR_MARGIN_K,
    DhwMode,
    HeatingMode,
    ThermalState,
)

if TYPE_CHECKING:  # pragma: no cover
    from akkudoktoreos.optimization.genetic.heatpumpgenes import HeatPumpRunPlan

SPACE_BREAKPOINTS = 4
"""Secants of the space heating electricity curve."""

RETURN_READY_HOURS = 2.0
"""Same weight as the exact simulation gives a cold tank at the holiday return."""


@dataclass
class ModeControl:
    """Heating modes as binaries (RL29 fork, ``milp.joint_modes``).

    The controller's space heat in mode ``m`` and slot ``t`` is
    ``clip(const[t][m] + k_room[t] * room + k_slab[t] * slab, 0, capacity)`` with the
    room and screed temperature at the slot start (``HeatPumpSystem.step``: feed-forward
    of the steady demand plus the screed correction, the comfort minimum as floor;
    FORCED runs at full capacity). One binary per gene period and mode selects the mode.
    """

    period_of_slot: list[int]
    n_periods: int
    modes: list[int]
    """Allowed heating modes (``HeatingMode`` values) in gene order."""
    const_w: list[list[float]]
    """Per slot and mode: the state-independent part of the heat [W]."""
    k_room: list[float]
    k_slab: list[float]
    period_hours: list[float]
    change_penalty: float = 0.0
    active_penalty: float = 0.0
    initial_mode: Optional[int] = None
    """Gene index in effect now (a different first mode counts as a change)."""
    fixed_periods: dict[int, int] = field(default_factory=dict)
    """Periods whose mode the hold time keeps (gene index)."""
    deviation_cost_per_wh: float = 0.002
    """Cost of heat that differs from the selected mode's controller heat [currency/Wh]."""
    integer_periods: Optional[set[int]] = None
    """Periods with integral mode and clipping binaries; the others are relaxed to
    [0, 1] (relax-and-fix). None = all periods integral."""

    def integral(self, period: int) -> bool:
        """Binaries of the period are integral: in the window or already fixed."""
        return (
            self.integer_periods is None
            or period in self.integer_periods
            or period in self.fixed_periods
        )


@dataclass
class ThermalBlock:
    """Numbers of one heat pump for the MILP; built from a run plan."""

    n: int
    dt: float
    # Building: x' = A x + B [q, T_out, gains]; ventilation folded into A/B per slot.
    a: list[list[float]]
    b: list[list[float]]
    slab_capacity: float
    room_capacity: float
    slab0: float
    room0: float
    outdoor_c: list[float]
    gains_w: list[float]
    vent_w_per_k: list[float]
    q_cap_w: list[float]
    space_points: list[list[tuple[float, float]]]
    """Per slot: breakpoints (heat W, electric W) of the space heating curve."""
    room_min_c: list[float]
    room_floor_c: list[Optional[float]]
    """Lowest room target of the allowed modes when REDUCED is not allowed (with REDUCED
    the comfort minimum is the floor; a free fall towards it tested better than holding
    the REDUCED target, the projection keeps the controller realistic)."""
    room_max_c: list[float]
    room_ceiling_c: list[float]
    """Highest room target any allowed mode reaches (plus a margin)."""
    comfort_penalty: float
    overheat_penalty: float
    # Tank
    tank0: float
    tank_capacity: float
    tank_decay: float
    tank_gain: float
    tank_ambient_c: float
    draw_w: list[float]
    dhw_heat_w: float
    cop_dhw: list[float]
    hp_dhw_max_c: float
    tank_floor_c: list[Optional[float]]
    dhw_penalty: float
    rod_power_w: float
    rod_max_c: float
    standby_w: float
    max_electric_w: list[Optional[float]]
    idle_w: list[float] = field(default_factory=list)
    """Electric power without operation per slot: standby_w, or the standby operating
    mode's off_w where the controller may use it (holiday)."""
    legionella_c: Optional[float] = None
    legionella_deadline: Optional[int] = None
    legionella_penalty: float = 0.0
    return_slot: Optional[int] = None
    return_min_c: float = 0.0
    building_heat_value: float = 0.0
    room_cap_end_c: float = 99.0
    tank_value_below: float = 0.0
    tank_value_above: float = 0.0
    tank_setpoint_c: float = 45.0
    separate_price_per_wh: Optional[list[float]] = None
    device_id: str = ""
    rod_cap_w: list[float] = field(default_factory=list)
    """Fork: rod power cap per slot (PV surplus with rod_planned_surplus_only); empty =
    rod_power_w in every slot."""
    mode_control: Optional[ModeControl] = None
    """Fork: heating modes as binaries; None = free heat schedule (projected later)."""


@dataclass
class ThermalVars:
    hp_wh: list[int]
    q: list[int]
    w: list[int]
    r: list[int]
    slab: list[int]
    room: list[int]
    tank: list[int]
    mode: list[list[int]] = field(default_factory=list)
    """Per gene period: one binary per allowed heating mode (``ModeControl``)."""


@dataclass
class ThermalTrajectory:
    """MILP heat schedule of one heat pump (states at the end of every slot)."""

    space_heat_w: list[float]
    dhw_heat_w: list[float]
    rod_w: list[float]
    electric_wh: list[float]
    slab_c: list[float]
    room_c: list[float]
    tank_c: list[float]
    extra: dict[str, Any] = field(default_factory=dict)
    heating_genes: Optional[list[int]] = None
    """Heating mode gene index per period when the modes were planned (``ModeControl``)."""


# ----------------------------------------------------------------------
# Building the block from a run plan
# ----------------------------------------------------------------------


def _breakpoints(f: Any, cap: float, k: int = SPACE_BREAKPOINTS) -> list[tuple[float, float]]:
    """(x, f(x)) on k+1 equidistant points of [0, cap]."""
    if cap <= 0:
        return [(0.0, 0.0)]
    return [(cap * i / k, float(f(cap * i / k))) for i in range(k + 1)]


FORCED_HEAT_W = 1e6
"""State-independent heat of FORCED: far above any capacity, so it clips to full power."""


def mode_heat_coefficients(
    system: Any, inputs: Any, room_target_c: float, room_ref_c: float
) -> tuple[float, float, float]:
    """(const, k_room, k_slab) of the controller's unclipped heat for a room target.

    ``HeatPumpSystem._space_heat_demand_w`` is
    ``steady(T) + (steady_slab(T) + k (T - room) - slab) * C_slab / tau``; the
    airing loss enters the gains with the room temperature at the slot start, which is
    taken from ``room_ref_c`` here so the expression stays affine in the state.
    """
    building = system.building
    hp = system.hp
    out = float(inputs.outdoor_c)
    gains = system.slot_gains_w(inputs)
    if inputs.ventilation_w_per_k > 0:
        gains -= inputs.ventilation_w_per_k * (room_ref_c - out)
    factor = building.params.slab_capacity_wh_per_k / hp.slab_time_constant_h
    steady = building.steady_heat_w(room_target_c, out, gains)
    slab_star = building.steady_slab_c(room_target_c, out, gains)
    const = steady + (slab_star + hp.room_gain_k_per_k * room_target_c) * factor
    return const, -hp.room_gain_k_per_k * factor, -factor


def mode_control(plan: "HeatPumpRunPlan", room_ref_c: Sequence[float]) -> ModeControl:
    """Heating modes of a run plan as MILP data (see ``ModeControl``)."""
    system = plan.system
    modes = [int(m) for m in plan.heating_modes]
    const_w: list[list[float]] = []
    k_room: list[float] = []
    k_slab: list[float] = []
    for t, slot in enumerate(plan.inputs):
        ref = room_ref_c[t] if t < len(room_ref_c) else plan.initial.room_c
        low, setpoint, _ = system.comfort_band(slot)
        floor_target = min(slot.comfort_min_c + REPAIR_MARGIN_K, setpoint)
        floor_const, kr, ks = mode_heat_coefficients(system, slot, floor_target, ref)
        row: list[float] = []
        for mode in modes:
            if mode == int(HeatingMode.FORCED):
                row.append(FORCED_HEAT_W)
                continue
            target = system._room_target(HeatingMode(mode), slot)
            const, _, _ = mode_heat_coefficients(system, slot, target, ref)
            row.append(max(const, floor_const))  # the comfort minimum is the floor
        const_w.append(row)
        k_room.append(kr)
        k_slab.append(ks)
    n_periods = plan.n_periods
    hours = [len(plan.period_slots(p)) * system.dt for p in range(n_periods)]
    initial = (
        plan.heating_index(plan.active_heating_mode)
        if plan.active_heating_mode is not None
        else None
    )
    return ModeControl(
        period_of_slot=[plan.period_of(t) for t in range(plan.n_slots)],
        n_periods=n_periods,
        modes=modes,
        const_w=const_w,
        k_room=k_room,
        k_slab=k_slab,
        period_hours=hours,
        change_penalty=plan.mode_change_penalty,
        active_penalty=plan.active_mode_penalty,
        initial_mode=initial,
    )


def thermal_block(plan: "HeatPumpRunPlan", joint_modes: bool = False) -> Optional[ThermalBlock]:
    """Linear model of one heat pump; None when the plan is out of the model's scope.

    With ``joint_modes`` the heating modes are binaries of the model (``ModeControl``).
    """
    inputs = plan.inputs
    n = len(inputs)
    if n == 0 or any(i.cooling_season for i in inputs):
        return None
    system = plan.system
    building = system.building
    tank = system.tank
    hp = system.hp
    dt = system.dt
    params = building.params
    # Reference trajectory (status quo) for the operating points of the COP.
    reference = plan.evaluate(plan.default_genes(), detail=True).detail or {}
    ref_slab = [float(v) for v in reference.get("slab_c", [])] or [plan.initial.slab_c] * n
    ref_tank = [float(v) for v in reference.get("dhw_c", [])] or [plan.initial.dhw_c] * n
    starts_slab = [plan.initial.slab_c] + ref_slab[:-1]
    starts_tank = [plan.initial.dhw_c] + ref_tank[:-1]

    allowed = [HeatingMode(m) for m in plan.heating_modes]
    rod_allowed = any(o.mode == DhwMode.ROD for o in plan.dhw_options)
    rod_power = system.dhw.rod_power_w if rod_allowed else 0.0
    rod_targets = [o.rod_target_c for o in plan.dhw_options if o.mode == DhwMode.ROD]
    rod_max = min(
        system.dhw.rod_max_c,
        max([t for t in rod_targets if t is not None] or [system.dhw.rod_max_c]),
    )
    hp_dhw_max = system.reachable_dhw_c([int(m) for m in allowed])
    setpoint = system.dhw.setpoint_c
    switch_on = setpoint - system.dhw.hysteresis_k

    q_cap: list[float] = []
    points: list[list[tuple[float, float]]] = []
    cop_dhw: list[float] = []
    gains: list[float] = []
    room_min: list[float] = []
    room_floor: list[Optional[float]] = []
    room_max: list[float] = []
    room_ceiling: list[float] = []
    tank_floor: list[Optional[float]] = []
    max_el: list[Optional[float]] = []
    idle_w: list[float] = []
    for t, slot in enumerate(inputs):
        out = float(slot.outdoor_c)
        slab_ref = starts_slab[t]
        cap, _, _ = system._space_heat_capacity_w(out, slab_ref)
        q_cap.append(max(0.0, cap))

        def electric(q: float, out: float = out, slab_ref: float = slab_ref) -> float:
            if q <= 0:
                return 0.0
            flow = building.flow_temperature_c(slab_ref, q, hp.flow_spread_k)
            return q / system.cop.cop(out, flow)

        points.append(_breakpoints(electric, q_cap[-1]))
        flow_dhw = (starts_tank[t] + setpoint) / 2 + hp.dhw_flow_overshoot_k
        cop_dhw.append(max(system.cop.cop(out, flow_dhw), 1.0))
        gains.append(system.slot_gains_w(slot))
        idle_w.append(system.off_w if slot.standby_possible else hp.standby_w)
        low, set_c, high = system.comfort_band(slot)
        room_min.append(low)
        room_max.append(high)
        targets = [system._room_target(m, slot) for m in allowed]
        reducible = HeatingMode.REDUCED in allowed and not slot.reduce_locked
        room_floor.append(None if reducible else min(targets))
        room_ceiling.append(max(targets) + REPAIR_MARGIN_K)
        dhw_min = slot.dhw_min_c if slot.dhw_min_c is not None else system.dhw.min_c
        tank_floor.append(None if slot.dhw_off else max(dhw_min, switch_on - 0.5))
        max_el.append(slot.max_electric_w)

    deadline = plan.legionella_deadline_slot
    if deadline is not None and plan.initial.dhw_c >= plan.legionella_c - 0.5:
        deadline = None
    tank_value_above = (
        plan.tank_heat_value_above_setpoint
        if plan.tank_heat_value_above_setpoint is not None
        else plan.tank_heat_value
    )
    return ThermalBlock(
        n=n,
        dt=dt,
        a=[[float(v) for v in row] for row in building.ad],
        b=[[float(v) for v in row] for row in building.bd],
        slab_capacity=params.slab_capacity_wh_per_k,
        room_capacity=params.room_capacity_wh_per_k,
        slab0=plan.initial.slab_c,
        room0=plan.initial.room_c,
        outdoor_c=[float(i.outdoor_c) for i in inputs],
        gains_w=gains,
        vent_w_per_k=[float(i.ventilation_w_per_k) for i in inputs],
        q_cap_w=q_cap,
        space_points=points,
        room_min_c=room_min,
        room_floor_c=room_floor,
        room_max_c=room_max,
        room_ceiling_c=room_ceiling,
        comfort_penalty=plan.comfort_penalty,
        overheat_penalty=plan.overheat_penalty,
        tank0=plan.initial.dhw_c,
        tank_capacity=tank.capacity,
        tank_decay=tank._decay,
        tank_gain=tank._input_gain,
        tank_ambient_c=tank.params.ambient_c,
        draw_w=[float(i.draw_wh) / dt for i in inputs],
        dhw_heat_w=hp.dhw_heat_w,
        cop_dhw=cop_dhw,
        hp_dhw_max_c=hp_dhw_max,
        tank_floor_c=tank_floor,
        dhw_penalty=plan.dhw_penalty,
        rod_power_w=rod_power,
        rod_max_c=rod_max,
        rod_cap_w=(
            [
                rod_power if i.rod_grid_allowed else min(rod_power, max(0.0, float(i.pv_surplus_w)))
                for i in inputs
            ]
            if rod_power > 0 and system.dhw.rod_planned_surplus_only
            else []
        ),
        standby_w=hp.standby_w,
        idle_w=idle_w,
        max_electric_w=max_el,
        legionella_c=plan.legionella_c if deadline is not None else None,
        legionella_deadline=deadline,
        legionella_penalty=plan.legionella_penalty,
        return_slot=(plan.away_slots - 1) if 0 < plan.away_slots < n else None,
        return_min_c=system.dhw.min_c,
        building_heat_value=plan.building_heat_value,
        room_cap_end_c=system.comfort_band(inputs[-1])[2],
        tank_value_below=plan.tank_heat_value,
        tank_value_above=min(tank_value_above, plan.tank_heat_value),
        tank_setpoint_c=setpoint,
        separate_price_per_wh=(
            [float(v) for v in plan.separate_meter_price_per_wh]
            if plan.separate_meter_price_per_wh is not None
            else None
        ),
        device_id=plan.device_id,
        mode_control=(
            mode_control(
                plan,
                [plan.initial.room_c]
                + [float(v) for v in reference.get("room_c", [])][: max(0, n - 1)],
            )
            if joint_modes
            else None
        ),
    )


# ----------------------------------------------------------------------
# MILP rows
# ----------------------------------------------------------------------


def add_thermal(b: Any, block: ThermalBlock) -> ThermalVars:
    """Add the variables and rows of one heat pump to a ``_Builder``.

    Costs (penalties, value of the stored heat, separate meter) go into the objective
    with weight 1; the house balance gets ``hp_wh`` from the caller.
    """
    inf = math.inf
    n, dt = block.n, block.dt
    a, bm = block.a, block.b
    slab = [b.var(block.slab0, block.slab0)]
    room = [b.var(block.room0, block.room0)]
    tank = [b.var(block.tank0, block.tank0)]
    q_vars: list[int] = []
    w_vars: list[int] = []
    r_vars: list[int] = []
    hp_wh: list[int] = []
    sep = block.separate_price_per_wh
    free = block.tank0
    mode_vars: list[list[int]] = []
    for t in range(n):
        cap = block.q_cap_w[t]
        q = b.var(0.0, cap)
        e_sp = b.var(0.0, inf)
        lambdas = [b.var(0.0, 1.0) for _ in block.space_points[t]]
        b.row({lam: 1.0 for lam in lambdas}, 1.0, 1.0)
        q_row: dict[int, float] = {q: -1.0}
        e_row: dict[int, float] = {e_sp: -1.0}
        for lam, (heat, elec) in zip(lambdas, block.space_points[t]):
            q_row[lam] = heat
            e_row[lam] = elec
        b.row(q_row, 0.0, 0.0)
        b.row(e_row, 0.0, 0.0)
        w = b.var(0.0, block.dhw_heat_w)
        if block.mode_control is not None:
            if not mode_vars:
                mode_vars.extend(_add_mode_binaries(b, block.mode_control))
            _add_mode_heat(b, block, t, q, w, room[t], slab[t], mode_vars)
        r = b.var(0.0, block.rod_cap_w[t] if t < len(block.rod_cap_w) else block.rod_power_w)
        energy = b.var(0.0, inf, (sep[t] if sep is not None and t < len(sep) else 0.0))
        idle = block.idle_w[t] if t < len(block.idle_w) else block.standby_w
        # energy [Wh] = dt * (space + dhw + rod + standby)
        b.row(
            {energy: 1.0, e_sp: -dt, w: -dt / block.cop_dhw[t], r: -dt},
            idle * dt,
            idle * dt,
        )
        # Compressor time shared between space heating and hot water.
        share: dict[int, float] = {w: 1.0 / block.dhw_heat_w}
        if cap > 0:
            share[q] = 1.0 / cap
        b.row(share, -inf, 1.0)
        limit = block.max_electric_w[t]
        if limit is not None:
            b.row(
                {e_sp: 1.0, w: 1.0 / block.cop_dhw[t], r: 1.0},
                -inf,
                max(0.0, limit - idle),
            )
        # Building (ventilation: extra loss with the room temperature at the slot start).
        v = block.vent_w_per_k[t]
        out = block.outdoor_c[t]
        g = block.gains_w[t]
        s_next = b.var(-inf, inf)
        r_next = b.var(-inf, inf)
        for target, row in ((s_next, 0), (r_next, 1)):
            const = bm[row][1] * out + bm[row][2] * (g + v * out)
            b.row(
                {
                    target: 1.0,
                    slab[t]: -a[row][0],
                    room[t]: -(a[row][1] - bm[row][2] * v),
                    q: -bm[row][0],
                },
                const,
                const,
            )
        # Tank: T' = amb + (T - amb) decay + (w + r - draw) gain
        const_t = (
            block.tank_ambient_c * (1.0 - block.tank_decay) - block.draw_w[t] * block.tank_gain
        )
        free = const_t + block.tank_decay * free  # tank without any heating
        t_next = b.var(
            -inf,
            max(block.hp_dhw_max_c, block.rod_max_c if block.rod_power_w else 0.0, free),
        )
        b.row(
            {t_next: 1.0, tank[t]: -block.tank_decay, w: -block.tank_gain, r: -block.tank_gain},
            const_t,
            const_t,
        )
        # The heat pump alone never charges above its reachable setpoint. Without a rod
        # the tank is never hotter than that (or than its own cool-down from a hotter
        # start), so one row suffices; with a rod the heat pump charges only while the
        # tank is below its setpoint (binary: charging or not).
        hp_limit = max(block.hp_dhw_max_c, free) - const_t
        if block.rod_power_w > 0:
            on = b.var(0.0, 1.0, 0.0, integer=True)
            b.row({w: 1.0, on: -block.dhw_heat_w}, -inf, 0.0)
            big = block.tank_decay * max(block.rod_max_c, block.tank0) + 1.0
            b.row({tank[t]: block.tank_decay, w: block.tank_gain, on: big}, -inf, hp_limit + big)
        else:
            b.row({tank[t]: block.tank_decay, w: block.tank_gain}, -inf, hp_limit)
        # Comfort and limits of the modes (soft).
        under = b.var(0.0, inf, block.comfort_penalty * dt)
        b.row({r_next: 1.0, under: 1.0}, block.room_min_c[t], inf)
        floor = block.room_floor_c[t]
        if floor is not None and floor > block.room_min_c[t]:
            below_floor = b.var(0.0, inf, block.comfort_penalty * dt)
            b.row({r_next: 1.0, below_floor: 1.0}, floor, inf)
        over = b.var(0.0, inf, block.overheat_penalty * dt)
        b.row({r_next: 1.0, over: -1.0}, -inf, block.room_max_c[t])
        beyond = b.var(0.0, inf, (block.overheat_penalty + block.comfort_penalty) * dt)
        b.row({r_next: 1.0, beyond: -1.0}, -inf, block.room_ceiling_c[t])
        tank_floor = block.tank_floor_c[t]
        if tank_floor is not None:
            short = b.var(0.0, inf, block.dhw_penalty * dt)
            b.row({t_next: 1.0, short: 1.0}, tank_floor, inf)
        slab.append(s_next)
        room.append(r_next)
        tank.append(t_next)
        q_vars.append(q)
        w_vars.append(w)
        r_vars.append(r)
        hp_wh.append(energy)

    if block.return_slot is not None and block.return_slot < n:
        short = b.var(0.0, inf, block.dhw_penalty * RETURN_READY_HOURS)
        b.row({tank[block.return_slot + 1]: 1.0, short: 1.0}, block.return_min_c, inf)

    if block.legionella_deadline is not None and block.legionella_c is not None:
        big = 100.0
        level = block.legionella_c - 0.5
        hits = []
        for t in range(min(block.legionella_deadline, n)):
            y = b.var(0.0, 1.0, 0.0, integer=True)
            b.row({tank[t + 1]: 1.0, y: -big}, level - big, inf)
            hits.append(y)
        miss = b.var(0.0, 1.0, block.legionella_penalty)
        b.row({**{y: 1.0 for y in hits}, miss: 1.0}, 1.0, inf)

    # Value of the stored heat at the end (constant start value left out).
    if block.building_heat_value:
        # Room heat above the comfort maximum has no value (min(room, cap)).
        room_eff = b.var(
            -inf, block.room_cap_end_c, -block.building_heat_value * block.room_capacity
        )
        b.row({room_eff: 1.0, room[n]: -1.0}, -inf, 0.0)
        b.cost[slab[n]] -= block.building_heat_value * block.slab_capacity
    if block.tank_value_below or block.tank_value_above:
        below = b.var(-inf, block.tank_setpoint_c, -block.tank_value_below * block.tank_capacity)
        above = b.var(0.0, inf, -block.tank_value_above * block.tank_capacity)
        b.row({below: 1.0, above: 1.0, tank[n]: -1.0}, 0.0, 0.0)
    return ThermalVars(hp_wh, q_vars, w_vars, r_vars, slab, room, tank, mode_vars)


ROOM_RANGE_C = (12.0, 30.0)
SLAB_RANGE_C = (12.0, 50.0)
"""Hard state bounds of the joint mode model (outside any real heating state); they keep
the big-M constants of the mode heat small enough for the solver tolerances."""


def _state_range_w(k_room: float, k_slab: float) -> tuple[float, float]:
    """Lowest and highest state-dependent heat over the state bounds [W]."""
    rooms = (k_room * ROOM_RANGE_C[0], k_room * ROOM_RANGE_C[1])
    slabs = (k_slab * SLAB_RANGE_C[0], k_slab * SLAB_RANGE_C[1])
    return min(rooms) + min(slabs), max(rooms) + max(slabs)


def _add_mode_binaries(b: Any, mc: ModeControl) -> list[list[int]]:
    """One binary per period and mode, exactly one per period, change and activity costs."""
    normal = mc.modes.index(int(HeatingMode.NORMAL)) if int(HeatingMode.NORMAL) in mc.modes else -1
    periods: list[list[int]] = []
    for period in range(mc.n_periods):
        hours = mc.period_hours[period] if period < len(mc.period_hours) else 1.0
        ys = []
        for index in range(len(mc.modes)):
            cost = 0.0 if index == normal else mc.active_penalty * hours
            fixed = mc.fixed_periods.get(period)
            lb = 1.0 if fixed == index else 0.0
            ub = 0.0 if fixed is not None and fixed != index else 1.0
            ys.append(b.var(lb, ub, cost, integer=mc.integral(period)))
        b.row({y: 1.0 for y in ys}, 1.0, 1.0)
        periods.append(ys)
    if mc.change_penalty > 0:
        for period, ys in enumerate(periods):
            change = b.var(0.0, 1.0, mc.change_penalty)
            for index, y in enumerate(ys):
                if period == 0:
                    if mc.initial_mode is None:
                        continue
                    before = 1.0 if index == mc.initial_mode else 0.0
                    # change >= y - before
                    b.row({change: 1.0, y: -1.0}, -before, math.inf)
                else:
                    b.row({change: 1.0, y: -1.0, periods[period - 1][index]: 1.0}, 0.0, math.inf)
    return periods


def _add_mode_heat(
    b: Any,
    block: ThermalBlock,
    t: int,
    q: int,
    w: int,
    room: int,
    slab: int,
    mode_vars: list[list[int]],
) -> None:
    """Rows that make ``q`` the controller's heat of the selected mode (exact clipping).

    ``d = sum_m const[m] y_m + k_room room + k_slab slab``, ``p = max(d, 0)`` and
    ``q = min(p, cap (1 - w / dhw_heat))`` with one binary each (big-M from the state
    bounds). The hot water charge takes its share of the compressor first, like the
    exact simulation.
    """
    mc = block.mode_control
    if mc is None:  # pragma: no cover - guarded by the caller
        return
    inf = math.inf
    period = mc.period_of_slot[t]
    ys = mode_vars[period]
    integral = mc.integral(period)
    consts = mc.const_w[t]
    kr, ks = mc.k_room[t], mc.k_slab[t]
    cap = block.q_cap_w[t]
    state_lo, state_hi = _state_range_w(kr, ks)
    # FORCED: just enough above the capacity to clip to full power in every state.
    consts = [cap - state_lo + 1.0 if c >= FORCED_HEAT_W else c for c in consts]
    d_lo = min(consts) + state_lo
    d_hi = max(consts) + state_hi
    # The state variables stay inside the bounds the big-M constants assume.
    for var, (low, high) in ((room, ROOM_RANGE_C), (slab, SLAB_RANGE_C)):
        b.lb[var] = max(b.lb[var], low)
        b.ub[var] = min(b.ub[var], high)
    d = b.var(d_lo, d_hi)
    row: dict[int, float] = {d: 1.0, room: -kr, slab: -ks}
    for y, const in zip(ys, consts):
        row[y] = row.get(y, 0.0) - const
    b.row(row, 0.0, 0.0)
    if cap <= 0:
        b.row({q: 1.0}, 0.0, 0.0)
        return
    # p = max(d, 0); one binary only where d can take both signs.
    if d_lo >= 0:
        p = d
    elif d_hi <= 0:
        p = b.var(0.0, 0.0)
    else:
        p = b.var(0.0, d_hi)
        u = b.var(0.0, 1.0, 0.0, integer=integral)
        b.row({p: 1.0, d: -1.0}, 0.0, inf)  # p >= d
        b.row({p: 1.0, d: -1.0, u: -d_lo}, -inf, -d_lo)  # p <= d - d_lo (1 - u)
        b.row({p: 1.0, u: -d_hi}, -inf, 0.0)  # p <= d_hi u
    # h = min(p, cap_eff), cap_eff = cap - cap / dhw_heat * w: the controller's heat.
    share = cap / block.dhw_heat_w if block.dhw_heat_w > 0 else 0.0
    h = b.var(0.0, cap)
    b.row({h: 1.0, p: -1.0}, -inf, 0.0)  # h <= p
    b.row({h: 1.0, w: share}, -inf, cap)  # h <= cap_eff
    if max(d_hi, 0.0) > 0.0:
        v = b.var(0.0, 1.0, 0.0, integer=integral)
        big_p = max(d_hi, 0.0)
        b.row({h: 1.0, p: -1.0, v: big_p}, 0.0, inf)  # h >= p - M v
        b.row({h: 1.0, w: share, v: -cap}, 0.0, inf)  # h >= cap_eff - cap (1 - v)
    # The planned heat follows the controller; a deviation is allowed at a high cost,
    # so every mode choice stays feasible and the solver finds solutions quickly.
    cost = mc.deviation_cost_per_wh * block.dt
    above = b.var(0.0, inf, cost)
    below = b.var(0.0, inf, cost)
    b.row({q: 1.0, h: -1.0, above: -1.0, below: 1.0}, 0.0, 0.0)


def trajectory(sol: Any, tv: ThermalVars) -> ThermalTrajectory:
    def vals(ids: Sequence[int]) -> list[float]:
        return [float(sol[i]) for i in ids]

    heating_genes = None
    if tv.mode:
        heating_genes = [max(range(len(ys)), key=lambda i: float(sol[ys[i]])) for ys in tv.mode]
    return ThermalTrajectory(
        space_heat_w=vals(tv.q),
        dhw_heat_w=vals(tv.w),
        rod_w=vals(tv.r),
        electric_wh=vals(tv.hp_wh),
        slab_c=vals(tv.slab[1:]),
        room_c=vals(tv.room[1:]),
        tank_c=vals(tv.tank[1:]),
        heating_genes=heating_genes,
    )


# ----------------------------------------------------------------------
# From heat to modes
# ----------------------------------------------------------------------


def project_modes(
    plan: "HeatPumpRunPlan", traj: ThermalTrajectory, room_weight: float = 1.0
) -> list[int]:
    """Operating modes per gene period that follow the MILP heat schedule.

    Period by period, every allowed (heating mode, hot water option) is simulated
    exactly from the current state; the one whose end state is closest to the MILP
    states wins: deviation of the heat stored in the building and in the tank [Wh],
    plus ``room_weight`` times the room deviation weighted with the room capacity.
    Ties go to the less active modes (NORMAL/HOLD first).
    """
    system = plan.system
    cs = system.building.params.slab_capacity_wh_per_k
    cr = system.building.params.room_capacity_wh_per_k
    ct = system.tank.capacity
    state: ThermalState = plan.initial.copy()
    heating_genes: list[int] = []
    dhw_genes: list[int] = []
    default_heating = plan.heating_index(HeatingMode.NORMAL)
    default_dhw = plan.dhw_index(DhwMode.HOLD)
    n = plan.n_slots
    for period in range(plan.n_periods):
        slots = plan.period_slots(period)
        if not slots:  # pragma: no cover - every period controls at least one slot
            heating_genes.append(default_heating)
            dhw_genes.append(default_dhw)
            continue
        first = slots.start
        last = slots.stop - 1
        best: Optional[tuple[float, int, int, ThermalState]] = None
        for h_index, h_mode in enumerate(plan.heating_modes):
            for d_index, option in enumerate(plan.dhw_options):
                current = state.copy()
                violation = 0.0
                for slot in range(first, last + 1):
                    result = system.step(
                        current,
                        plan.inputs[slot],
                        HeatingMode(h_mode),
                        option.mode,
                        option.rod_target_c,
                    )
                    current = result.state
                    violation += result.comfort_violation_kh + result.dhw_violation_kh
                dev_building = cs * (current.slab_c - traj.slab_c[last]) + cr * (
                    current.room_c - traj.room_c[last]
                )
                dev_room = cr * (current.room_c - traj.room_c[last])
                dev_tank = ct * (current.dhw_c - traj.tank_c[last])
                score = (
                    abs(dev_building)
                    + room_weight * abs(dev_room)
                    + abs(dev_tank)
                    + violation * 1e4
                    + (0.0 if h_index == default_heating else 1e-3)
                    + (0.0 if d_index == default_dhw else 1e-3)
                )
                if best is None or score < best[0]:
                    best = (score, h_index, d_index, current)
        if best is None:  # pragma: no cover - at least one mode is always allowed
            raise ValueError("no operating mode allowed")
        heating_genes.append(best[1])
        dhw_genes.append(best[2])
        state = best[3]
    return plan.expand_zone_genes(heating_genes + dhw_genes)


def project_dhw(
    plan: "HeatPumpRunPlan", traj: ThermalTrajectory, heating_genes: Sequence[int]
) -> list[int]:
    """Hot water option per period for fixed heating modes, following the MILP tank.

    Like ``project_modes`` but only the hot water option is chosen; returns the full
    single-zone gene list (heating + hot water), expanded to the zones.
    """
    system = plan.system
    ct = system.tank.capacity
    state: ThermalState = plan.initial.copy()
    dhw_genes: list[int] = []
    default_dhw = plan.dhw_index(DhwMode.HOLD)
    for period in range(plan.n_periods):
        slots = plan.period_slots(period)
        h_mode = HeatingMode(plan.heating_modes[heating_genes[period]])
        if not slots:  # pragma: no cover - every period controls at least one slot
            dhw_genes.append(default_dhw)
            continue
        last = slots.stop - 1
        best: Optional[tuple[float, int, ThermalState]] = None
        for d_index, option in enumerate(plan.dhw_options):
            current = state.copy()
            violation = 0.0
            for slot in slots:
                result = system.step(
                    current, plan.inputs[slot], h_mode, option.mode, option.rod_target_c
                )
                current = result.state
                violation += result.dhw_violation_kh
            score = (
                abs(ct * (current.dhw_c - traj.tank_c[last]))
                + violation * 1e4
                + (0.0 if d_index == default_dhw else 1e-3)
            )
            if best is None or score < best[0]:
                best = (score, d_index, current)
        if best is None:  # pragma: no cover - HOLD is always allowed
            raise ValueError("no hot water option allowed")
        dhw_genes.append(best[1])
        state = best[2]
    return plan.expand_zone_genes(list(heating_genes) + dhw_genes)
