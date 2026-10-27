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


@dataclass
class ThermalVars:
    hp_wh: list[int]
    q: list[int]
    w: list[int]
    r: list[int]
    slab: list[int]
    room: list[int]
    tank: list[int]


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


# ----------------------------------------------------------------------
# Building the block from a run plan
# ----------------------------------------------------------------------


def _breakpoints(f: Any, cap: float, k: int = SPACE_BREAKPOINTS) -> list[tuple[float, float]]:
    """(x, f(x)) on k+1 equidistant points of [0, cap]."""
    if cap <= 0:
        return [(0.0, 0.0)]
    return [(cap * i / k, float(f(cap * i / k))) for i in range(k + 1)]


def thermal_block(plan: "HeatPumpRunPlan") -> Optional[ThermalBlock]:
    """Linear model of one heat pump; None when the plan is out of the model's scope."""
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
        room_floor.append(None if HeatingMode.REDUCED in allowed else min(targets))
        room_ceiling.append(max(targets) + REPAIR_MARGIN_K)
        tank_floor.append(None if slot.dhw_off else max(system.dhw.min_c, switch_on - 0.5))
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
        r = b.var(0.0, block.rod_power_w)
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
    return ThermalVars(hp_wh, q_vars, w_vars, r_vars, slab, room, tank)


def trajectory(sol: Any, tv: ThermalVars) -> ThermalTrajectory:
    def vals(ids: Sequence[int]) -> list[float]:
        return [float(sol[i]) for i in ids]

    return ThermalTrajectory(
        space_heat_w=vals(tv.q),
        dhw_heat_w=vals(tv.w),
        rod_w=vals(tv.r),
        electric_wh=vals(tv.hp_wh),
        slab_c=vals(tv.slab[1:]),
        room_c=vals(tv.room[1:]),
        tank_c=vals(tv.tank[1:]),
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
    spg = plan.slots_per_gene
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
        first = period * spg
        last = min(n, first + spg) - 1
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
    return heating_genes + dhw_genes
