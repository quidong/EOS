"""Mixed-integer linear dispatch of battery, grid and appliances (RL29 fork).

A pure model, independent of EOS objects: forecasts and device figures go in, a
cost-optimal schedule comes out. Solved with HiGHS through ``scipy.optimize.milp``
(SciPy is already an EOS dependency, so no solver has to be installed).

Per slot ``t`` (energies in Wh, prices per Wh)::

    pv[t] + imp[t] + dis[t] = load[t] + appliance[t] + chg_pv[t] + chg_grid[t] + exp[t]
    soc[t+1] = soc[t] + eta_c * (chg_pv[t] + chg_grid[t]) - dis[t] / eta_d
    min_soc <= soc <= max_soc,  chg_pv + chg_grid <= max_charge,  dis <= max_discharge
    chg_pv[t] <= pv[t] - load[t],  exp[t] <= pv[t]  (surplus charging; only PV is fed in)
    dis[t] <= load[t] + appliance[t]              (the battery serves the house only)
    appliance[t] = sum_k x[g,k] * profile_g[t - start_k],  sum_k x[g,k] = 1,  x binary

    minimise  sum_t price[t]*imp[t] - feed_in[t]*exp[t] + lcos*dis[t]
              - terminal_value(soc[T])

``terminal_value`` is the piecewise linear credit for the energy left in the battery
(concave segments); it keeps the battery from being emptied at the horizon end.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np


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
    """Factor from DC battery content to the AC energy the terminal curve values."""
    max_grid_import_wh: Optional[float] = None
    max_grid_export_wh: Optional[float] = None
    time_limit_s: float = 10.0
    mip_rel_gap: float = 1e-4


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
        self, time_limit: float, gap: float
    ) -> tuple[str, bool, Optional[np.ndarray], Optional[float]]:
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import csr_array

        n = len(self.lb)
        data, indices, indptr = [], [], [0]
        for row in self.rows:
            for k, v in sorted(row.items()):
                indices.append(k)
                data.append(v)
            indptr.append(len(indices))
        constraints = []
        if self.rows:
            matrix = csr_array((data, indices, indptr), shape=(len(self.rows), n))
            constraints.append(
                LinearConstraint(matrix, np.array(self.row_lb), np.array(self.row_ub))
            )
        result = milp(
            c=np.array(self.cost),
            integrality=np.array(self.integer),
            bounds=Bounds(np.array(self.lb), np.array(self.ub)),
            constraints=constraints,
            options={"time_limit": float(time_limit), "mip_rel_gap": float(gap), "disp": False},
        )
        ok = result.x is not None and result.status in (0, 1)  # optimal / limit with solution
        return (
            str(result.message),
            ok,
            result.x,
            (float(result.fun) if result.fun is not None else None),
        )


def solve_dispatch(data: DispatchInput) -> DispatchResult:
    """Build and solve the dispatch model; see the module docstring."""
    started = time.perf_counter()
    n = min(len(data.pv_wh), len(data.load_wh), len(data.price_per_wh), len(data.feed_in_per_wh))
    if n == 0:
        raise ValueError("empty horizon")
    pv = [max(float(v), 0.0) for v in data.pv_wh[:n]]
    load = [max(float(v), 0.0) for v in data.load_wh[:n]]
    price = [float(v) for v in data.price_per_wh[:n]]
    feed = [float(v) for v in data.feed_in_per_wh[:n]]
    b = _Builder()
    inf = math.inf

    imp = [
        b.var(
            0.0, data.max_grid_import_wh if data.max_grid_import_wh is not None else inf, price[t]
        )
        for t in range(n)
    ]
    # Negative feed-in tariff: exporting costs money; the model may curtail by not exporting
    # (PV that is neither used nor exported is curtailed).
    exp = [
        b.var(
            0.0,
            min(pv[t], data.max_grid_export_wh if data.max_grid_export_wh is not None else inf),
            -feed[t],
        )
        for t in range(n)
    ]

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
            # PV charging only from the surplus over the fixed load, as in the simulation
            # (idle and self-consumption states charge from surplus only).
            c_pv = b.var(0.0, min(max(pv[t] - load[t], 0.0), bat.max_charge_wh))
            c_grid = b.var(0.0, bat.max_charge_wh if bat.allow_grid_charge else 0.0, 1e-9)
            d = b.var(0.0, bat.max_discharge_wh, bat.lcos_per_wh)
            chg_pv[t], chg_grid[t], dis[t] = c_pv, c_grid, d
            soc.append(b.var(low, max(bat.max_soc_wh, start_soc)))
            b.row({c_pv: 1.0, c_grid: 1.0}, -inf, bat.max_charge_wh)
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
        # Terminal credit: sum of segment energies equals the AC value of the end SoC.
        if data.terminal_segments:
            seg_vars = [
                b.var(0.0, max(width, 0.0), -value) for width, value in data.terminal_segments
            ]
            terminal: dict[int, float] = {v: 1.0 for v in seg_vars}
            terminal[soc[n]] = -data.terminal_dc_to_ac
            # Segments may stay below the content (credit flat beyond the last breakpoint).
            b.row(terminal, -inf, 0.0)

    # Appliances.
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

    status, ok, solution, objective = b.solve(data.time_limit_s, data.mip_rel_gap)
    elapsed = time.perf_counter() - started
    if not ok or solution is None:
        return DispatchResult(status, False, None, [], [], [], [], [], [], [], [], elapsed)

    sol: np.ndarray = solution

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
