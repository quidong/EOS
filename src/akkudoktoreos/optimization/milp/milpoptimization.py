"""MILP optimization, selectable next to GENETIC (eos-plus fork).

The algorithm ``MILP`` uses the GENETIC machinery for everything that is not the
search itself - parameter preparation, the device simulation that judges every plan,
the solution and plan formats Home Assistant reads, the PDF report - and replaces the
random search by mathematical optimization:

1. **Dispatch MILP** (``dispatch.py``): battery charge/discharge, grid import/export and
   appliance starts, cost-optimal for a given heat pump schedule, with the terminal value
   of the battery energy (HiGHS, typically well below a second).
2. **Heat pump schedule**: first the joint MILP of battery, grid, appliances *and* the
   linear heat pump model (``thermal.py``, ``milp.thermal_model``) plans the heat for
   building and tank over the whole horizon in one solve; its heat schedule is turned
   into operating modes by following it with the exact simulation. These candidates,
   the status quo, the warm start of the previous run and the PV-/price-led heuristics
   are then scored with the exact thermal simulation (penalties, stored heat) plus the
   optimal dispatch cost for their electric load; a coordinate search over the modes of
   each hour improves the best one within ``hp_search_seconds``.
3. **Check and polish**: the best candidates are encoded as genomes and put at the head
   of the initial population; ``polish_generations`` GENETIC generations then run on the
   exact simulation (0 = take the MILP plan as it is, after simulating it). The result
   is never worse than the best MILP candidate under the simulation.

Not covered by the MILP (handled by the polish step): electric vehicles, battery export
for direct marketing.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from loguru import logger

from akkudoktoreos.core.forkpaths import fork_data_dir
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import (
    GeneticOptimizationParameters,
)
from akkudoktoreos.optimization.milp.dispatch import (
    ApplianceRun,
    BatteryModel,
    DispatchInput,
    DispatchResult,
    battery_states,
    solve_dispatch,
)
from akkudoktoreos.optimization.milp.milpsettings import MilpCommonSettings
from akkudoktoreos.optimization.milp.thermal import project_modes, thermal_block


def terminal_segments(
    curve: Any, fixed_price_per_wh: float, capacity_wh: float
) -> list[tuple[float, float]]:
    """(width Wh, value per Wh) segments of the terminal value curve.

    The curve's breakpoints become segments with the slope of each piece; without a
    curve the FIXED value applies to the whole capacity. Segments beyond the last
    breakpoint get no value (the curve is flat there).
    """
    energy = list(getattr(curve, "energy_wh", []) or [])
    value = list(getattr(curve, "value_euro", []) or [])
    if len(energy) >= 2 and len(energy) == len(value):
        segments = []
        for i in range(1, len(energy)):
            width = energy[i] - energy[i - 1]
            if width <= 0:
                continue
            segments.append((float(width), float((value[i] - value[i - 1]) / width)))
        if segments:
            return segments
    if fixed_price_per_wh and capacity_wh > 0:
        return [(float(capacity_wh), float(fixed_price_per_wh))]
    return []


class MilpOptimization(GeneticOptimization):
    """GENETIC run whose initial population is led by MILP solutions."""

    @property
    def _milp(self) -> MilpCommonSettings:
        return self.config.optimization.milp

    # ------------------------------------------------------------------
    # Model input from the prepared GENETIC run
    # ------------------------------------------------------------------

    def _dispatch_input(self, extra_load: Optional[list[float]]) -> DispatchInput:
        sim = self.simulation
        n = self.control_slots
        if (
            sim.load_energy_array is None
            or sim.pv_prediction_wh is None
            or sim.elect_price_hourly is None
            or sim.elect_revenue_per_hour_arr is None
        ):
            raise ValueError("MILP: simulation is not prepared")
        base = [float(v) for v in sim.load_energy_array[:n]]
        if extra_load is not None:
            base = [b + float(e) for b, e in zip(base, extra_load[:n])]
        battery_model: Optional[BatteryModel] = None
        segments: list[tuple[float, float]] = []
        dc_to_ac = 1.0
        bat = sim.battery
        if bat is not None:
            inverter = sim.inverter
            ac_to_dc = getattr(inverter, "ac_to_dc_efficiency", 1.0) if inverter else 1.0
            dc_to_ac = getattr(inverter, "reference_dc_to_ac_efficiency", 1.0) if inverter else 1.0
            max_charge = float(bat.max_charge_power_w) * self.slot_duration_h
            max_ac = getattr(inverter, "max_ac_charge_power_w", None) if inverter else None
            allow_grid = ac_to_dc > 0 and (max_ac is None or max_ac > 0)
            battery_model = BatteryModel(
                capacity_wh=float(bat.capacity_wh),
                soc_wh=float(bat.soc_wh),
                min_soc_wh=float(bat.min_soc_wh),
                max_soc_wh=float(bat.max_soc_wh),
                max_charge_wh=max_charge,
                max_discharge_wh=max_charge,
                charge_efficiency=float(bat.charging_efficiency),
                discharge_efficiency=float(bat.discharging_efficiency) * float(dc_to_ac),
                lcos_per_wh=float(bat.levelized_cost_of_storage_kwh or 0.0) / 1000.0,
                allow_grid_charge=bool(allow_grid and self.bat_possible_charge_values),
            )
            segments = terminal_segments(
                getattr(self, "_terminal_value_curve", None),
                float(getattr(self._run_parameters.ems, "price_per_wh_battery", 0.0) or 0.0),
                float(bat.capacity_wh),
            )
        appliances = []
        for gene in self.appliance_layout.genes:
            appliance = sim.home_appliances[gene.appliance_index]
            appliances.append(
                ApplianceRun(
                    allowed_starts=list(gene.allowed_start_slots),
                    profile_wh=[float(v) for v in appliance.run_energy_wh],
                )
            )
        pv = [float(v) for v in sim.pv_prediction_wh[:n]]
        settings = self._milp
        scenarios: list[tuple[float, Any, Any]] = []
        if settings.scenarios:
            # Only the base load is uncertain; heat pump load (extra_load) is planned.
            base_only = [float(v) for v in sim.load_energy_array[:n]]
            fixed = [b - o for b, o in zip(base, base_only)]
            pv_spread = settings.pv_scenario_spread
            load_spread = self._load_spread()
            low_pv = [v * (1 - pv_spread) for v in pv]
            high_pv = [v * (1 + pv_spread) for v in pv]
            high_load = [o * (1 + load_spread) + f for o, f in zip(base_only, fixed)]
            low_load = [o * (1 - load_spread) + f for o, f in zip(base_only, fixed)]
            scenarios = [(0.5, pv, base), (0.25, low_pv, high_load), (0.25, high_pv, low_load)]
        slot_h = self.slot_duration_h
        return DispatchInput(
            pv_wh=pv,
            load_wh=base,
            scenarios=scenarios,
            shared_slots=settings.shared_slots,
            max_grid_import_wh=(
                settings.max_grid_import_w * slot_h
                if settings.max_grid_import_w and not self._ignore_import_limit
                else None
            ),
            max_grid_export_wh=(
                settings.max_grid_export_w * slot_h if settings.max_grid_export_w else None
            ),
            price_per_wh=[float(v) for v in sim.elect_price_hourly[:n]],
            feed_in_per_wh=[float(v) for v in sim.elect_revenue_per_hour_arr[:n]],
            battery=battery_model,
            appliances=appliances,
            terminal_segments=segments,
            terminal_dc_to_ac=float(dc_to_ac),
            time_limit_s=self._milp.time_limit_s,
            mip_rel_gap=self._milp.mip_rel_gap,
        )

    _ignore_import_limit: bool = False
    _thermal_keys: set[tuple[int, ...]] = set()

    def _load_spread(self) -> float:
        """Relative load spread of the outer scenarios (setting, LoadML quantiles, 0.2)."""
        configured = self._milp.load_scenario_spread
        if configured is not None:
            return float(configured)
        try:
            import json

            path = fork_data_dir(self.config.general.data_folder_path) / "load_ml.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("used"):
                spread = max(
                    float(data.get("spread_low", 0.0)), float(data.get("spread_high", 0.0))
                )
                if 0.0 < spread < 0.9:
                    return spread
        except Exception:  # noqa: S110 - no LoadML report: default spread
            pass
        return 0.2

    def _score(self, hp_genes: Optional[list[int]]) -> tuple[float, Optional[DispatchResult]]:
        """Dispatch cost plus heat pump extra cost for a heat pump schedule."""
        extra_cost = 0.0
        hp_load: Optional[list[float]] = None
        layout = self._hp_layout()
        if layout and hp_genes is not None:
            hp_load, extra_cost = layout.evaluate(hp_genes)
        key = tuple(hp_genes or [])
        cached = self._milp_cache.get(key)
        if cached is not None:
            return cached
        result = solve_dispatch(self._dispatch_input(hp_load))
        self._milp_solves += 1
        if not result.success and self._milp.max_grid_import_w and not self._ignore_import_limit:
            # The fixed load alone exceeds the import limit: plan without it.
            logger.warning(
                "MILP: grid import limit not feasible ({}); planning without it.", result.status
            )
            self._ignore_import_limit = True
            result = solve_dispatch(self._dispatch_input(hp_load))
            self._milp_solves += 1
        score = (
            (result.objective + extra_cost)
            if result.success and result.objective is not None
            else float("inf")
        )
        self._milp_cache[key] = (score, result)
        return score, result

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------

    def _battery_genes(self, result: DispatchResult) -> list[int]:
        n_total = self.control_end_slot
        states = [0] * n_total
        bat = self.simulation.battery
        if bat is None or not result.charge_grid_wh:
            return states
        layout = self._battery_state_layout()
        encoded = battery_states(
            result,
            max_charge_wh=float(bat.max_charge_power_w) * self.slot_duration_h,
            charge_levels=list(self.bat_possible_charge_values),
        )
        n_levels = len(self.bat_possible_charge_values)
        for slot, state in enumerate(encoded[:n_total]):
            if layout.dc_allowed_state is not None:
                # Direct marketing layout: idle must allow PV charging explicitly.
                if state == 0:
                    state = layout.dc_allowed_state
                elif state == n_levels and layout.self_consumption_state is not None:
                    state = layout.self_consumption_state
            states[slot] = state
        return states

    def _genome(self, result: DispatchResult, hp_genes: Optional[list[int]]) -> list[int]:
        import numpy as np

        return self.merge_individual(
            np.array(self._battery_genes(result), dtype=int),
            None,
            list(result.appliance_starts),
            hp_genes if self._hp_layout() else None,
        )

    # ------------------------------------------------------------------
    # Hook
    # ------------------------------------------------------------------

    def _hp_candidates(self, parameters: GeneticOptimizationParameters) -> list[list[int]]:
        layout = self._hp_layout()
        if not layout:
            return []
        sim = self.simulation
        n = self.control_slots
        candidates = [layout.default_genes()]
        if (
            sim.pv_prediction_wh is None
            or sim.load_energy_array is None
            or sim.elect_price_hourly is None
        ):
            return candidates
        warm = getattr(self, "_warm_hp_genes", None)
        if warm and layout.valid(warm):
            candidates.append(list(warm))
        candidates.extend(
            layout.heuristic_gene_sets(
                sim.pv_prediction_wh[:n], sim.load_energy_array[:n], sim.elect_price_hourly[:n]
            )
        )
        unique: dict[tuple[int, ...], list[int]] = {}
        for genes in candidates:
            unique.setdefault(tuple(int(g) for g in genes), [int(g) for g in genes])
        return list(unique.values())

    def _thermal_candidates(self) -> list[list[int]]:
        """Heat pump modes that follow the joint MILP heat schedule (see ``thermal.py``)."""
        layout = self._hp_layout()
        self.thermal_summary: dict[str, Any] = {}
        if not layout or not self._milp.thermal_model:
            return []
        started = time.monotonic()
        try:
            blocks = [thermal_block(plan) for plan in layout.plans]
            if any(block is None for block in blocks):
                logger.info(
                    "MILP: heat pump model not applicable (cooling season); mode search only."
                )
                return []
            data = self._dispatch_input(None)
            data.thermal = [block for block in blocks if block is not None]
            result = solve_dispatch(data)
            self._milp_solves += 1
            if (
                not result.success
                and self._milp.max_grid_import_w
                and not self._ignore_import_limit
            ):
                self._ignore_import_limit = True
                data = self._dispatch_input(None)
                data.thermal = [block for block in blocks if block is not None]
                result = solve_dispatch(data)
                self._milp_solves += 1
            if not result.success or not result.thermal:
                logger.warning("MILP: heat pump model not solved ({}).", result.status)
                return []
            variants: list[list[int]] = []
            for room_weight in (1.0, 0.0, 3.0):
                genes: list[int] = []
                for plan, traj in zip(layout.plans, result.thermal):
                    genes.extend(project_modes(plan, traj, room_weight=room_weight))
                if genes not in variants:
                    variants.append(genes)
            seconds = time.monotonic() - started
            self.thermal_summary = {
                "seconds": round(seconds, 2),
                "objective": round(result.objective, 4) if result.objective is not None else None,
                "heat_pump_wh": round(sum(sum(t.electric_wh) for t in result.thermal), 1),
                "candidates": len(variants),
            }
            logger.info(
                "MILP: heat pump model solved in {:.1f} s (objective {:.3f}, heat pump {:.1f} kWh), "
                "{} mode candidate(s).",
                seconds,
                result.objective if result.objective is not None else float("nan"),
                self.thermal_summary["heat_pump_wh"] / 1000.0,
                len(variants),
            )
            self._thermal_keys.update(tuple(v) for v in variants)
            return variants
        except Exception as exc:  # never lose the run because of the heat pump model
            logger.warning("MILP: heat pump model failed ({}); mode search only.", exc)
            return []

    def _coordinate_search(
        self, best: list[int], best_score: float, deadline: float
    ) -> tuple[list[int], float]:
        """Improve one gene at a time (all allowed values) until no change or time is up."""
        layout = self._hp_layout()
        bounds: list[int] = []
        for plan in layout.plans:
            bounds.extend(plan.upper_bounds())
        improved = True
        while improved and time.monotonic() < deadline:
            improved = False
            for index, upper in enumerate(bounds):
                if time.monotonic() >= deadline:
                    break
                current = best[index]
                for value in range(upper + 1):
                    if value == current:
                        continue
                    trial = list(best)
                    trial[index] = value
                    if not layout.valid(trial):
                        continue
                    score, _ = self._score(trial)
                    if score < best_score - 1e-6:
                        best, best_score, current = trial, score, value
                        improved = True
        return best, best_score

    def _extra_seeds(
        self, parameters: GeneticOptimizationParameters, start_slot: int
    ) -> list[list[int]]:
        if self.optimize_ev:
            logger.warning("MILP: electric vehicles are not modelled; GENETIC search only.")
            return []
        if self.optimize_battery_grid_export:
            logger.info("MILP: battery export (direct marketing) left to the polish step.")
        started = time.monotonic()
        self._run_parameters = parameters
        self._milp_cache: dict[tuple[int, ...], tuple[float, Optional[DispatchResult]]] = {}
        self._ignore_import_limit = False
        self._milp_solves = 0
        warm = parameters.start_solution
        self._warm_hp_genes = None
        if warm is not None and self._hp_layout():
            try:
                shifted = self._start_solution_for_run_start(
                    warm, self._resolve_start_solution_datetime(parameters)
                )
                if shifted is not None:
                    shifted = self._start_solution_for_slot_grid(shifted)
                    self._warm_hp_genes = self._heat_pump_genes(list(shifted))
            except Exception as exc:  # warm start is optional
                logger.debug("MILP: warm start heat pump genes unusable: {}", exc)

        scored: list[tuple[float, list[int], DispatchResult]] = []
        self._thermal_keys = set()
        self._thermal_start = False
        if self._hp_layout():
            candidates = self._hp_candidates(parameters)
            for genes in self._thermal_candidates():
                if genes not in candidates:
                    candidates.append(genes)
            for genes in candidates:
                score, result = self._score(genes)
                if result is not None and result.success:
                    scored.append((score, genes, result))
            if not scored:
                logger.error("MILP: no feasible dispatch; GENETIC search only.")
                return []
            scored.sort(key=lambda item: item[0])
            status_quo = self._score(self._hp_layout().default_genes())[0]
            start_from_thermal = tuple(scored[0][1]) in self._thermal_keys
            self._thermal_start = start_from_thermal
            logger.info(
                "MILP: best heat pump start {} ({:.3f}; status quo {:.3f}).",
                "from the heat pump model" if start_from_thermal else "from the heuristics",
                scored[0][0],
                status_quo,
            )
            # Local search from the best start of each family (heat pump model and
            # heuristics): they often end in different local optima. Time left over by
            # the first search goes to the second.
            starts = [scored[0]]
            other = next(
                (
                    item
                    for item in scored[1:]
                    if (tuple(item[1]) in self._thermal_keys) != start_from_thermal
                ),
                None,
            )
            if other is not None:
                starts.append(other)
            end = time.monotonic() + self._milp.hp_search_seconds
            found: list[tuple[float, list[int]]] = []
            for index, (score0, genes0, _) in enumerate(starts):
                remaining = max(0.0, end - time.monotonic())
                deadline = time.monotonic() + remaining / (len(starts) - index)
                found.append(self._coordinate_search(list(genes0), score0, deadline)[::-1])
            best_score, best_genes = min(found, key=lambda item: item[0])
            if best_score < scored[0][0] - 1e-9:
                _, result = self._score(best_genes)
                scored.insert(0, (best_score, best_genes, result))  # type: ignore[arg-type]
        else:
            score, result = self._score(None)
            if result is None or not result.success:
                logger.error(
                    "MILP: no feasible dispatch ({}); GENETIC search only.",
                    result.status if result else "-",
                )
                return []
            scored.append((score, [], result))

        seeds = []
        seen: set[tuple[int, ...]] = set()
        for score, genes, result in scored:
            genome = self._genome(result, genes or None)
            key = tuple(genome)
            if key in seen:
                continue
            seen.add(key)
            seeds.append(genome)
            if len(seeds) >= self._milp.seeds:
                break
        best_score, _, best_result = scored[0]
        logger.info(
            "MILP: {} dispatch solves in {:.1f} s, best cost {:.3f} (dispatch {:.3f}), {} seeds.",
            self._milp_solves,
            time.monotonic() - started,
            best_score,
            best_result.objective if best_result.objective is not None else float("nan"),
            len(seeds),
        )
        self.milp_summary = {
            "thermal": getattr(self, "thermal_summary", {}),
            "thermal_best_start": bool(getattr(self, "_thermal_start", False)),
            "solves": self._milp_solves,
            "seconds": round(time.monotonic() - started, 2),
            "best_cost": round(best_score, 4),
            "seeds": len(seeds),
        }
        return seeds
