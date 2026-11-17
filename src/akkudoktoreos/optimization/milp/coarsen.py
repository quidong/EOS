"""Coarse slots for the far horizon of the dispatch model (RL29 fork, evcc ``coarsenHorizon``).

The near hours decide what Home Assistant executes; the far hours only have to get the
energy balance right (how much to keep in the battery, the screed and the tank for
tomorrow). Beyond ``fine_slots`` the model therefore merges ``factor`` slots into one:

- energies (PV, load, appliance profiles, grid charge limits) are summed, prices and
  per-slot conditions are averaged by duration;
- power limits per slot scale with the slot length;
- the heat pump's building and tank models are discretised exactly for the longer slot
  (``A^k`` and ``sum A^i B`` for inputs held over the slot, ventilation folded in);
- the result is expanded back to the original slots: flows split evenly, states
  interpolated linearly inside a coarse slot.

Everything a slot-exact decision depends on (appliance start windows, the shared slots of
the scenarios) stays in the fine part.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Sequence

import numpy as np

from akkudoktoreos.optimization.milp.dispatch import (
    BatteryModel,
    DispatchInput,
    DispatchResult,
    EVModel,
    worst_deviation,
)
from akkudoktoreos.optimization.milp.thermal import ThermalBlock, ThermalTrajectory


def slot_groups(n: int, fine_slots: int, factor: int) -> list[tuple[int, int]]:
    """(start, length) per model slot: single slots up to ``fine_slots``, then ``factor``.

    Args:
        n: Number of original slots.
        fine_slots: Slots kept at full resolution.
        factor: Original slots per coarse slot (the last one may be shorter).

    Returns:
        Groups covering ``range(n)`` in order.
    """
    groups: list[tuple[int, int]] = []
    t = 0
    while t < n:
        length = 1 if t < fine_slots or factor <= 1 else min(factor, n - t)
        groups.append((t, length))
        t += length
    return groups


def _sum(values: Sequence[float], groups: list[tuple[int, int]]) -> list[float]:
    return [float(sum(values[s : s + k])) for s, k in groups]


def _mean(values: Sequence[float], groups: list[tuple[int, int]]) -> list[float]:
    return [float(sum(values[s : s + k])) / k for s, k in groups]


def _group_of(slot: int, groups: list[tuple[int, int]]) -> int:
    """Model slot whose start is at or after ``slot`` (a check at a slot start)."""
    for index, (start, _) in enumerate(groups):
        if start >= slot:
            return index
    return len(groups)


def fine_slots_needed(data: DispatchInput, fine_slots: int) -> int:
    """``fine_slots`` extended so appliance runs and shared scenario slots stay exact."""
    needed = max(fine_slots, data.shared_slots if data.scenarios else 0)
    for run in data.appliances:
        if run.allowed_starts:
            needed = max(needed, max(run.allowed_starts) + len(run.profile_wh))
    for block in data.thermal:
        if block.mode_control is not None:
            return len(data.pv_wh)  # joint mode model: periods are slot exact
    return needed


def _coarsen_battery(bat: BatteryModel, groups: list[tuple[int, int]]) -> BatteryModel:
    """Grid charge caps summed, the full charge check moved to its model slot.

    The scalar power limits keep their per-original-slot value; the model scales them
    with ``DispatchInput.slot_factor``.
    """
    grid_cap: Optional[list[float]] = None
    if bat.grid_charge_max_wh is not None:
        n = groups[-1][0] + groups[-1][1]
        padded = list(bat.grid_charge_max_wh) + [0.0] * max(0, n - len(bat.grid_charge_max_wh))
        grid_cap = _sum(padded, groups)
    return replace(
        bat,
        grid_charge_max_wh=grid_cap,
        target_slot=(
            _group_of(int(bat.target_slot), groups) if bat.target_slot is not None else None
        ),
    )


def _coarsen_ev(ev: EVModel, groups: list[tuple[int, int]]) -> EVModel:
    """Slot caps summed (missing = full power), the target check moved to its model slot."""
    caps: list[Optional[float]] = []
    for start, k in groups:
        part = [
            ev.slot_cap_wh[t] if t < len(ev.slot_cap_wh) else None for t in range(start, start + k)
        ]
        if all(c is None for c in part):
            caps.append(None)
        else:
            caps.append(
                float(
                    sum(ev.max_charge_wh if c is None else min(c, ev.max_charge_wh) for c in part)
                )
            )
    return replace(
        ev,
        slot_cap_wh=caps,
        target_slot=(
            _group_of(int(ev.target_slot), groups) if ev.target_slot is not None else None
        ),
    )


def _building_step(
    a: np.ndarray, bm: np.ndarray, vent: float, k: int
) -> tuple[list[list[float]], list[list[float]]]:
    """Exact k-step building matrices for inputs held constant (ventilation folded in)."""
    a_eff = a.copy()
    a_eff[:, 1] = a[:, 1] - bm[:, 2] * vent
    b_eff = bm.copy()
    # The ventilation loss also adds v * T_out through the gains input.
    b_eff[:, 1] = bm[:, 1] + bm[:, 2] * vent
    power = np.eye(2)
    total = np.zeros_like(bm)
    for _ in range(k):
        total = total + power @ b_eff
        power = power @ a_eff
    return power.tolist(), total.tolist()


def coarsen_block(block: ThermalBlock, groups: list[tuple[int, int]]) -> ThermalBlock:
    """The heat pump model on the model slots (exact discretisation per slot)."""
    a = np.array(block.a, dtype=float)
    bm = np.array(block.b, dtype=float)
    a_slots: list[list[list[float]]] = []
    b_slots: list[list[list[float]]] = []
    decays: list[float] = []
    gains: list[float] = []
    dts: list[float] = []
    vent: list[float] = []
    for start, k in groups:
        v = float(np.mean(block.vent_w_per_k[start : start + k]))
        if k == 1:
            a_slots.append([list(r) for r in block.a])
            b_slots.append([list(r) for r in block.b])
            vent.append(v)
        else:
            ak, bk = _building_step(a, bm, v, k)
            a_slots.append(ak)
            b_slots.append(bk)
            vent.append(0.0)  # folded into the matrices
        decay_k = block.tank_decay**k
        gain_k = block.tank_gain * sum(block.tank_decay**i for i in range(k))
        decays.append(decay_k)
        gains.append(gain_k)
        dts.append(block.dt * k)

    def mean(values: Sequence[float]) -> list[float]:
        return _mean(values, groups)

    def worst(values: Sequence[Optional[float]], pick: str) -> list[Optional[float]]:
        out: list[Optional[float]] = []
        for s, k in groups:
            part = [v for v in values[s : s + k] if v is not None]
            out.append((max(part) if pick == "max" else min(part)) if part else None)
        return out

    points = []
    for s, k in groups:
        rows = block.space_points[s : s + k]
        width = min(len(r) for r in rows)
        points.append(
            [
                (
                    float(np.mean([r[i][0] for r in rows])),
                    float(np.mean([r[i][1] for r in rows])),
                )
                for i in range(width)
            ]
        )
    deadline = block.legionella_deadline
    return_slot = block.return_slot
    return replace(
        block,
        n=len(groups),
        slot_dt=dts,
        a_slots=a_slots,
        b_slots=b_slots,
        tank_decay_slots=decays,
        tank_gain_slots=gains,
        outdoor_c=mean(block.outdoor_c),
        gains_w=mean(block.gains_w),
        vent_w_per_k=vent,
        q_cap_w=mean(block.q_cap_w),
        space_points=points,
        room_min_c=[max(block.room_min_c[s : s + k]) for s, k in groups],
        room_floor_c=worst(block.room_floor_c, "max"),
        room_max_c=[min(block.room_max_c[s : s + k]) for s, k in groups],
        room_ceiling_c=[min(block.room_ceiling_c[s : s + k]) for s, k in groups],
        draw_w=mean(block.draw_w),
        cop_dhw=mean(block.cop_dhw),
        tank_floor_c=worst(block.tank_floor_c, "max"),
        max_electric_w=worst(block.max_electric_w, "min"),
        idle_w=mean(block.idle_w) if block.idle_w else [],
        rod_cap_w=mean(block.rod_cap_w) if block.rod_cap_w else [],
        legionella_deadline=(_group_of(deadline, groups) if deadline is not None else None),
        return_slot=(
            max(0, _group_of(return_slot + 1, groups) - 1) if return_slot is not None else None
        ),
        separate_price_per_wh=(
            mean(block.separate_price_per_wh) if block.separate_price_per_wh is not None else None
        ),
    )


def coarsen_input(data: DispatchInput, groups: list[tuple[int, int]]) -> DispatchInput:
    """The dispatch problem on the model slots."""
    n = groups[-1][0] + groups[-1][1]
    pv = list(data.pv_wh)[:n]
    load = list(data.load_wh)[:n]
    uncertainty = data.uncertainty
    if uncertainty is not None and uncertainty.budget_slots > 0 and data.battery is not None:
        fine_worst = worst_deviation(
            [max(float(v), 0.0) for v in pv],
            [max(float(v), 0.0) for v in load],
            uncertainty,
            data.battery.max_charge_wh,
        )
        # The worst case at the end of every model slot (it only grows within a day).
        uncertainty = replace(uncertainty, worst_wh=[fine_worst[s + k - 1] for s, k in groups])
    return replace(
        data,
        pv_wh=_sum(pv, groups),
        load_wh=_sum(load, groups),
        price_per_wh=_mean(list(data.price_per_wh)[:n], groups),
        feed_in_per_wh=_mean(list(data.feed_in_per_wh)[:n], groups),
        battery=(_coarsen_battery(data.battery, groups) if data.battery is not None else None),
        ev=(_coarsen_ev(data.ev, groups) if data.ev is not None else None),
        scenarios=[
            (w, _sum(list(p)[:n], groups), _sum(list(lo)[:n], groups))
            for w, p, lo in data.scenarios
        ],
        thermal=[coarsen_block(block, groups) for block in data.thermal],
        uncertainty=uncertainty,
        slot_factor=[float(k) for _, k in groups],
        coarsen_after_slots=None,
    )


def _expand(values: Sequence[float], groups: list[tuple[int, int]]) -> list[float]:
    """Split a per-model-slot amount evenly over its original slots."""
    out: list[float] = []
    for value, (_, k) in zip(values, groups):
        out.extend([float(value) / k] * k)
    return out


def _repeat(values: Sequence[float], groups: list[tuple[int, int]]) -> list[float]:
    out: list[float] = []
    for value, (_, k) in zip(values, groups):
        out.extend([float(value)] * k)
    return out


def _states(start: float, ends: Sequence[float], groups: list[tuple[int, int]]) -> list[float]:
    """States at the end of every original slot, linear inside a model slot."""
    out: list[float] = []
    previous = start
    for end, (_, k) in zip(ends, groups):
        for i in range(1, k + 1):
            out.append(previous + (float(end) - previous) * i / k)
        previous = float(end)
    return out


def expand_result(
    result: DispatchResult, data: DispatchInput, groups: list[tuple[int, int]]
) -> DispatchResult:
    """The model slot result on the original slots."""
    if not result.success:
        return result
    soc = result.soc_wh
    expanded_soc = [soc[0]] + _states(soc[0], soc[1:], groups) if soc else []
    thermal: list[ThermalTrajectory] = []
    for traj, block in zip(result.thermal, data.thermal):
        thermal.append(
            ThermalTrajectory(
                space_heat_w=_repeat(traj.space_heat_w, groups),
                dhw_heat_w=_repeat(traj.dhw_heat_w, groups),
                rod_w=_repeat(traj.rod_w, groups),
                electric_wh=_expand(traj.electric_wh, groups),
                slab_c=_states(block.slab0, traj.slab_c, groups),
                room_c=_states(block.room0, traj.room_c, groups),
                tank_c=_states(block.tank0, traj.tank_c, groups),
                extra=dict(traj.extra),
                heating_genes=traj.heating_genes,
            )
        )
    n = groups[-1][0] + groups[-1][1]
    appliance_wh = list(result.appliance_wh)
    fine = [0.0] * n
    # Appliance runs lie in the fine part, where model slots are original slots.
    for (start, k), value in zip(groups, appliance_wh):
        if k == 1:
            fine[start] = float(value)
    return replace(
        result,
        grid_import_wh=_expand(result.grid_import_wh, groups),
        grid_export_wh=_expand(result.grid_export_wh, groups),
        charge_pv_wh=_expand(result.charge_pv_wh, groups),
        charge_grid_wh=_expand(result.charge_grid_wh, groups),
        discharge_wh=_expand(result.discharge_wh, groups),
        soc_wh=expanded_soc,
        appliance_wh=fine,
        thermal=thermal,
        import_excess_wh=(
            _expand(result.import_excess_wh, groups) if result.import_excess_wh else []
        ),
        ev_charge_wh=_expand(result.ev_charge_wh, groups) if result.ev_charge_wh else [],
        ev_soc_wh=(
            [result.ev_soc_wh[0]] + _states(result.ev_soc_wh[0], result.ev_soc_wh[1:], groups)
            if result.ev_soc_wh
            else []
        ),
    )
