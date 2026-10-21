"""Heat pump genes for the GENETIC optimizer (RL29 fork).

The heat pump is optimized jointly with battery, EV and appliances: every
configured heat pump adds two blocks of genes to the genome, one space heating
mode and one hot water mode per gene period (one hour). The genome stays
compact (48 h horizon: 96 genes per heat pump) and the modes match what the
actuators can execute without frequent switching (SG Ready relays, the
"Warmwasser PUSH" contact and the heating rod).

Per candidate, the thermal model (``akkudoktoreos.devices.thermal``)
turns the modes into the electric load of heat pump and heating rod per slot.
The load enters the energy balance of the simulation before the inverter, so
PV, battery and grid see it like any other consumer. Penalties for comfort
violations, a missed legionella heat-up and mode changes plus the value of the
heat stored at the end of the horizon are added to the fitness.

The thermal simulation depends on the heat pump genes only. Results are cached
per gene combination, so the many candidates that differ only in battery genes
cost one dictionary lookup.

Gene layout per heat pump (``n`` = number of gene periods)::

    [heating_0 .. heating_n-1, dhw_0 .. dhw_n-1]

Each gene is an index into the allowed modes of its dimension.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from loguru import logger
from pydantic import Field

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import (
    DhwMode,
    HeatingMode,
    HeatPumpSystem,
    SlotInputs,
    ThermalState,
)
from akkudoktoreos.optimization.genetic.geneticabc import GeneticParametersBaseModel

# SG Ready state reported per space heating mode; configurable per device
# (devices.heat_pumps.<id>.optimization.sg_ready_states).
DEFAULT_SG_READY_STATES = {
    "REDUCED": 1,
    "NORMAL": 2,
    "RAISED": 3,
    "PREHEAT": 3,
    "FORCED": 4,
}


class GeneticHeatPumpParameters(GeneticParametersBaseModel):
    """Heat pump device and its current thermal state for one optimization run."""

    device_id: str = Field(json_schema_extra={"description": "ID of the heat pump."})
    settings: HeatPumpCommonSettings = Field(
        json_schema_extra={"description": "Device configuration (devices.heat_pumps.<id>)."}
    )
    initial_room_c: Optional[float] = Field(
        default=None,
        json_schema_extra={"description": "Measured room temperature [degC]; None = setpoint."},
    )
    initial_slab_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": "Measured screed temperature [degC]; None = steady state estimate."
        },
    )
    initial_dhw_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": "Measured hot water temperature [degC]; None = setpoint - hysteresis/2."
        },
    )
    dhw_setpoint_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": "Hot water setpoint read from the heat pump; None = configuration."
        },
    )
    power_limited_w: Optional[float] = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "description": (
                "Active §14a dimming: electric limit of heat pump plus rod from now [W]; "
                "None = not dimmed."
            )
        },
    )
    power_limited_hours: float = Field(
        default=1.0,
        gt=0,
        json_schema_extra={"description": "Assumed remaining duration of the dimming [h]."},
    )
    separate_meter_price_reduction_amt_wh: Optional[float] = Field(
        default=None,
        ge=0,
        json_schema_extra={
            "description": (
                "§14a module 2: the heat pump has its own meter; its import price is the "
                "household price minus this reduction [currency/Wh, gross]. It then cannot "
                "use PV or battery energy. None = shared meter."
            )
        },
    )
    hours_since_legionella: Optional[float] = Field(
        default=None,
        ge=0,
        json_schema_extra={
            "description": (
                "Hours since the tank last reached the legionella temperature; None = "
                "unknown (treated as just done, no heat-up required in this run)."
            )
        },
    )


def _nearest(allowed: Sequence[int], wanted: int) -> int:
    """Index into ``allowed`` of the mode closest to ``wanted`` (lower mode on a tie)."""
    best = min(range(len(allowed)), key=lambda i: (abs(allowed[i] - wanted), allowed[i]))
    return best


@dataclass(frozen=True)
class DhwOption:
    """One hot water choice of a gene period: a mode and, for the rod, its target."""

    mode: DhwMode
    rod_target_c: Optional[float] = None

    @property
    def label(self) -> str:
        """Operation mode id, e.g. ``HOLD``, ``HEAT_PUMP``, ``ROD_60``."""
        if self.mode == DhwMode.ROD and self.rod_target_c is not None:
            return f"ROD_{self.rod_target_c:g}"
        return self.mode.name


def dhw_mode_value(label: str) -> float:
    """Numeric hot water mode of an operation mode id (``ROD_60`` -> 2)."""
    if label.startswith("ROD"):
        return float(DhwMode.ROD)
    return float(DhwMode[label])


@dataclass
class HeatPumpOutcome:
    """Result of the thermal simulation of one heat pump gene combination."""

    electric_wh: list[float]
    """Electric energy of heat pump plus heating rod per slot [Wh]."""
    extra_cost: float
    """Penalties minus the value of the heat stored at the end [currency]."""
    comfort_kh: float = 0.0
    dhw_kh: float = 0.0
    legionella_ok: Optional[bool] = None
    mode_changes: int = 0
    stored_heat_credit: float = 0.0
    separate_meter_cost: float = 0.0
    """§14a module 2: cost of the separately metered energy [currency]."""
    detail: Optional[dict[str, Any]] = None


@dataclass
class HeatPumpRunPlan:
    """Everything one heat pump needs during a run, built once per run."""

    device_id: str
    system: HeatPumpSystem
    inputs: list[SlotInputs]
    initial: ThermalState
    slots_per_gene: int
    heating_modes: tuple[int, ...]
    dhw_options: tuple[DhwOption, ...]
    comfort_penalty: float
    dhw_penalty: float
    legionella_penalty: float
    mode_change_penalty: float
    building_heat_value: float
    """Value of building heat [currency/Wh] for the stored heat at the end."""
    tank_heat_value: float
    """Value of tank heat [currency/Wh]."""
    legionella_c: float = 60.0
    legionella_deadline_slot: Optional[int] = None
    """Slot by which the tank has to reach legionella_c; None = no requirement."""
    load_includes_heat_pump: bool = False
    separate_meter_price_per_wh: Optional[list[float]] = None
    """§14a module 2: own meter, price per slot; its energy is not part of the house balance."""
    sg_ready_states: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SG_READY_STATES))
    _cache: dict[tuple[int, ...], HeatPumpOutcome] = field(default_factory=dict, repr=False)

    CACHE_SIZE = 20000

    @property
    def n_slots(self) -> int:
        return len(self.inputs)

    @property
    def n_periods(self) -> int:
        return max(1, math.ceil(self.n_slots / self.slots_per_gene))

    @property
    def n_genes(self) -> int:
        return 2 * self.n_periods

    def upper_bounds(self) -> list[int]:
        """Highest gene value per gene."""
        return [len(self.heating_modes) - 1] * self.n_periods + [
            len(self.dhw_options) - 1
        ] * self.n_periods

    # --- option lookup -------------------------------------------------

    def heating_index(self, mode: int) -> int:
        """Gene value of the allowed heating mode closest to ``mode``."""
        return _nearest(self.heating_modes, int(mode))

    def dhw_index(self, mode: int, rod_target_c: Optional[float] = None) -> int:
        """Gene value of the hot water option closest to ``mode`` (and rod target)."""
        candidates = [i for i, o in enumerate(self.dhw_options) if o.mode == mode]
        if not candidates:
            # Fall back to the closest mode value (ROD -> HEAT_PUMP -> HOLD).
            modes = [int(o.mode) for o in self.dhw_options]
            return _nearest(modes, int(mode))
        if rod_target_c is None:
            return candidates[0]
        return min(
            candidates,
            key=lambda i: abs((self.dhw_options[i].rod_target_c or 0.0) - rod_target_c),
        )

    def rod_index_reaching(self, temperature_c: float) -> Optional[int]:
        """Lowest rod option whose target reaches ``temperature_c``."""
        reaching = [
            i
            for i, o in enumerate(self.dhw_options)
            if o.mode == DhwMode.ROD and (o.rod_target_c or 0.0) >= temperature_c - 0.5
        ]
        if not reaching:
            return None
        return min(reaching, key=lambda i: self.dhw_options[i].rod_target_c or 0.0)

    def heating_mode_reaching_dhw(self, temperature_c: float) -> Optional[int]:
        """Allowed heating mode whose hot water setpoint reaches ``temperature_c``."""
        reaching = [
            mode
            for mode in self.heating_modes
            if self.system.dhw_setpoint_c(HeatingMode(mode)) >= temperature_c - 0.5
        ]
        return min(reaching) if reaching else None

    def genes_for_modes(self, heating: Sequence[int], dhw: Sequence[Any]) -> list[int]:
        """Encode modes per period.

        ``heating`` holds heating mode values; ``dhw`` holds ``DhwOption``,
        ``(mode, rod_target)`` tuples or plain hot water mode values (the
        lowest rod target for ``DhwMode.ROD``).
        """
        dhw_genes = []
        for entry in dhw:
            if isinstance(entry, DhwOption):
                dhw_genes.append(self.dhw_index(entry.mode, entry.rod_target_c))
            elif isinstance(entry, tuple):
                dhw_genes.append(self.dhw_index(entry[0], entry[1]))
            else:
                dhw_genes.append(self.dhw_index(int(entry)))
        return [self.heating_index(mode) for mode in heating] + dhw_genes

    def default_genes(self) -> list[int]:
        """Status quo: NORMAL heating, hot water left to the heat pump controller."""
        n = self.n_periods
        return self.genes_for_modes([HeatingMode.NORMAL] * n, [DhwMode.HOLD] * n)

    def modes(self, genes: Sequence[int]) -> tuple[list[HeatingMode], list[DhwOption]]:
        """Heating mode and hot water option per period."""
        n = self.n_periods
        heating = [HeatingMode(self.heating_modes[int(g)]) for g in genes[:n]]
        dhw = [self.dhw_options[int(g)] for g in genes[n : 2 * n]]
        return heating, dhw

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def evaluate(self, genes: Sequence[int], detail: bool = False) -> HeatPumpOutcome:
        """Simulate the horizon for the given genes (cached unless ``detail``)."""
        key = tuple(int(g) for g in genes)
        if not detail:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        outcome = self._simulate(key, detail)
        if not detail:
            if len(self._cache) >= self.CACHE_SIZE:
                # FIFO eviction; the evaluation is deterministic.
                del self._cache[next(iter(self._cache))]
            self._cache[key] = outcome
        return outcome

    def _simulate(self, genes: tuple[int, ...], detail: bool) -> HeatPumpOutcome:
        heating, dhw = self.modes(genes)
        system = self.system
        step = system.step
        spg = self.slots_per_gene
        state = self.initial.copy()
        electric: list[float] = []
        comfort_kh = 0.0
        dhw_kh = 0.0
        max_dhw = state.dhw_c
        deadline = self.legionella_deadline_slot
        reached_by_deadline = deadline is not None and state.dhw_c >= self.legionella_c - 0.5
        rows: dict[str, list[Any]] = {}
        if detail:
            for name in (
                "heating_mode",
                "dhw_mode",
                "rod_target_c",
                "electric_wh",
                "hp_electric_wh",
                "rod_electric_wh",
                "heat_space_wh",
                "heat_dhw_wh",
                "room_c",
                "slab_c",
                "dhw_c",
                "flow_c",
                "cop_space",
                "forced_space_wh",
            ):
                rows[name] = []
        for slot, inputs in enumerate(self.inputs):
            period = slot // spg
            option = dhw[period]
            result = step(state, inputs, heating[period], option.mode, option.rod_target_c)
            state = result.state
            energy = result.hp_electric_wh + result.rod_electric_wh
            electric.append(energy)
            comfort_kh += result.comfort_violation_kh
            dhw_kh += result.dhw_violation_kh
            if state.dhw_c > max_dhw:
                max_dhw = state.dhw_c
            if deadline is not None and slot < deadline and state.dhw_c >= self.legionella_c - 0.5:
                reached_by_deadline = True
            if detail:
                rows["heating_mode"].append(heating[period].name)
                rows["dhw_mode"].append(option.label)
                rows["rod_target_c"].append(
                    float(option.rod_target_c)
                    if option.mode == DhwMode.ROD and option.rod_target_c
                    else 0.0
                )
                rows["electric_wh"].append(energy)
                rows["hp_electric_wh"].append(result.hp_electric_wh)
                rows["rod_electric_wh"].append(result.rod_electric_wh)
                rows["heat_space_wh"].append(result.heat_space_wh)
                rows["heat_dhw_wh"].append(result.heat_dhw_hp_wh + result.heat_dhw_rod_wh)
                rows["room_c"].append(state.room_c)
                rows["slab_c"].append(state.slab_c)
                rows["dhw_c"].append(state.dhw_c)
                rows["flow_c"].append(result.flow_c)
                rows["cop_space"].append(result.cop_space)
                rows["forced_space_wh"].append(result.forced_space_wh)

        # Mode changes between consecutive gene periods.
        n = self.n_periods
        changes = sum(1 for i in range(1, n) if heating[i] != heating[i - 1]) + sum(
            1 for i in range(1, n) if dhw[i] != dhw[i - 1]
        )

        # Value of the heat stored (or used up) compared to the start.
        building_start = system.building.stored_heat_wh(
            self.initial.slab_c, self.initial.room_c, 0.0
        )
        building_end = system.building.stored_heat_wh(state.slab_c, state.room_c, 0.0)
        tank_start = system.tank.stored_heat_wh(self.initial.dhw_c, 0.0)
        tank_end = system.tank.stored_heat_wh(state.dhw_c, 0.0)
        credit = (building_end - building_start) * self.building_heat_value + (
            tank_end - tank_start
        ) * self.tank_heat_value

        legionella_ok: Optional[bool] = None
        legionella_cost = 0.0
        if deadline is not None:
            legionella_ok = reached_by_deadline
            if not legionella_ok:
                shortfall = max(0.0, self.legionella_c - max_dhw)
                legionella_cost = self.legionella_penalty * (1.0 + shortfall / 10.0)

        separate_cost = 0.0
        if self.separate_meter_price_per_wh is not None:
            prices = self.separate_meter_price_per_wh
            separate_cost = sum(e * prices[i] for i, e in enumerate(electric) if i < len(prices))
        extra = (
            separate_cost
            + comfort_kh * self.comfort_penalty
            + dhw_kh * self.dhw_penalty
            + legionella_cost
            + changes * self.mode_change_penalty
            - credit
        )
        return HeatPumpOutcome(
            electric_wh=electric,
            extra_cost=extra,
            comfort_kh=comfort_kh,
            dhw_kh=dhw_kh,
            legionella_ok=legionella_ok,
            mode_changes=changes,
            stored_heat_credit=credit,
            separate_meter_cost=separate_cost,
            detail=rows if detail else None,
        )

    # ------------------------------------------------------------------
    # Seeds and mutation
    # ------------------------------------------------------------------

    def heuristic_genes(
        self,
        pv_wh: Sequence[float],
        load_wh: Sequence[float],
        price_per_wh: Sequence[float],
    ) -> list[list[int]]:
        """Domain-informed starting points for the search.

        Status quo, PV-led (raise heating and charge hot water while PV covers
        it, heating rod on large surplus), price-led (raise in cheap, reduce in
        expensive hours) and both combined. Only allowed modes are used. A due
        legionella heat-up is placed on the best hours before its deadline in
        each of them.
        """
        n = self.n_periods
        spg = self.slots_per_gene
        n_slots = min(self.n_slots, len(pv_wh), len(load_wh), len(price_per_wh))
        surplus: list[float] = []
        price: list[float] = []
        for p in range(n):
            slots = range(p * spg, min((p + 1) * spg, n_slots))
            if not slots:
                surplus.append(0.0)
                price.append(0.0)
                continue
            surplus.append(sum(pv_wh[s] - load_wh[s] for s in slots))
            price.append(sum(price_per_wh[s] for s in slots) / len(slots))
        ordered = sorted(price)
        low = ordered[len(ordered) // 4]
        high = ordered[(3 * len(ordered)) // 4]
        rod_wh = self.system.dhw.rod_power_w
        rods = [i for i, o in enumerate(self.dhw_options) if o.mode == DhwMode.ROD]
        lowest_rod = (
            min(rods, key=lambda i: self.dhw_options[i].rod_target_c or 0.0) if rods else None
        )
        hold = self.dhw_index(DhwMode.HOLD)
        push = self.dhw_index(DhwMode.HEAT_PUMP)
        allowed = set(self.heating_modes)

        def strongest(*modes: int) -> int:
            for mode in modes:
                if mode in allowed:
                    return mode
            return HeatingMode.NORMAL

        def pv_modes(p: int) -> tuple[Optional[int], Optional[int]]:
            heat: Optional[int] = None
            if surplus[p] > 2500:
                heat = strongest(HeatingMode.PREHEAT, HeatingMode.RAISED)
            elif surplus[p] > 1000:
                heat = strongest(HeatingMode.RAISED)
            water: Optional[int] = None
            if lowest_rod is not None and surplus[p] > rod_wh + 500:
                water = lowest_rod
            elif surplus[p] > 1500:
                water = push
            return heat, water

        def price_modes(p: int) -> tuple[int, int]:
            if price[p] <= low:
                return strongest(HeatingMode.RAISED), push
            if price[p] >= high:
                return strongest(HeatingMode.REDUCED), hold
            return HeatingMode.NORMAL, hold

        normal = self.heating_index(HeatingMode.NORMAL)
        status_quo = ([normal] * n, [hold] * n)
        pv_led: tuple[list[int], list[int]] = ([], [])
        price_led: tuple[list[int], list[int]] = ([], [])
        combined: tuple[list[int], list[int]] = ([], [])
        for p in range(n):
            heat_pv, water_pv = pv_modes(p)
            heat_price, water_price = price_modes(p)
            pv_led[0].append(self.heating_index(heat_pv if heat_pv is not None else 1))
            pv_led[1].append(water_pv if water_pv is not None else hold)
            price_led[0].append(self.heating_index(heat_price))
            price_led[1].append(water_price)
            combined[0].append(self.heating_index(heat_pv if heat_pv is not None else heat_price))
            combined[1].append(water_pv if water_pv is not None else water_price)

        unique: dict[tuple[int, ...], list[int]] = {}
        for heat, water in (status_quo, pv_led, price_led, combined):
            heat, water = list(heat), list(water)
            self._place_legionella(heat, water, surplus, price)
            genes = heat + water
            unique.setdefault(tuple(genes), genes)
        return list(unique.values())

    def _place_legionella(
        self, heat: list[int], water: list[int], surplus: list[float], price: list[float]
    ) -> None:
        """Plan a due legionella heat-up on the two best periods before the deadline.

        Uses the lowest rod target that reaches the temperature; without one,
        a heating mode whose hot water setpoint reaches it (SG Ready raise) with
        an active hot water charge. ``heat`` and ``water`` hold gene values.
        """
        deadline = self.legionella_deadline_slot
        if deadline is None:
            return
        rod = self.rod_index_reaching(self.legionella_c)
        mode = None if rod is not None else self.heating_mode_reaching_dhw(self.legionella_c)
        if rod is None and mode is None:
            return
        last_period = max(0, min(self.n_periods - 1, (deadline - 1) // self.slots_per_gene))
        if rod is not None and any(water[p] == rod for p in range(last_period + 1)):
            return
        periods = [0] if last_period < 1 else None
        if periods is None:
            best = max(range(last_period), key=lambda p: (surplus[p] + surplus[p + 1], -price[p]))
            periods = [best, best + 1]
        for p in periods:
            if rod is not None:
                water[p] = rod
            elif mode is not None:
                heat[p] = self.heating_index(mode)
                water[p] = self.dhw_index(DhwMode.HEAT_PUMP)

    def mutate(self, genes: list[int], rng: random.Random | Any = random) -> bool:
        """Change a single period or a block of periods in one dimension."""
        n = self.n_periods
        dimension = rng.randrange(2)
        upper = (len(self.heating_modes) if dimension == 0 else len(self.dhw_options)) - 1
        if upper <= 0:
            dimension = 1 - dimension
            upper = (len(self.heating_modes) if dimension == 0 else len(self.dhw_options)) - 1
            if upper <= 0:
                return False
        offset = dimension * n
        start = rng.randrange(n)
        length = 1 if rng.random() < 0.5 else rng.randint(2, 4)
        value = rng.randint(0, upper)
        changed = False
        for p in range(start, min(n, start + length)):
            if genes[offset + p] != value:
                genes[offset + p] = value
                changed = True
        if not changed:
            old = genes[offset + start]
            genes[offset + start] = (old + 1 + rng.randrange(upper)) % (upper + 1)
            changed = True
        return changed

    def shift(self, genes: list[int], elapsed_periods: int) -> list[int]:
        """Drop elapsed periods of a warm start and repeat the last one."""
        if elapsed_periods <= 0:
            return list(genes)
        n = self.n_periods
        shifted: list[int] = []
        for block in (genes[:n], genes[n : 2 * n]):
            kept = list(block[elapsed_periods:])
            shifted.extend(kept + [block[-1]] * (n - len(kept)))
        return shifted

    def valid(self, genes: Sequence[Any]) -> bool:
        """Every gene is an index into its allowed modes."""
        if len(genes) != self.n_genes:
            return False
        return all(0 <= int(g) <= u and int(g) == g for g, u in zip(genes, self.upper_bounds()))


class HeatPumpGeneLayout:
    """Gene block of all heat pumps (concatenated in configuration order)."""

    def __init__(self, plans: Optional[list[HeatPumpRunPlan]] = None) -> None:
        self.plans = list(plans or [])
        self.offsets: list[int] = []
        offset = 0
        for plan in self.plans:
            self.offsets.append(offset)
            offset += plan.n_genes
        self.n_genes = offset

    def __bool__(self) -> bool:
        return self.n_genes > 0

    def split(self, genes: Sequence[int]) -> list[Sequence[int]]:
        return [genes[o : o + plan.n_genes] for plan, o in zip(self.plans, self.offsets)]

    def default_genes(self) -> list[int]:
        genes: list[int] = []
        for plan in self.plans:
            genes.extend(plan.default_genes())
        return genes

    def random_genes(self, rng: random.Random | Any = random) -> list[int]:
        genes: list[int] = []
        for plan in self.plans:
            genes.extend(rng.randint(0, upper) for upper in plan.upper_bounds())
        return genes

    def valid(self, genes: Sequence[Any]) -> bool:
        if len(genes) != self.n_genes:
            return False
        return all(plan.valid(part) for plan, part in zip(self.plans, self.split(genes)))

    def evaluate(self, genes: Sequence[int]) -> tuple[Optional[list[float]], float]:
        """Total electric load per slot and extra cost of all heat pumps."""
        if not self.plans:
            return None, 0.0
        total: Optional[list[float]] = None
        extra = 0.0
        for plan, part in zip(self.plans, self.split(genes)):
            outcome = plan.evaluate(part)
            extra += outcome.extra_cost
            if plan.separate_meter_price_per_wh is not None:
                # Own meter: not part of the house energy balance.
                if total is None:
                    total = [0.0] * len(outcome.electric_wh)
                continue
            if total is None:
                total = list(outcome.electric_wh)
            else:
                total = [a + b for a, b in zip(total, outcome.electric_wh)]
        return total, extra

    def heuristic_gene_sets(self, pv_wh: Any, load_wh: Any, price_per_wh: Any) -> list[list[int]]:
        """Combined heuristic seeds (the i-th heuristic of every heat pump)."""
        per_plan = [plan.heuristic_genes(pv_wh, load_wh, price_per_wh) for plan in self.plans]
        if not per_plan:
            return []
        count = max(len(sets) for sets in per_plan)
        combined = []
        for i in range(count):
            genes: list[int] = []
            for sets in per_plan:
                genes.extend(sets[min(i, len(sets) - 1)])
            combined.append(genes)
        return combined

    def mutate(self, genes: list[int], rng: random.Random | Any = random) -> bool:
        if not self.plans:
            return False
        index = rng.randrange(len(self.plans))
        plan, offset = self.plans[index], self.offsets[index]
        part = list(genes[offset : offset + plan.n_genes])
        changed = plan.mutate(part, rng)
        genes[offset : offset + plan.n_genes] = part
        return changed

    def shift(self, genes: list[int], elapsed_slots: int) -> list[int]:
        shifted: list[int] = []
        for plan, part in zip(self.plans, self.split(genes)):
            shifted.extend(plan.shift(list(part), elapsed_slots // plan.slots_per_gene))
        return shifted


# ----------------------------------------------------------------------
# Building the run plan
# ----------------------------------------------------------------------


def build_run_plan(
    parameters: GeneticHeatPumpParameters,
    slot_starts: list[Any],
    slot_hours: float,
    outdoor_c: Sequence[Optional[float]],
    ghi_w_per_m2: Optional[Sequence[Optional[float]]],
    price_per_wh: Sequence[float],
) -> HeatPumpRunPlan:
    """Prepare one heat pump for a run over ``slot_starts`` (the control slots).

    Args:
        parameters: Device configuration and measured state.
        slot_starts: Start datetime of every control slot, from now.
        slot_hours: Slot length [h].
        outdoor_c: Outdoor temperature per slot; missing values are carried
            forward, a completely missing forecast falls back to 10 degC.
        ghi_w_per_m2: Global horizontal irradiance per slot, optional.
        price_per_wh: Import price per slot, for the value of stored heat.
    """
    settings = parameters.settings
    system = settings.to_system(slot_hours, dhw_setpoint_c=parameters.dhw_setpoint_c)
    known = [t for t in outdoor_c[: len(slot_starts)] if t is not None]
    fallback = known[0] if known else 10.0
    if not known:
        logger.warning(
            "Heat pump {}: no outdoor temperature forecast, assuming {} degC.",
            parameters.device_id,
            fallback,
        )
    inputs = settings.slot_inputs(
        slot_starts,
        slot_hours,
        list(outdoor_c[: len(slot_starts)]),
        list(ghi_w_per_m2[: len(slot_starts)]) if ghi_w_per_m2 is not None else None,
        fallback_outdoor_c=fallback,
    )
    if not inputs:
        raise ValueError("Heat pump run plan needs at least one slot.")

    # Active §14a dimming: limit the first slots.
    if parameters.power_limited_w is not None:
        limited_slots = max(1, math.ceil(parameters.power_limited_hours / slot_hours))
        for slot_input in inputs[:limited_slots]:
            current = slot_input.max_electric_w
            slot_input.max_electric_w = (
                parameters.power_limited_w
                if current is None
                else min(current, parameters.power_limited_w)
            )

    # §14a module 2: own meter with reduced price.
    separate_prices: Optional[list[float]] = None
    if parameters.separate_meter_price_reduction_amt_wh is not None:
        reduction = parameters.separate_meter_price_reduction_amt_wh
        separate_prices = [
            max(0.0, float(price_per_wh[i]) - reduction) if i < len(price_per_wh) else 0.0
            for i in range(len(inputs))
        ]

    # Initial state: measurements, otherwise plausible defaults.
    first = inputs[0]
    room = (
        parameters.initial_room_c
        if parameters.initial_room_c is not None
        else settings.comfort.setpoint_c
    )
    gains = system.building.gains_w(first.ghi_w_per_m2)
    slab = (
        parameters.initial_slab_c
        if parameters.initial_slab_c is not None
        else system.building.steady_slab_c(room, first.outdoor_c, gains)
    )
    dhw = (
        parameters.initial_dhw_c
        if parameters.initial_dhw_c is not None
        else system.dhw.setpoint_c - system.dhw.hysteresis_k / 2.0
    )
    initial = ThermalState(slab_c=slab, room_c=room, dhw_c=dhw)

    # Allowed modes; one rod option per configured target, only with a rod.
    opt = settings.optimization
    heating_modes = tuple(opt.heating_modes)
    options: list[DhwOption] = []
    for mode in opt.dhw_modes:
        if mode == DhwMode.ROD:
            if settings.dhw.rod_power_w > 0:
                options.extend(
                    DhwOption(DhwMode.ROD, target) for target in settings.dhw.rod_targets_c
                )
        else:
            options.append(DhwOption(DhwMode(mode)))
    dhw_options = tuple(options) or (DhwOption(DhwMode.HOLD),)

    # Value of stored heat: average import price over the horizon divided by
    # the COP the heat pump would need to deliver the heat later.
    price_source = separate_prices if separate_prices is not None else price_per_wh[: len(inputs)]
    prices = [p for p in price_source if p is not None and math.isfinite(p)]
    mean_price = sum(prices) / len(prices) if prices else 0.0
    mean_outdoor = sum(i.outdoor_c for i in inputs) / len(inputs)
    gains_mean = system.building.gains_w(0.0)
    steady = system.building.steady_heat_w(settings.comfort.setpoint_c, mean_outdoor, gains_mean)
    slab_ref = system.building.steady_slab_c(settings.comfort.setpoint_c, mean_outdoor, gains_mean)
    flow_ref = system.building.flow_temperature_c(slab_ref, steady, system.hp.flow_spread_k)
    cop_space = system.cop.cop(mean_outdoor, flow_ref)
    cop_dhw = system.cop.cop(mean_outdoor, system.dhw.setpoint_c + system.hp.dhw_flow_overshoot_k)

    # Legionella requirement, only if some allowed option reaches the
    # temperature: a rod target or a raised/forced heat pump setpoint.
    deadline_slot: Optional[int] = None
    interval_days = settings.dhw.legionella_interval_days
    legionella_c = settings.dhw.legionella_c
    reachable = any(
        o.mode == DhwMode.ROD and (o.rod_target_c or 0.0) >= legionella_c - 0.5 for o in dhw_options
    ) or (
        any(o.mode == DhwMode.HEAT_PUMP for o in dhw_options)
        and system.reachable_dhw_c(heating_modes) >= legionella_c - 0.5
    )
    if interval_days is not None and not reachable:
        logger.info(
            "Heat pump {}: no allowed mode reaches {} degC; legionella heat-up not planned.",
            parameters.device_id,
            legionella_c,
        )
    if interval_days is not None and reachable:
        since = parameters.hours_since_legionella
        if since is None:
            logger.info(
                "Heat pump {}: last legionella heat-up unknown; none required in this run.",
                parameters.device_id,
            )
        else:
            remaining_h = interval_days * 24.0 - since
            slot = max(1, int(remaining_h / slot_hours)) if remaining_h > 0 else len(inputs)
            if slot <= len(inputs):
                deadline_slot = slot

    return HeatPumpRunPlan(
        device_id=parameters.device_id,
        system=system,
        inputs=inputs,
        initial=initial,
        slots_per_gene=max(1, round(1.0 / slot_hours)),
        heating_modes=heating_modes,
        dhw_options=dhw_options,
        comfort_penalty=opt.comfort_penalty_amt_per_kh,
        dhw_penalty=opt.dhw_penalty_amt_per_kh,
        legionella_penalty=opt.legionella_penalty_amt,
        mode_change_penalty=opt.mode_change_penalty_amt,
        building_heat_value=mean_price / cop_space if cop_space > 0 else 0.0,
        tank_heat_value=mean_price / cop_dhw if cop_dhw > 0 else 0.0,
        legionella_c=settings.dhw.legionella_c,
        legionella_deadline_slot=deadline_slot,
        load_includes_heat_pump=opt.load_forecast_includes_heat_pump and separate_prices is None,
        separate_meter_price_per_wh=separate_prices,
        sg_ready_states=dict(opt.sg_ready_states),
    )


def plan_result(plan: HeatPumpRunPlan, genes: Sequence[int]) -> dict[str, Any]:
    """Per-slot schedule and states of the chosen genes, for the solution."""
    outcome = plan.evaluate(genes, detail=True)
    detail = outcome.detail or {}
    heating_names = detail.get("heating_mode", [])
    dhw_names = detail.get("dhw_mode", [])
    states = {**DEFAULT_SG_READY_STATES, **plan.sg_ready_states}
    result = dict(detail)
    result["sg_ready_state"] = [states[name] for name in heating_names]
    result["valves_forced"] = [HeatingMode[name] == HeatingMode.PREHEAT for name in heating_names]
    result["dhw_push"] = [name == DhwMode.HEAT_PUMP.name for name in dhw_names]
    result["summary"] = {
        "extra_cost_amt": float(outcome.extra_cost),
        "comfort_violation_kh": float(outcome.comfort_kh),
        "dhw_violation_kh": float(outcome.dhw_kh),
        "legionella_ok": outcome.legionella_ok,
        "legionella_deadline_slot": plan.legionella_deadline_slot,
        "mode_changes": int(outcome.mode_changes),
        "stored_heat_credit_amt": float(outcome.stored_heat_credit),
        "separate_meter": plan.separate_meter_price_per_wh is not None,
        "separate_meter_cost_amt": float(outcome.separate_meter_cost),
        "electric_wh": float(sum(outcome.electric_wh)),
    }
    for key, values in list(result.items()):
        if isinstance(values, list):
            result[key] = [float(v) if isinstance(v, float) else v for v in values]
    return result
