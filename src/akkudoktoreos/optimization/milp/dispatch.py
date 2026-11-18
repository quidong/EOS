"""Mixed-integer linear dispatch of battery, grid and appliances (RL29 fork).

A pure model, independent of EOS objects: forecasts and device figures go in, a
cost-optimal schedule comes out. Solved with HiGHS through ``scipy.optimize.milp``
(SciPy is already an EOS dependency, so no solver has to be installed).

Per slot ``t`` (energies in Wh, prices per Wh)::

    pv[t] + imp[t] + dis[t] = load[t] + appliance[t] + chg_pv[t] + chg_grid[t] + exp[t]
    soc[t+1] = soc[t] + eta_c * (chg_pv[t] + chg_grid[t]) - dis[t] / eta_d
    min_soc <= soc <= max_soc,  chg_pv + chg_grid <= max_charge,  dis <= max_discharge
    chg_grid[t] <= grid_charge_max[t]            (inverter AC limit, full charge window)
    soc[k] + shortfall >= full_charge_target     (due full charge, shortfall penalised)
    chg_pv[t] <= pv[t] - load[t],  exp[t] <= pv[t]  (surplus charging; only PV is fed in)
    dis[t] <= load[t] + appliance[t]              (the battery serves the house only)
    appliance[t] = sum_k x[g,k] * profile_g[t - start_k],  sum_k x[g,k] = 1,  x binary
    heat pumps (optional, ``thermal.py``): their electric energy joins the load

    minimise  sum_t price[t]*imp[t] - feed_in[t]*exp[t] + lcos*dis[t]
              - terminal_value(soc[T])

``terminal_value`` is the piecewise linear credit for the energy left in the battery
(concave segments); it keeps the battery from being emptied at the horizon end.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

import numpy as np

from akkudoktoreos.optimization.milp.thermal import (
    ThermalBlock,
    ThermalTrajectory,
    add_thermal,
    legionella_candidate_slots,
    rod_marginal_price_per_wh,
    trajectory,
)


@dataclass
class BatteryModel:
    """Battery figures in DC Wh per slot."""

    capacity_wh: float
    soc_wh: float
    min_soc_wh: float
    max_soc_wh: float
    max_charge_wh: float
    """Per slot (power x slot duration), measured on the AC side."""
    max_discharge_wh: float
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    """Including the DC/AC conversion of the inverter."""
    lcos_per_wh: float = 0.0
    """Levelized cost of storage per delivered AC Wh."""
    allow_grid_charge: bool = True
    grid_charge_max_wh: Optional[list[float]] = None
    """Per-slot limit of grid charging [AC Wh] (inverter AC charge power, end of a
    full charge exception); None = only ``max_charge_wh``. Slots beyond the list are 0."""
    target_soc_wh: Optional[float] = None
    """Fork: state of charge a due full charge has to reach [Wh]; None = none due."""
    target_slot: Optional[int] = None
    """Slot at whose start ``target_soc_wh`` is checked (capped to the horizon end)."""
    target_shortfall_per_wh: float = 0.0
    """Penalty per Wh the full charge stays below its target."""


@dataclass
class SwitchingModel:
    """Battery mode changes in the dispatch model (RL29 fork, evcc ``continuity``).

    The modes are the ones Home Assistant switches at the inverter and the GENETIC
    states encode (``battery_states``): idle (PV surplus charges), discharge allowed,
    grid charge. A slot is in a mode when its flow exceeds ``threshold_wh``; binaries
    make that exact, so the changes counted here are the changes of the plan.

    Two mechanisms, usable together:

    - ``change_cost``/``hold_cost`` price a change and a mode shorter than
      ``hold_slots`` in the objective, like the GENETIC fitness does
      (``battery_change_penalty_amt``, ``battery_hold_penalty_amt``): a change has to
      earn at least its cost.
    - ``tie_break``: a second solve keeps the objective found (within
      ``tie_break_tolerance``) and minimises the number of changes, so equally good
      plans do not switch back and forth.
    """

    change_cost: float = 0.0
    hold_slots: int = 0
    hold_cost: float = 0.0
    threshold_wh: float = 1.0
    tie_break: bool = True
    tie_break_tolerance: float = 1e-4
    tie_break_time_limit_s: float = 10.0
    initial_state: Optional[int] = None
    """Mode in effect before the first slot (0 idle, 1 discharge, 2 grid charge);
    None = the first slot is free."""


@dataclass
class EVModel:
    """Electric vehicle in the dispatch model (RL29 fork, F1; evcc loadpoint battery).

    The charger draws ``ev_wh[t]`` (raw, before the charging losses) per slot: 0 or
    between the smallest charge rate and full power (semi-continuous, one binary per
    slot). It is a consumer of the house: PV, grid and the house battery cover it as in
    the GENETIC simulation. The target is soft: every Wh missing at ``target_slot``
    costs ``shortfall_per_wh`` (``ev_soc_miss`` per percent). Charging starts cost
    ``start_cost`` each, so the plan charges in few blocks (evcc continuity).
    """

    capacity_wh: float
    soc_wh: float
    max_soc_wh: float
    max_charge_wh: float
    """Raw energy at full power per slot."""
    min_rate: float = 0.0
    """Smallest positive charge rate (share of full power); 0 = continuous."""
    charge_efficiency: float = 0.9
    target_wh: Optional[float] = None
    target_slot: Optional[int] = None
    """Slot at whose start the target is checked; None = end of the horizon."""
    shortfall_per_wh: float = 0.0
    start_cost: float = 0.0
    slot_cap_wh: list[Optional[float]] = field(default_factory=list)
    """Raw energy cap per slot (§14a dimming, no charging at the end); missing = none."""
    charging_now: bool = False
    """The charger runs at the start: continuing costs no start."""


@dataclass
class UncertaintyBudget:
    """Forecast errors the plan hedges against (RL29 fork, evcc ``uncertainty``).

    Every slot may deliver ``pv_dev`` less PV and need ``load_dev`` more load than
    forecast, but at most ``budget_slots`` slots within a day do so (budget of
    Bertsimas and Sim). The worst case up to each slot is known before the solve; the
    model prices the recourse: the reserve above the minimum SoC that covers the
    shortfall (at what the plan values stored energy) and the import for what it cannot
    cover (at the price of the slot). No binaries, four variables per slot.
    """

    pv_dev: float = 0.3
    load_dev: float = 0.2
    budget_slots: float = 0.0
    day_slots: int = 96
    """Slots the worst case looks back (one day)."""
    worst_wh: Optional[list[float]] = None
    """Precomputed worst case per model slot (coarse slots); None = from the forecast."""


HEDGE_TIE_DISCOUNT = 1e-3
"""Protection is priced slightly below its worth, so a tie keeps the forecast's plan."""


def worst_deviation(
    pv: Sequence[float],
    load: Sequence[float],
    hedge: UncertaintyBudget,
    max_charge_wh: float,
) -> list[float]:
    """Budgeted worst case deviation up to each slot [Wh] (the day before it).

    A slot's deviation is ``pv_dev * pv + load_dev * load`` beyond the PV surplus the
    battery cannot take anyway (that surplus is exported or curtailed whatever the plan).
    """
    import heapq

    n = len(pv)
    dev = []
    for t in range(n):
        deviation = hedge.pv_dev * pv[t] + hedge.load_dev * load[t]
        spill = max(pv[t] - load[t] - max_charge_wh, 0.0)
        dev.append(max(deviation - spill, 0.0))
    budget = min(max(hedge.budget_slots, 0.0), float(n))
    size = math.ceil(budget)
    part = budget - math.floor(budget)
    worst = [0.0] * n
    if size == 0:
        return worst
    for t in range(n):
        first = max(0, t - max(hedge.day_slots, 1) + 1)
        largest = heapq.nlargest(size, dev[first : t + 1])
        total = sum(largest)
        if part and len(largest) == size:
            total -= (1.0 - part) * largest[-1]
        worst[t] = total
    return worst


@dataclass
class ApplianceRun:
    """One run of a schedulable appliance: allowed start slots and its energy profile."""

    allowed_starts: list[int]
    profile_wh: list[float]


@dataclass
class DispatchInput:
    pv_wh: Sequence[float]
    load_wh: Sequence[float]
    """Fixed consumption per slot (base load, heat pump, ...)."""
    price_per_wh: Sequence[float]
    feed_in_per_wh: Sequence[float]
    battery: Optional[BatteryModel] = None
    appliances: list[ApplianceRun] = field(default_factory=list)
    terminal_segments: list[tuple[float, float]] = field(default_factory=list)
    """(width Wh of AC energy, value per Wh) of the terminal credit, in order."""
    terminal_dc_to_ac: float = 1.0
    """Factor from DC battery content to the AC energy the terminal curve values
    (battery discharge efficiency times inverter DC/AC efficiency, as the GENETIC
    simulation's usable energy content)."""
    terminal_offset_wh: float = 0.0
    """Fork: DC content the terminal curve does not value (the minimum SoC): the curve
    is over the usable energy ``terminal_dc_to_ac * (soc - offset)``."""
    max_grid_import_wh: Optional[float] = None
    max_grid_export_wh: Optional[float] = None
    scenarios: list[tuple[float, Sequence[float], Sequence[float]]] = field(default_factory=list)
    """(weight, pv_wh, load_wh) per scenario; empty = the single pv_wh/load_wh above.
    Put the central scenario first: it is the one reported."""
    shared_slots: int = 4
    """Slots whose battery decisions are the same in every scenario."""
    time_limit_s: float = 10.0
    mip_rel_gap: float = 1e-4
    thermal: list[ThermalBlock] = field(default_factory=list)
    """Heat pumps planned inside the model (``thermal.py``); their electric energy is part
    of the house balance (or priced separately with their own meter)."""
    switching: Optional[SwitchingModel] = None
    """Fork: battery mode changes (P1); None = changes are free and not modelled."""
    uncertainty: Optional[UncertaintyBudget] = None
    """Fork: hedge the reported scenario against forecast errors (P2); None = off."""
    ev: Optional[EVModel] = None
    """Fork: electric vehicle planned in the model (F1); None = no EV."""
    import_excess_cost_per_wh: Optional[float] = None
    """Fork: import above ``max_grid_import_wh`` costs this per Wh on top of the price
    (soft limit, as in the GENETIC simulation); None = hard limit."""
    slot_factor: list[float] = field(default_factory=list)
    """Fork (coarse slots): original slots per model slot; power limits per slot scale
    with it. Empty = 1 everywhere."""
    coarsen_after_slots: Optional[int] = None
    """Fork (P4): merge ``coarse_factor`` slots into one beyond this slot (``coarsen.py``);
    None = full resolution."""
    coarse_factor: int = 4
    lock_charge_discharge: str = "negative_prices"
    """Fork: binaries that forbid charging and discharging in the same slot:
    ``never``, ``negative_prices`` (only where cycling could earn money) or ``always``."""


@dataclass
class DispatchResult:
    status: str
    success: bool
    objective: Optional[float]
    grid_import_wh: list[float]
    grid_export_wh: list[float]
    charge_pv_wh: list[float]
    charge_grid_wh: list[float]
    discharge_wh: list[float]
    soc_wh: list[float]
    """State of charge at the start of every slot plus the end state (T+1 values)."""
    appliance_starts: list[int]
    """Index into ``allowed_starts`` of each appliance run."""
    appliance_wh: list[float]
    solve_seconds: float
    thermal: list[ThermalTrajectory] = field(default_factory=list)
    optimal: bool = False
    """The solver proved the optimum (within ``mip_rel_gap``); False when it stopped at
    the time limit with a feasible solution."""
    mip_gap: Optional[float] = None
    """Relative gap between the solution and the solver's bound when it stopped (0 for a
    proven optimum, None for a pure LP or when the solver does not report it)."""
    mode_changes: Optional[int] = None
    """Fork: battery mode changes of the plan (with ``switching``)."""
    tie_break: Optional[str] = None
    """Fork: what the switching tie break did (``improved 5 to 3``, ``kept``, ...)."""
    import_excess_wh: list[float] = field(default_factory=list)
    """Fork: import above the soft import limit per slot (empty without a limit)."""
    ev_charge_wh: list[float] = field(default_factory=list)
    """Fork: raw EV charger energy per slot (empty without an EV)."""
    ev_soc_wh: list[float] = field(default_factory=list)
    """Fork: EV energy at the start of every slot plus the end state."""

    def grid_cost(self, price: Sequence[float], feed_in: Sequence[float]) -> float:
        return float(
            sum(p * i for p, i in zip(price, self.grid_import_wh))
            - sum(f * e for f, e in zip(feed_in, self.grid_export_wh))
        )


class _Builder:
    """Collects variables and sparse constraint rows for ``scipy.optimize.milp``."""

    def __init__(self) -> None:
        self.lb: list[float] = []
        self.ub: list[float] = []
        self.cost: list[float] = []
        self.integer: list[int] = []
        self.rows: list[dict[int, float]] = []
        self.row_lb: list[float] = []
        self.row_ub: list[float] = []

    def var(
        self, lb: float = 0.0, ub: float = math.inf, cost: float = 0.0, integer: bool = False
    ) -> int:
        self.lb.append(lb)
        self.ub.append(ub)
        self.cost.append(cost)
        self.integer.append(1 if integer else 0)
        return len(self.lb) - 1

    def row(self, coeffs: dict[int, float], lb: float, ub: float) -> None:
        self.rows.append({k: v for k, v in coeffs.items() if v != 0.0})
        self.row_lb.append(lb)
        self.row_ub.append(ub)

    def solve(
        self,
        time_limit: float,
        gap: float,
        cost: Optional[list[float]] = None,
        extra_rows: Optional[list[tuple[dict[int, float], float, float]]] = None,
    ) -> "_SolveOutcome":
        """Solve the model.

        ``cost``/``extra_rows`` replace the objective and add rows for this solve only
        (lexicographic stages).
        """
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import csr_array

        n = len(self.lb)
        rows = list(self.rows)
        row_lb = list(self.row_lb)
        row_ub = list(self.row_ub)
        for coeffs, lo, hi in extra_rows or []:
            rows.append({k: v for k, v in coeffs.items() if v != 0.0})
            row_lb.append(lo)
            row_ub.append(hi)
        data, indices, indptr = [], [], [0]
        for row in rows:
            for k, v in sorted(row.items()):
                indices.append(k)
                data.append(v)
            indptr.append(len(indices))
        constraints = []
        if rows:
            matrix = csr_array((data, indices, indptr), shape=(len(rows), n))
            constraints.append(LinearConstraint(matrix, np.array(row_lb), np.array(row_ub)))
        result = milp(
            c=np.array(self.cost if cost is None else cost),
            integrality=np.array(self.integer),
            bounds=Bounds(np.array(self.lb), np.array(self.ub)),
            constraints=constraints,
            options={"time_limit": float(time_limit), "mip_rel_gap": float(gap), "disp": False},
        )
        return _outcome(result)


@dataclass
class _SolveOutcome:
    status: str
    ok: bool
    """A solution is available (proven optimal or stopped at a limit)."""
    solution: Optional[np.ndarray]
    objective: Optional[float]
    optimal: bool
    mip_gap: Optional[float]


def _outcome(result: Any) -> _SolveOutcome:
    """Translate a ``scipy.optimize.milp`` result (status 0 optimal, 1 limit)."""
    ok = result.x is not None and result.status in (0, 1)
    optimal = ok and result.status == 0
    raw_gap = getattr(result, "mip_gap", None)
    mip_gap: Optional[float]
    if optimal:
        # HiGHS reports the gap it stopped at (<= mip_rel_gap); proven is proven.
        mip_gap = float(raw_gap) if raw_gap is not None and math.isfinite(raw_gap) else 0.0
    elif raw_gap is not None and math.isfinite(float(raw_gap)):
        mip_gap = float(raw_gap)
    else:
        mip_gap = None
    return _SolveOutcome(
        status=str(result.message),
        ok=ok,
        solution=result.x if ok else None,
        objective=(float(result.fun) if ok and result.fun is not None else None),
        optimal=optimal,
        mip_gap=mip_gap,
    )


def describe_optimality(optimal: bool, mip_gap: Optional[float]) -> str:
    """Short log text: ``optimal`` or ``NOT optimal (gap 3.2 %)``."""
    if optimal:
        return "optimal"
    if mip_gap is None:
        return "NOT optimal (gap unknown)"
    return f"NOT optimal (gap {mip_gap * 100:.1f} %)"


def _factor(data: DispatchInput, t: int) -> float:
    """Original slots in model slot ``t`` (coarse slots, P4)."""
    return float(data.slot_factor[t]) if t < len(data.slot_factor) else 1.0


@dataclass
class _ScenarioVars:
    imp: list[int]
    exp: list[int]
    chg_pv: list[Optional[int]]
    chg_grid: list[Optional[int]]
    dis: list[Optional[int]]
    soc: list[int]
    excess: list[Optional[int]] = field(default_factory=list)


def _add_scenario(
    b: "_Builder",
    data: DispatchInput,
    pv: list[float],
    load: list[float],
    price: list[float],
    feed: list[float],
    weight: float,
    app_load: list[dict[int, float]],
) -> _ScenarioVars:
    """Variables and constraints of one scenario; costs scaled by ``weight``."""
    n = len(pv)
    inf = math.inf
    factor = [_factor(data, t) for t in range(n)]
    imp_limit = data.max_grid_import_wh if data.max_grid_import_wh is not None else inf
    exp_limit = data.max_grid_export_wh if data.max_grid_export_wh is not None else inf
    soft_import = data.max_grid_import_wh is not None and data.import_excess_cost_per_wh is not None
    # Soft import limit (fork): excess is allowed at a penalty on top of the price, so a
    # fixed load above the limit no longer makes the model infeasible.
    imp = [
        b.var(0.0, inf if soft_import else imp_limit * factor[t], weight * price[t])
        for t in range(n)
    ]
    excess: list[Optional[int]] = [None] * n
    if soft_import:
        for t in range(n):
            over = b.var(0.0, inf, weight * float(data.import_excess_cost_per_wh or 0.0))
            b.row({imp[t]: 1.0, over: -1.0}, -inf, imp_limit * factor[t])
            excess[t] = over
    # Negative feed-in tariff: exporting costs money; the model may curtail by not exporting
    # (PV that is neither used nor exported is curtailed).
    exp = [b.var(0.0, min(pv[t], exp_limit * factor[t]), -weight * feed[t]) for t in range(n)]

    bat = data.battery
    chg_pv: list[Optional[int]] = [None] * n
    chg_grid: list[Optional[int]] = [None] * n
    dis: list[Optional[int]] = [None] * n
    soc: list[int] = []
    if bat is not None and bat.capacity_wh > 0:
        start_soc = min(max(bat.soc_wh, 0.0), bat.capacity_wh)
        low = min(bat.min_soc_wh, start_soc)
        soc.append(b.var(start_soc, start_soc))
        for t in range(n):
            max_c = bat.max_charge_wh * factor[t]
            max_d = bat.max_discharge_wh * factor[t]
            # PV charging only from the surplus over the fixed load, as in the simulation
            # (idle and self-consumption states charge from surplus only).
            c_pv = b.var(0.0, min(max(pv[t] - load[t], 0.0), max_c))
            grid_cap = max_c if bat.allow_grid_charge else 0.0
            if bat.grid_charge_max_wh is not None:
                slot_cap = bat.grid_charge_max_wh[t] if t < len(bat.grid_charge_max_wh) else 0.0
                grid_cap = min(grid_cap, max(slot_cap, 0.0))
            c_grid = b.var(0.0, grid_cap, 1e-9)
            d = b.var(0.0, max_d, weight * bat.lcos_per_wh)
            chg_pv[t], chg_grid[t], dis[t] = c_pv, c_grid, d
            lock = data.lock_charge_discharge
            if lock == "always" or (lock == "negative_prices" and price[t] < 0.0):
                # Fork (P5): the inverter cannot charge and discharge at once; without the
                # lock, cycling would "sell" losses while the import price is negative.
                z = b.var(0.0, 1.0, 0.0, integer=True)
                b.row({c_pv: 1.0, c_grid: 1.0, z: max_c}, -inf, max_c)
                b.row({d: 1.0, z: -max_d}, -inf, 0.0)
            soc.append(b.var(low, max(bat.max_soc_wh, start_soc)))
            b.row({c_pv: 1.0, c_grid: 1.0}, -inf, max_c)
            # Grid charging is paid for: it needs as much import in the same slot.
            b.row({c_grid: 1.0, imp[t]: -1.0}, -inf, 0.0)
            b.row(
                {
                    soc[t + 1]: 1.0,
                    soc[t]: -1.0,
                    c_pv: -bat.charge_efficiency,
                    c_grid: -bat.charge_efficiency,
                    d: 1.0 / max(bat.discharge_efficiency, 1e-9),
                },
                0.0,
                0.0,
            )
        # Fork: a due full charge (soft): soc[k] + shortfall >= target, shortfall penalised.
        if bat.target_soc_wh is not None and bat.target_slot is not None:
            k = min(max(int(bat.target_slot), 0), n)
            target_wh = min(bat.target_soc_wh, bat.capacity_wh)
            shortfall = b.var(0.0, max(target_wh, 0.0), weight * bat.target_shortfall_per_wh)
            b.row({soc[k]: 1.0, shortfall: 1.0}, target_wh, inf)
        # Terminal credit: sum of segment energies equals the AC value of the end SoC.
        if data.terminal_segments:
            seg_vars = [
                b.var(0.0, max(width, 0.0), -weight * value)
                for width, value in data.terminal_segments
            ]
            terminal: dict[int, float] = {v: 1.0 for v in seg_vars}
            terminal[soc[n]] = -data.terminal_dc_to_ac
            # Segments may stay below the content (credit flat beyond the last breakpoint).
            # Only the usable content above the minimum SoC is valued (fork, as the
            # simulation's current_energy_content()).
            # Never above the lowest reachable SoC (a start below the minimum stays feasible).
            offset = min(max(data.terminal_offset_wh, 0.0), low)
            b.row(terminal, -inf, -data.terminal_dc_to_ac * offset)

    # Energy balance and battery-serves-house-only.
    for t in range(n):
        balance: dict[int, float] = {imp[t]: 1.0, exp[t]: -1.0}
        supply: dict[int, float] = {imp[t]: 1.0}
        for x_var, energy in app_load[t].items():
            balance[x_var] = balance.get(x_var, 0.0) - energy
            supply[x_var] = -energy
        d_var, cp_var, cg_var = dis[t], chg_pv[t], chg_grid[t]
        if d_var is not None and cp_var is not None and cg_var is not None:
            balance[d_var] = 1.0
            balance[cp_var] = -1.0
            balance[cg_var] = -1.0
            supply[d_var] = 1.0
            supply[cg_var] = -1.0
            supply[cp_var] = -1.0
            serve: dict[int, float] = {d_var: 1.0}
            for x_var, energy in app_load[t].items():
                serve[x_var] = -energy
            b.row(serve, -inf, load[t])
        # pv + imp + dis - app - chg - exp >= load  (>=: surplus PV may be curtailed)
        b.row(balance, load[t] - pv[t], inf)
        # Grid and battery do not supply more than is consumed or stored (no import that
        # is curtailed); keeps the solution physical when prices are negative.
        b.row(supply, -inf, load[t])
    return _ScenarioVars(imp, exp, chg_pv, chg_grid, dis, soc, excess)


@dataclass
class _SwitchVars:
    grid: list[int]
    discharge: list[int]
    changes: list[int]
    short: list[int]
    initial_state: Optional[int] = None

    def count(self, solution: np.ndarray) -> int:
        """Mode changes of a solution, from the mode binaries."""
        modes = [
            2 if solution[g] > 0.5 else 1 if solution[d] > 0.5 else 0
            for g, d in zip(self.grid, self.discharge)
        ]
        if self.initial_state is not None:
            modes = [self.initial_state] + modes
        return sum(1 for a, b in zip(modes, modes[1:]) if a != b)


def _add_switching(
    b: "_Builder", data: DispatchInput, plan: _ScenarioVars
) -> Optional[_SwitchVars]:
    """Mode binaries, change and short-hold variables of the reported scenario (P1)."""
    sw, bat = data.switching, data.battery
    if sw is None or bat is None or bat.capacity_wh <= 0:
        return None
    n = len(plan.imp)
    inf = math.inf
    # Slightly above the threshold: battery_states counts a flow strictly above it.
    thr = max(float(sw.threshold_wh), 1e-6) * 1.01 + 1e-3
    grid: list[int] = []
    discharge: list[int] = []
    for t in range(n):
        c_grid, d = plan.chg_grid[t], plan.dis[t]
        if c_grid is None or d is None:  # pragma: no cover - set with a battery
            return None
        g_t = b.var(0.0, 1.0, 0.0, integer=True)
        d_t = b.var(0.0, 1.0, 0.0, integer=True)
        f_t = _factor(data, t)
        # Exact modes: flow above the threshold <=> binary set (as battery_states reads it).
        b.row({c_grid: 1.0, g_t: -bat.max_charge_wh * f_t}, -inf, 0.0)
        b.row({c_grid: 1.0, g_t: -thr}, 0.0, inf)
        b.row({d: 1.0, d_t: -bat.max_discharge_wh * f_t}, -inf, 0.0)
        b.row({d: 1.0, d_t: -thr}, 0.0, inf)
        b.row({g_t: 1.0, d_t: 1.0}, -inf, 1.0)
        grid.append(g_t)
        discharge.append(d_t)
    initial = sw.initial_state
    changes: list[int] = []
    for t in range(n):
        w = b.var(0.0, 1.0, float(sw.change_cost))
        changes.append(w)
        if t == 0:
            if initial is None:
                b.row({w: 1.0}, 0.0, 0.0)
                continue
            g_prev = 1.0 if initial == 2 else 0.0
            d_prev = 1.0 if initial == 1 else 0.0
            b.row({w: 1.0, grid[0]: -1.0}, -g_prev, inf)
            b.row({w: 1.0, grid[0]: 1.0}, g_prev, inf)
            b.row({w: 1.0, discharge[0]: -1.0}, -d_prev, inf)
            b.row({w: 1.0, discharge[0]: 1.0}, d_prev, inf)
            continue
        for x in (grid, discharge):
            b.row({w: 1.0, x[t]: -1.0, x[t - 1]: 1.0}, 0.0, inf)
            b.row({w: 1.0, x[t]: 1.0, x[t - 1]: -1.0}, 0.0, inf)
    short: list[int] = []
    if sw.hold_slots > 1 and sw.hold_cost > 0:
        # Two changes closer than the hold time: the mode between them is too short.
        for t in range(n):
            for k in range(1, sw.hold_slots):
                if t + k >= n:
                    break
                v = b.var(0.0, 1.0, float(sw.hold_cost))
                b.row({v: 1.0, changes[t]: -1.0, changes[t + k]: -1.0}, -1.0, inf)
                short.append(v)
    return _SwitchVars(grid, discharge, changes, short, initial)


def _switch_tie_break(
    b: "_Builder", data: DispatchInput, sv: _SwitchVars, outcome: "_SolveOutcome"
) -> tuple["_SolveOutcome", str]:
    """Fewest mode changes at the objective already found (evcc ``continuity``)."""
    sw = data.switching
    if sw is None or outcome.solution is None or outcome.objective is None:
        return outcome, "no solution"
    before = sv.count(outcome.solution)
    if before == 0:
        return outcome, "no changes"
    bound = outcome.objective + abs(outcome.objective) * 1e-9 + float(sw.tie_break_tolerance)
    cost = [0.0] * len(b.cost)
    for i in sv.changes:
        cost[i] = 1.0
    for i in sv.short:
        cost[i] = 0.5
    objective_row = {i: c for i, c in enumerate(b.cost) if c != 0.0}
    second = b.solve(
        min(float(sw.tie_break_time_limit_s), float(data.time_limit_s)),
        data.mip_rel_gap,
        cost=cost,
        extra_rows=[(objective_row, -math.inf, bound)],
    )
    if not second.ok or second.solution is None:
        return outcome, f"kept ({second.status})"
    after = sv.count(second.solution)
    if after >= before:
        return outcome, "kept"
    objective = float(np.dot(np.array(b.cost), second.solution))
    improved = _SolveOutcome(
        status=outcome.status,
        ok=True,
        solution=second.solution,
        objective=objective,
        optimal=outcome.optimal,
        mip_gap=outcome.mip_gap,
    )
    return improved, f"improved {before} to {after}"


def _add_hedge(
    b: "_Builder",
    data: DispatchInput,
    plan: _ScenarioVars,
    pv: list[float],
    load: list[float],
    price: list[float],
    feed: list[float],
    weight: float,
) -> None:
    """Budgeted recourse cost of forecast errors (evcc ``_add_uncertainty_constraints``)."""
    hedge, bat = data.uncertainty, data.battery
    if hedge is None or hedge.budget_slots <= 0 or bat is None or bat.capacity_wh <= 0:
        return
    if hedge.pv_dev <= 0 and hedge.load_dev <= 0:
        return
    n = len(plan.imp)
    inf = math.inf
    worst = (
        list(hedge.worst_wh)
        if hedge.worst_wh is not None
        else worst_deviation(pv, load, hedge, bat.max_charge_wh)
    )
    if not any(worst):
        return
    eta_c = max(bat.charge_efficiency, 1e-9)
    eta_d = max(bat.discharge_efficiency, 1e-9)
    stored_value = max((value for _, value in data.terminal_segments), default=0.0)
    reserve_price = max(max(feed, default=0.0), stored_value / eta_d, 0.0) * (
        1.0 - HEDGE_TIE_DISCOUNT
    )
    running_prev: Optional[int] = None
    for t in range(n):
        p_t = max(price[t], 0.0) * (1.0 - HEDGE_TIE_DISCOUNT)
        u_imp = b.var(0.0, inf, weight * p_t)
        u_chg = b.var(0.0, inf, weight * p_t / (eta_c * eta_d))
        u_res = b.var(0.0, inf, weight * reserve_price)
        running = b.var(0.0, worst[t])
        c_pv, c_grid, d = plan.chg_pv[t], plan.chg_grid[t], plan.dis[t]
        if c_pv is None or c_grid is None or d is None:  # pragma: no cover - battery set
            return
        # running <= running_prev + u_imp + u_chg
        row: dict[int, float] = {running: 1.0, u_imp: -1.0, u_chg: -1.0}
        if running_prev is not None:
            row[running_prev] = -1.0
        b.row(row, -inf, 0.0)
        # Extra import replaces a planned discharge or covers what arrives in this slot.
        arriving = max(worst[t] - (worst[t - 1] if t > 0 else 0.0), 0.0)
        b.row({u_imp: 1.0, d: -1.0}, -inf, arriving)
        # Refill from the grid with the charge power the plan leaves unused.
        if bat.allow_grid_charge:
            b.row(
                {u_chg: 1.0, c_grid: eta_c * eta_d, c_pv: eta_c * eta_d},
                -inf,
                eta_c * eta_d * bat.max_charge_wh * _factor(data, t),
            )
        else:
            b.row({u_chg: 1.0}, -inf, 0.0)
        # The reserve above the minimum SoC covers the rest: running >= worst - reserve.
        b.row(
            {running: 1.0, plan.soc[t + 1]: eta_d},
            worst[t] + eta_d * bat.min_soc_wh,
            inf,
        )
        # Reserve consumed by the worst case, what this slot adds to it.
        res_row: dict[int, float] = {u_res: 1.0, running: 1.0}
        lower = worst[t]
        if running_prev is not None:
            res_row[running_prev] = -1.0
            lower -= worst[t - 1]
        b.row(res_row, lower, inf)
        running_prev = running


@dataclass
class _EVVars:
    charge: list[int]
    soc: list[int]


def _add_ev(
    b: "_Builder", data: DispatchInput, n: int, app_load: list[dict[int, float]]
) -> Optional[_EVVars]:
    """EV charger and EV battery (decided now: shared by all scenarios)."""
    ev = data.ev
    if ev is None or ev.capacity_wh <= 0 or ev.max_charge_wh <= 0:
        return None
    inf = math.inf
    start = min(max(ev.soc_wh, 0.0), ev.capacity_wh)
    soc = [b.var(start, start)]
    charge: list[int] = []
    on_prev: Optional[int] = None
    for t in range(n):
        full = ev.max_charge_wh * _factor(data, t)
        cap = ev.slot_cap_wh[t] if t < len(ev.slot_cap_wh) else None
        upper = full if cap is None else max(0.0, min(full, float(cap)))
        e = b.var(0.0, upper)
        if ev.min_rate > 0 or ev.start_cost > 0:
            on = b.var(0.0, 1.0, 0.0, integer=True)
            b.row({e: 1.0, on: -upper}, -inf, 0.0)
            # Smallest rate of one original slot (a coarse slot may run part of it); a
            # start only means something when "on" draws real power (1 % at least).
            rate = max(ev.min_rate, 0.01 if ev.start_cost > 0 else 0.0)
            low = min(rate * ev.max_charge_wh, upper)
            b.row({e: 1.0, on: -low}, 0.0, inf)
            if ev.start_cost > 0:
                started = b.var(0.0, 1.0, ev.start_cost)
                if on_prev is None:
                    b.row({started: 1.0, on: -1.0}, -1.0 if ev.charging_now else 0.0, inf)
                else:
                    b.row({started: 1.0, on: -1.0, on_prev: 1.0}, 0.0, inf)
            on_prev = on
        soc.append(b.var(0.0, max(ev.max_soc_wh, start)))
        b.row({soc[t + 1]: 1.0, soc[t]: -1.0, e: -ev.charge_efficiency}, 0.0, 0.0)
        app_load[t][e] = app_load[t].get(e, 0.0) + 1.0
        charge.append(e)
    if ev.target_wh is not None:
        k = n if ev.target_slot is None else min(max(int(ev.target_slot), 0), n)
        target = min(ev.target_wh, ev.max_soc_wh)
        if target > start:
            short = b.var(0.0, target, ev.shortfall_per_wh)
            b.row({soc[k]: 1.0, short: 1.0}, target, inf)
    return _EVVars(charge, soc)


def solve_dispatch(data: DispatchInput) -> DispatchResult:
    """Build and solve the dispatch model; see the module docstring.

    With ``data.scenarios`` the expected cost over the weighted scenarios is minimised.
    Appliance starts and the battery decisions of the first ``data.shared_slots`` slots
    are the same in every scenario (they are executed before the outcome is known); the
    rest adapts per scenario. The result reports the first scenario (the central one).
    """
    started = time.perf_counter()
    n = min(len(data.pv_wh), len(data.load_wh), len(data.price_per_wh), len(data.feed_in_per_wh))
    if n == 0:
        raise ValueError("empty horizon")
    if data.coarsen_after_slots is not None and data.coarse_factor > 1:
        from akkudoktoreos.optimization.milp.coarsen import (
            coarsen_input,
            expand_result,
            fine_slots_needed,
            slot_groups,
        )

        fine = fine_slots_needed(data, int(data.coarsen_after_slots))
        if fine < n - 1:
            groups = slot_groups(n, fine, int(data.coarse_factor))
            coarse = coarsen_input(data, groups)
            return expand_result(solve_dispatch(coarse), data, groups)
    price = [float(v) for v in data.price_per_wh[:n]]
    feed = [float(v) for v in data.feed_in_per_wh[:n]]
    scenarios = list(data.scenarios) or [(1.0, list(data.pv_wh), list(data.load_wh))]
    total_weight = sum(max(w, 0.0) for w, _, _ in scenarios) or 1.0
    b = _Builder()

    # Appliances (shared by all scenarios).
    app_vars: list[list[int]] = []
    app_load: list[dict[int, float]] = [dict() for _ in range(n)]
    for run in data.appliances:
        starts = [s for s in run.allowed_starts]
        xs = [b.var(0.0, 1.0, 0.0, integer=True) for _ in starts]
        app_vars.append(xs)
        b.row({xv: 1.0 for xv in xs}, 1.0, 1.0)
        for xv, start in zip(xs, starts):
            for offset, energy in enumerate(run.profile_wh):
                slot = start + offset
                if 0 <= slot < n and energy:
                    app_load[slot][xv] = app_load[slot].get(xv, 0.0) + float(energy)

    ev_vars = _add_ev(b, data, n, app_load)

    # Heat pumps (decided before the outcome is known: shared by all scenarios).
    thermal_vars = []
    for block in data.thermal:
        if block.n < n:
            raise ValueError("thermal block shorter than the horizon")
        block_n = replace(block, n=n) if block.n > n else block
        candidates = None
        if block_n.legionella_deadline is not None and block_n.legionella_c is not None:
            _, pv0, load0 = scenarios[0]
            surplus_wh = [
                max(0.0, float(pv0[t]) - float(load0[t])) if t < min(len(pv0), len(load0)) else 0.0
                for t in range(n)
            ]
            candidates = legionella_candidate_slots(
                block_n, rod_marginal_price_per_wh(block_n, price, feed, surplus_wh)
            )
        tv = add_thermal(b, block_n, candidates)
        thermal_vars.append(tv)
        if block.separate_price_per_wh is None:
            for t in range(n):
                app_load[t][tv.hp_wh[t]] = app_load[t].get(tv.hp_wh[t], 0.0) + 1.0

    built: list[_ScenarioVars] = []
    for weight, pv_s, load_s in scenarios:
        pv = [max(float(v), 0.0) for v in list(pv_s)[:n]]
        load = [max(float(v), 0.0) for v in list(load_s)[:n]]
        if len(pv) < n or len(load) < n:
            raise ValueError("scenario shorter than the horizon")
        built.append(
            _add_scenario(b, data, pv, load, price, feed, max(weight, 0.0) / total_weight, app_load)
        )

    # Non-anticipativity: the first slots' battery decisions are common.
    first = built[0]
    for other in built[1:]:
        for t in range(min(data.shared_slots, n)):
            for a_var, b_var in (
                (first.chg_grid[t], other.chg_grid[t]),
                (first.dis[t], other.dis[t]),
            ):
                if a_var is not None and b_var is not None:
                    b.row({a_var: 1.0, b_var: -1.0}, 0.0, 0.0)
    imp, exp, chg_pv, chg_grid, dis, soc = (
        first.imp,
        first.exp,
        first.chg_pv,
        first.chg_grid,
        first.dis,
        first.soc,
    )
    switch_vars = _add_switching(b, data, first)
    weight0, pv0, load0 = scenarios[0]
    _add_hedge(
        b,
        data,
        first,
        [max(float(v), 0.0) for v in list(pv0)[:n]],
        [max(float(v), 0.0) for v in list(load0)[:n]],
        price,
        feed,
        max(weight0, 0.0) / total_weight,
    )

    outcome = b.solve(data.time_limit_s, data.mip_rel_gap)
    tie_break: Optional[str] = None
    if switch_vars is not None and data.switching is not None and data.switching.tie_break:
        outcome, tie_break = _switch_tie_break(b, data, switch_vars, outcome)
    status, objective = outcome.status, outcome.objective
    elapsed = time.perf_counter() - started
    if not outcome.ok or outcome.solution is None:
        return DispatchResult(status, False, None, [], [], [], [], [], [], [], [], elapsed)

    sol: np.ndarray = outcome.solution

    def values(ids: Sequence[Optional[int]]) -> list[float]:
        return [float(sol[i]) if i is not None else 0.0 for i in ids]

    appliance_starts = [int(np.argmax([sol[v] for v in xs])) for xs in app_vars]
    appliance_wh = [0.0] * n
    for run, chosen in zip(data.appliances, appliance_starts):
        start = run.allowed_starts[chosen]
        for offset, energy in enumerate(run.profile_wh):
            if 0 <= start + offset < n:
                appliance_wh[start + offset] += float(energy)
    return DispatchResult(
        status=status,
        success=True,
        objective=objective,
        grid_import_wh=values(imp),
        grid_export_wh=values(exp),
        charge_pv_wh=values(chg_pv),
        charge_grid_wh=values(chg_grid),
        discharge_wh=values(dis),
        soc_wh=[float(sol[i]) for i in soc],
        appliance_starts=appliance_starts,
        appliance_wh=appliance_wh,
        solve_seconds=elapsed,
        thermal=[trajectory(sol, tv) for tv in thermal_vars],
        optimal=outcome.optimal,
        mip_gap=outcome.mip_gap,
        mode_changes=switch_vars.count(sol) if switch_vars is not None else None,
        tie_break=tie_break,
        import_excess_wh=[float(sol[i]) for i in first.excess if i is not None],
        ev_charge_wh=values(ev_vars.charge) if ev_vars is not None else [],
        ev_soc_wh=[float(sol[i]) for i in ev_vars.soc] if ev_vars is not None else [],
    )


def battery_states(
    result: DispatchResult,
    max_charge_wh: float,
    charge_levels: Sequence[float],
    threshold_wh: float = 1.0,
) -> list[int]:
    """GENETIC battery state per slot for a dispatch result.

    Without direct marketing the GENETIC states are: idle ``0`` (PV surplus charges, no
    discharge), discharge allowed ``len(levels)`` (self-consumption) and AC charge
    ``2*len(levels) + i`` with charge level ``levels[i]``.
    """
    n_levels = len(charge_levels)
    states = []
    for grid, discharge in zip(result.charge_grid_wh, result.discharge_wh):
        if grid > threshold_wh and max_charge_wh > 0:
            factor = min(grid / max_charge_wh, 1.0)
            # Smallest level that delivers the planned grid charge (the simulator caps
            # at the free capacity anyway).
            reaching = [i for i, level in enumerate(charge_levels) if level >= factor - 1e-6]
            index = (
                min(reaching, key=lambda i: charge_levels[i])
                if reaching
                else int(np.argmax(charge_levels))
            )
            states.append(2 * n_levels + index)
        elif discharge > threshold_wh:
            states.append(n_levels)
        else:
            states.append(0)
    return states
