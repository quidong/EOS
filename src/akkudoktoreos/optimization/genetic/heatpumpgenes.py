"""Heat pump genes for the GENETIC optimizer (RL29 fork).

The heat pump is optimized jointly with battery, EV and appliances: every
configured heat pump adds two blocks of genes to the genome, one space heating
mode and one hot water mode per gene period (one hour). The genome stays
compact (48 h horizon: 96 genes per heat pump) and the modes change rarely.

EOS only decides *what* the heat pump should do. The solution carries the
modes per slot (heating mode, hot water mode, rod target temperature). The
controller (e.g. Home Assistant) maps them to signals - which SG Ready state,
which valves, which hot water command - and decides how they reach the heat
pump (relay contacts, Modbus, KNX, a vendor API).

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
from akkudoktoreos.utils.datetimeutil import DateTime


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
    cooling_season: bool = Field(
        default=False,
        json_schema_extra={
            "description": (
                "RL29 fork: the system is in cooling operation; the optimizer chooses "
                "between NORMAL (no active cooling) and COOL instead of heating modes."
            )
        },
    )
    dewpoint_c: Optional[float] = Field(
        default=None,
        json_schema_extra={"description": "Measured room dew point [degC]."},
    )
    away: bool = Field(
        default=False,
        json_schema_extra={
            "description": "RL29 fork: holiday mode is active (reduced comfort until away_until)."
        },
    )
    away_until: Optional[DateTime] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Return from holiday; slots before it use the away settings. None with away = "
                "the whole horizon is away (return unknown)."
            )
        },
    )
    away_room_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": "Room temperature held while away [degC]; None = away.room_c."
        },
    )
    away_min_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "RL29 fork: room floor while away with the heat pump in standby [degC]; "
                "None = away.standby_min_c."
            )
        },
    )
    setback_room_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "RL29 fork: temperature of the set-back zone [degC]; None = zone_setback.room_c."
            )
        },
    )
    window_open_share: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
        json_schema_extra={
            "description": "RL29 fork: share of the monitored windows open now (0..1)."
        },
    )
    room_min_margin_k: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Fork: smallest margin of a room above its own lower limit now [K]; "
                "None = not reported."
            )
        },
    )
    room_max_margin_k: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Fork: smallest margin of a room below its own upper limit now [K]; "
                "None = not reported."
            )
        },
    )
    humidity_pct: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        json_schema_extra={
            "description": "Fork: highest relative humidity of the monitored rooms now [%]."
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
    overheat_kh: float = 0.0
    """Room above the comfort maximum, integrated [K*h]."""
    active_hours: float = 0.0
    """Hours with an active mode (heating above NORMAL, hot water not HOLD) [h]."""
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
    """Value of tank heat [currency/Wh] up to the heat pump setpoint."""
    legionella_c: float = 60.0
    legionella_deadline_slot: Optional[int] = None
    """Slot by which the tank has to reach legionella_c; None = no requirement."""
    load_includes_heat_pump: bool = False
    separate_meter_price_per_wh: Optional[list[float]] = None
    """§14a module 2: own meter, price per slot; its energy is not part of the house balance."""
    overheat_penalty: float = 0.0
    """Cost of a room above its comfort maximum [currency per K*h]."""
    active_mode_penalty: float = 0.0
    """Cost per hour of an active mode; prefers NORMAL/HOLD where a mode changes nothing."""
    away_slots: int = 0
    """Holiday mode: number of slots before the return (0 = at home)."""
    zone_setback_slots: tuple[bool, ...] = ()
    """Zone setback active per slot (RL29 fork)."""
    ventilate_slots: tuple[bool, ...] = ()
    """Recommended airing per slot (RL29 fork)."""
    tank_heat_value_above_setpoint: Optional[float] = None
    """Value of tank heat above the heat pump setpoint [currency/Wh]; None = tank_heat_value.
    Water the rod heated beyond the setpoint loses more and is valued with a discount."""
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
        overheat_kh = 0.0
        active_hours = 0.0
        dt = system.dt
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
                "comfort_min_c",
                "comfort_max_c",
                "outdoor_c",
                "away",
                "zone_setback",
                "ventilate",
                "ventilation_w_per_k",
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
            overheat_kh += result.overheat_kh
            if heating[period] > HeatingMode.NORMAL:
                active_hours += dt
            if option.mode != DhwMode.HOLD:
                active_hours += dt
            dhw_kh += result.dhw_violation_kh
            if slot == self.away_slots - 1 and self.away_slots < len(self.inputs):
                # Holiday mode: hot water has to be ready at the return, not only after
                # the heat pump's own reheat in the first hour at home.
                shortfall = max(0.0, system.dhw.min_c - state.dhw_c)
                dhw_kh += shortfall * RETURN_READY_HOURS
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
                low, _, high = self.system.comfort_band(inputs)
                rows["comfort_min_c"].append(low)
                rows["comfort_max_c"].append(high)
                rows["outdoor_c"].append(inputs.outdoor_c)
                rows["away"].append(1.0 if slot < self.away_slots else 0.0)
                zone = self.zone_setback_slots
                vent = self.ventilate_slots
                rows["zone_setback"].append(1.0 if slot < len(zone) and zone[slot] else 0.0)
                rows["ventilate"].append(1.0 if slot < len(vent) and vent[slot] else 0.0)
                rows["ventilation_w_per_k"].append(float(inputs.ventilation_w_per_k))

        # Mode changes between consecutive gene periods.
        n = self.n_periods
        changes = sum(1 for i in range(1, n) if heating[i] != heating[i - 1]) + sum(
            1 for i in range(1, n) if dhw[i] != dhw[i - 1]
        )

        # Value of the heat stored (or used up) compared to the start. Room heat
        # above the comfort maximum has no value: the room controllers do not use it.
        room_cap = system.comfort_band(self.inputs[-1])[2] if self.inputs else state.room_c
        building_start = system.building.stored_heat_wh(
            self.initial.slab_c, min(self.initial.room_c, room_cap), 0.0
        )
        building_end = system.building.stored_heat_wh(
            state.slab_c, min(state.room_c, room_cap), 0.0
        )
        # Tank heat up to the heat pump setpoint displaces a heat pump charge;
        # heat above it (heating rod) counts with its own, discounted value.
        setpoint = system.dhw.setpoint_c
        above_value = (
            self.tank_heat_value_above_setpoint
            if self.tank_heat_value_above_setpoint is not None
            else self.tank_heat_value
        )

        def tank_value(dhw_c: float) -> float:
            below = system.tank.stored_heat_wh(min(dhw_c, setpoint), 0.0)
            above = system.tank.stored_heat_wh(max(dhw_c, setpoint), setpoint)
            return below * self.tank_heat_value + above * above_value

        credit = (building_end - building_start) * self.building_heat_value + (
            tank_value(state.dhw_c) - tank_value(self.initial.dhw_c)
        )

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
            + overheat_kh * self.overheat_penalty
            + active_hours * self.active_mode_penalty
            + dhw_kh * self.dhw_penalty
            + legionella_cost
            + changes * self.mode_change_penalty
            - credit
        )
        return HeatPumpOutcome(
            electric_wh=electric,
            extra_cost=extra,
            comfort_kh=comfort_kh,
            overheat_kh=overheat_kh,
            active_hours=active_hours,
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
                heat = strongest(HeatingMode.COOL, HeatingMode.PREHEAT, HeatingMode.RAISED)
            elif surplus[p] > 1000:
                heat = strongest(HeatingMode.COOL, HeatingMode.RAISED)
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


RETURN_READY_HOURS = 2.0
"""Holiday mode: a tank below its minimum at the return counts as this many hours short."""

TANK_DWELL_HOURS = 12.0
"""Typical time stored hot water waits until it is tapped; sets the loss discount
on tank heat above the heat pump setpoint at the end of the horizon."""


def terminal_price_per_wh(
    prices: Sequence[Optional[float]],
    quantile: float,
    pv_surplus_w: Optional[Sequence[float]] = None,
    feed_in_per_wh: Optional[Sequence[Optional[float]]] = None,
    surplus_needed_w: float = 0.0,
) -> float:
    """Marginal price the stored heat is valued at [currency/Wh].

    Heat left in the building or the tank at the end of the horizon displaces
    heat the optimizer would produce in the cheapest hours, so it is worth the
    mean of the cheapest ``quantile`` share of the horizon's marginal prices.
    Hours whose PV surplus covers the heat pump (``surplus_needed_w``) count with
    the feed-in tariff: there the heat pump would run on surplus anyway.
    """
    marginal: list[float] = []
    for i, price in enumerate(prices):
        if price is None or not math.isfinite(price):
            continue
        value = float(price)
        if (
            pv_surplus_w is not None
            and feed_in_per_wh is not None
            and i < len(pv_surplus_w)
            and i < len(feed_in_per_wh)
            and feed_in_per_wh[i] is not None
            and math.isfinite(float(feed_in_per_wh[i]))  # type: ignore[arg-type]
            and float(pv_surplus_w[i]) >= surplus_needed_w
        ):
            value = min(value, float(feed_in_per_wh[i]))  # type: ignore[arg-type]
        marginal.append(value)
    if not marginal:
        return 0.0
    marginal.sort()
    count = max(1, math.ceil(len(marginal) * min(1.0, max(0.0, quantile))))
    cheapest = marginal[:count]
    return sum(cheapest) / len(cheapest)


def build_run_plan(
    parameters: GeneticHeatPumpParameters,
    slot_starts: list[Any],
    slot_hours: float,
    outdoor_c: Sequence[Optional[float]],
    ghi_w_per_m2: Optional[Sequence[Optional[float]]],
    price_per_wh: Sequence[float],
    pv_surplus_w: Optional[Sequence[float]] = None,
    feed_in_per_wh: Optional[Sequence[Optional[float]]] = None,
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
        pv_surplus_w: Forecast PV surplus (PV minus load) per slot [W]; in the
            cooling season with ``cooling.pv_only`` it is the cooling budget and
            otherwise marks the hours whose heat would come from surplus.
        feed_in_per_wh: Feed-in tariff per slot, the marginal price in surplus hours.
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

    # Cooling season: cooling target and band, dew point, PV-only budget.
    cooling = settings.cooling
    cooling_season = bool(parameters.cooling_season and cooling.enabled)
    if cooling_season:
        for i, slot_input in enumerate(inputs):
            slot_input.cooling_season = True
            slot_input.setpoint_c = cooling.setpoint_c
            slot_input.comfort_max_c = cooling.max_c
            slot_input.dewpoint_c = parameters.dewpoint_c
            if cooling.pv_only:
                surplus = (
                    float(pv_surplus_w[i])
                    if pv_surplus_w is not None and i < len(pv_surplus_w)
                    else 0.0
                )
                slot_input.cooling_budget_w = (
                    surplus if surplus >= cooling.min_pv_surplus_w else 0.0
                )

    # Holiday mode (RL29 fork): reduced comfort and hot water off until the return.
    away_slots = 0
    if parameters.away:
        away = settings.away
        room_away = parameters.away_room_c if parameters.away_room_c is not None else away.room_c
        floor = parameters.away_min_c if parameters.away_min_c is not None else away.standby_min_c
        room_floor = min(floor, room_away) if floor is not None else room_away
        until = parameters.away_until
        for i, (start, slot_input) in enumerate(zip(slot_starts, inputs)):
            if until is not None and start.timestamp() >= until.timestamp():
                break
            away_slots = i + 1
            if slot_input.cooling_season:
                slot_input.setpoint_c = away.cooling_max_c
                slot_input.comfort_max_c = max(away.cooling_max_c, slot_input.comfort_max_c or 0.0)
            else:
                normal_setpoint = slot_input.setpoint_c
                # REDUCED (heat pump in standby) may let the house cool to the floor
                slot_input.comfort_min_c = min(slot_input.comfort_min_c, room_floor)
                slot_input.setpoint_c = room_away
                slot_input.raised_c = normal_setpoint  # RAISED heats back to normal
                # comfort_max_c stays: PREHEAT/FORCED heat up towards the return
            slot_input.draw_wh = 0.0  # nobody taps hot water while away
            slot_input.internal_gains_factor = away.internal_gains_share
            if away.dhw_off:
                slot_input.dhw_off = True
                if floor is not None and not slot_input.cooling_season:
                    # The controller's standby for REDUCED; a restart may charge hot water.
                    slot_input.standby_possible = True
                    slot_input.standby_exit_dhw_c = away.standby_exit_dhw_c
        logger.info(
            "Heat pump {}: holiday mode, {} of {} slots away (return {}), room {} degC, hot water {}.",
            parameters.device_id,
            away_slots,
            len(inputs),
            until.isoformat() if until is not None else "unknown",
            room_away,
            "off" if away.dhw_off else "on",
        )

    zone_slots, vent_slots = _apply_zone_setback_and_ventilation(
        parameters, slot_starts, slot_hours, inputs, away_slots, pv_surplus_w
    )
    _apply_room_limits_and_humidity(parameters, slot_hours, inputs, away_slots)

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
    gains = system.slot_gains_w(first)
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
    # In standby at the start when the controller's rule holds (RL29 fork): the
    # restart charge of the next non-REDUCED slot is then part of the plan.
    standby0 = bool(first.standby_possible and room > first.comfort_min_c + 0.3)
    initial = ThermalState(slab_c=slab, room_c=room, dhw_c=dhw, standby=standby0)

    # Allowed modes; one rod option per configured target, only with a rod.
    opt = settings.optimization
    heating_modes = (
        (int(HeatingMode.NORMAL), int(HeatingMode.COOL))
        if cooling_season
        else tuple(opt.heating_modes)
    )
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

    # Value of stored heat: the marginal price of the cheapest hours (surplus
    # hours at the feed-in tariff) divided by the COP the heat pump would need
    # to deliver the heat later.
    mean_outdoor = sum(i.outdoor_c for i in inputs) / len(inputs)
    gains_mean = system.building.gains_w(0.0)
    steady = system.building.steady_heat_w(settings.comfort.setpoint_c, mean_outdoor, gains_mean)
    slab_ref = system.building.steady_slab_c(settings.comfort.setpoint_c, mean_outdoor, gains_mean)
    flow_ref = system.building.flow_temperature_c(slab_ref, steady, system.hp.flow_spread_k)
    cop_space = system.cop.cop(mean_outdoor, flow_ref)
    cop_dhw = system.cop.cop(mean_outdoor, system.dhw.setpoint_c + system.hp.dhw_flow_overshoot_k)
    price_source = separate_prices if separate_prices is not None else price_per_wh[: len(inputs)]
    mean_price = terminal_price_per_wh(
        list(price_source),
        opt.stored_heat_price_quantile,
        # Module 2 (own meter) cannot use PV surplus.
        pv_surplus_w if separate_prices is None else None,
        feed_in_per_wh,
        surplus_needed_w=(steady / cop_space if cop_space > 0 else 0.0) + system.hp.standby_w,
    )
    # Hot water above the heat pump setpoint loses more until it is tapped.
    tank_params = system.tank.params
    tank_capacity = system.tank.capacity
    retention = (
        math.exp(-tank_params.loss_w_per_k * TANK_DWELL_HOURS / tank_capacity)
        if tank_capacity > 0
        else 1.0
    )

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

    tank_value = mean_price / cop_dhw if cop_dhw > 0 else 0.0
    tank_value_above = tank_value * retention
    if inputs and inputs[-1].dhw_off:
        # Still away with hot water off at the horizon end (RL29 fork): heat put into
        # the tank now is lost long before anybody taps it, and the heat pump charges
        # the tank at the restart anyway - it has no value at the end.
        tank_value = tank_value_above = 0.0

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
        tank_heat_value=tank_value,
        tank_heat_value_above_setpoint=tank_value_above,
        legionella_c=settings.dhw.legionella_c,
        legionella_deadline_slot=deadline_slot,
        load_includes_heat_pump=opt.load_forecast_includes_heat_pump and separate_prices is None,
        separate_meter_price_per_wh=separate_prices,
        overheat_penalty=opt.overheat_penalty_amt_per_kh,
        active_mode_penalty=opt.active_mode_penalty_amt_per_h,
        away_slots=away_slots,
        zone_setback_slots=tuple(zone_slots),
        ventilate_slots=tuple(vent_slots),
    )


def _apply_zone_setback_and_ventilation(
    parameters: GeneticHeatPumpParameters,
    slot_starts: list[Any],
    slot_hours: float,
    inputs: list[SlotInputs],
    away_slots: int,
    pv_surplus_w: Optional[Sequence[float]],
) -> tuple[list[bool], list[bool]]:
    """Zone setback and window ventilation of the slots (RL29 fork).

    Zone setback: the zone's share of the difference to its setback temperature
    comes off the comfort minimum and the NORMAL setpoint (the measured room
    temperature is the mean over all rooms). Not while away (all rooms are
    reduced then) and not in the cooling season.

    Ventilation: the slot inputs already carry the expected airing of the
    profile; windows open now count in the first slot, and with ``recommend``
    one slot per day in the midday window gets the recommended airing.
    """
    settings = parameters.settings
    n = len(inputs)
    zone = settings.zone_setback
    zone_slots = [False] * n
    if zone.enabled:
        zone_c = parameters.setback_room_c if parameters.setback_room_c is not None else zone.room_c
        for i, (start, slot_input) in enumerate(zip(slot_starts, inputs)):
            if i < away_slots or slot_input.cooling_season or not zone.active_at(start):
                continue
            zone_slots[i] = True
            # The controller keeps the zone at zone_c in every mode; the other rooms
            # follow the mode, so every target of the mean drops by the zone's share.
            setpoint = slot_input.setpoint_c
            if setpoint is not None and zone_c < setpoint:
                raised = (
                    slot_input.raised_c
                    if slot_input.raised_c is not None
                    else setpoint + settings.comfort.raised_offset_k
                )
                slot_input.setpoint_c = setpoint - zone.share * (setpoint - zone_c)
                slot_input.raised_c = raised - zone.share * max(0.0, raised - zone_c)
                high = slot_input.comfort_max_c
                if high is not None:
                    slot_input.comfort_max_c = max(
                        slot_input.raised_c, high - zone.share * max(0.0, high - zone_c)
                    )
            zone_min = zone_c - zone.min_offset_k
            if zone_min < slot_input.comfort_min_c:
                slot_input.comfort_min_c -= zone.share * (slot_input.comfort_min_c - zone_min)
        if any(zone_slots):
            logger.info(
                "Heat pump {}: zone setback to {} degC in {} of {} slots (share {}).",
                parameters.device_id,
                zone_c,
                sum(zone_slots),
                n,
                zone.share,
            )

    vent = settings.ventilation
    vent_slots = [False] * n
    if not vent.enabled:
        return zone_slots, vent_slots
    share_now = parameters.window_open_share
    if share_now is not None and share_now > 0 and n:
        # Open now: assume they close within the hour.
        open_hours = min(slot_hours, 0.5) * share_now
        inputs[0].ventilation_w_per_k = max(
            inputs[0].ventilation_w_per_k, vent.loss_w_per_k * open_hours / slot_hours
        )
    if vent.recommend:
        by_day: dict[Any, list[int]] = {}
        for i, start in enumerate(slot_starts):
            if i < away_slots or inputs[i].cooling_season:
                continue
            if vent.recommend_start_hour <= start.hour < vent.recommend_end_hour:
                by_day.setdefault(start.date(), []).append(i)
        for candidates in by_day.values():

            def score(i: int) -> tuple[float, float]:
                surplus = (
                    float(pv_surplus_w[i])
                    if pv_surplus_w is not None and i < len(pv_surplus_w)
                    else 0.0
                )
                return (inputs[i].outdoor_c, surplus)

            best = max(candidates, key=score)
            vent_slots[best] = True
            # plan the recommended airing on top of what the profile expects in the window
            expected = sum(vent.open_hours(slot_starts[i]) * slot_hours for i in candidates)
            extra = max(0.0, vent.recommend_open_hours - expected)
            inputs[best].ventilation_w_per_k += vent.loss_w_per_k * extra / slot_hours
    return zone_slots, vent_slots


def _apply_room_limits_and_humidity(
    parameters: GeneticHeatPumpParameters,
    slot_hours: float,
    inputs: list[SlotInputs],
    away_slots: int,
) -> None:
    """Per-room margins and humidity protection reported by the controller (fork).

    Both only apply to heating slots near now: the rooms are assumed to move with
    the mean for ``room_limits.horizon_hours``, a high humidity reading holds for
    ``humidity.hold_hours``. Room margins also stop at the end of a holiday, because
    the controller reports them against the limits valid now.
    """
    settings = parameters.settings
    room_now = parameters.initial_room_c
    limits = settings.room_limits
    if limits.enabled and room_now is not None:
        count = min(len(inputs), max(1, math.ceil(limits.horizon_hours / slot_hours)))
        if away_slots > 0:
            count = min(count, away_slots)
        changed = 0
        for slot_input in inputs[:count]:
            if slot_input.cooling_season or slot_input.setpoint_c is None:
                continue
            setpoint = slot_input.setpoint_c
            if parameters.room_min_margin_k is not None:
                floor = room_now - parameters.room_min_margin_k
                floor = min(floor, slot_input.comfort_min_c + limits.max_raise_k, setpoint)
                if floor > slot_input.comfort_min_c:
                    slot_input.comfort_min_c = floor
                    changed += 1
            if parameters.room_max_margin_k is not None:
                ceiling = max(room_now + parameters.room_max_margin_k, setpoint)
                if slot_input.comfort_max_c is not None and ceiling < slot_input.comfort_max_c:
                    slot_input.comfort_max_c = ceiling
                    changed += 1
                raised = (
                    slot_input.raised_c
                    if slot_input.raised_c is not None
                    else setpoint + settings.comfort.raised_offset_k
                )
                if ceiling < raised:
                    slot_input.raised_c = ceiling
        if changed:
            logger.info(
                "Heat pump {}: room margins min {} K / max {} K applied to {} slots.",
                parameters.device_id,
                parameters.room_min_margin_k,
                parameters.room_max_margin_k,
                count,
            )

    humidity = settings.humidity
    value = parameters.humidity_pct
    if humidity.enabled and value is not None and value >= humidity.max_pct:
        count = min(len(inputs), max(1, math.ceil(humidity.hold_hours / slot_hours)))
        for slot_input in inputs[:count]:
            if slot_input.cooling_season or slot_input.setpoint_c is None:
                continue
            hold = slot_input.setpoint_c - humidity.max_drop_k
            if hold > slot_input.comfort_min_c:
                slot_input.comfort_min_c = hold
        logger.info(
            "Heat pump {}: humidity {} % >= {} %, rooms held warm for {} slots.",
            parameters.device_id,
            value,
            humidity.max_pct,
            count,
        )


def plan_result(plan: HeatPumpRunPlan, genes: Sequence[int]) -> dict[str, Any]:
    """Per-slot schedule and states of the chosen genes, for the solution."""
    outcome = plan.evaluate(genes, detail=True)
    detail = outcome.detail or {}
    result = dict(detail)
    result["summary"] = {
        "extra_cost_amt": float(outcome.extra_cost),
        "comfort_violation_kh": float(outcome.comfort_kh),
        "overheat_kh": float(outcome.overheat_kh),
        "active_hours": float(outcome.active_hours),
        "dhw_violation_kh": float(outcome.dhw_kh),
        "legionella_ok": outcome.legionella_ok,
        "legionella_deadline_slot": plan.legionella_deadline_slot,
        "mode_changes": int(outcome.mode_changes),
        "stored_heat_credit_amt": float(outcome.stored_heat_credit),
        "separate_meter": plan.separate_meter_price_per_wh is not None,
        "separate_meter_cost_amt": float(outcome.separate_meter_cost),
        "electric_wh": float(sum(outcome.electric_wh)),
        "away_slots": int(plan.away_slots),
    }
    for key, values in list(result.items()):
        if isinstance(values, list):
            result[key] = [float(v) if isinstance(v, float) else v for v in values]
    return result
