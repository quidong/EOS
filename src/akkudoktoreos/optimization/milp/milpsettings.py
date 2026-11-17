"""Settings of the MILP optimization (RL29 fork); kept light for the config import."""

from typing import Literal, Optional

from pydantic import Field

from akkudoktoreos.config.configabc import SettingsBaseModel


class MilpCommonSettings(SettingsBaseModel):
    """MILP optimization settings (RL29 fork)."""

    time_limit_s: float = Field(
        default=10.0,
        gt=0,
        le=300,
        json_schema_extra={
            "description": "Time limit of one dispatch MILP [s].",
            "examples": [10.0],
        },
    )
    mip_rel_gap: float = Field(
        default=1e-4,
        ge=0,
        le=0.1,
        json_schema_extra={
            "description": "Relative optimality gap of the MILP.",
            "examples": [1e-4],
        },
    )
    hp_search_seconds: float = Field(
        default=20.0,
        ge=0,
        le=600,
        json_schema_extra={
            "description": "Time budget of the coordinate search over the heat pump modes [s].",
            "examples": [20.0],
        },
    )
    thermal_model: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "Plan the heat pump inside the MILP (linear building and tank model, COP "
                "secants) and turn the heat schedule into mode candidates; off = mode "
                "candidates from heuristics and the coordinate search only."
            ),
            "examples": [True],
        },
    )
    joint_modes: bool = Field(
        default=False,
        json_schema_extra={
            "description": (
                "RL29 fork: plan the heating modes inside the MILP. One binary per gene "
                "period and mode selects the room target; the controller's heat for that "
                "target (affine in room and screed temperature, clipped to zero and the "
                "capacity) is modelled exactly with binaries. The resulting modes are one "
                "more candidate for the exact simulation and the search, next to the "
                "projected heat schedule. Needs thermal_model."
            ),
            "examples": [False, True],
        },
    )
    joint_time_limit_s: float = Field(
        default=30.0,
        gt=0.0,
        json_schema_extra={
            "description": "Time limit of the joint mode MILP [s].",
            "examples": [30.0, 90.0],
        },
    )
    joint_window_hours: int = Field(
        default=6,
        ge=1,
        le=48,
        json_schema_extra={
            "description": (
                "Relax-and-fix window of the joint mode MILP [h]: the modes of one window "
                "are decided with binaries while the later hours are relaxed, then fixed; "
                "the time limit is shared between the windows."
            ),
            "examples": [6],
        },
    )
    seeds: int = Field(
        default=3,
        ge=1,
        le=20,
        json_schema_extra={
            "description": "Best candidates handed to the polish step.",
            "examples": [3],
        },
    )
    polish_generations: int = Field(
        default=30,
        ge=0,
        le=2000,
        json_schema_extra={
            "description": (
                "GENETIC generations on the exact simulation after the MILP (0 = MILP plan only)."
            ),
            "examples": [30, 0],
        },
    )
    polish_individuals: int = Field(
        default=60,
        ge=10,
        le=2000,
        json_schema_extra={"description": "Population of the polish step.", "examples": [60]},
    )

    scenarios: bool = Field(
        default=False,
        json_schema_extra={
            "description": (
                "Plan against three weighted scenarios (central 50 %, little PV + high load "
                "25 %, much PV + low load 25 %) instead of one forecast; appliance starts "
                "and the first `shared_slots` battery decisions are common to all."
            ),
            "examples": [True],
        },
    )
    shared_slots: int = Field(
        default=4,
        ge=1,
        le=96,
        json_schema_extra={
            "description": "Slots decided jointly for all scenarios.",
            "examples": [4],
        },
    )
    pv_scenario_spread: float = Field(
        default=0.25,
        ge=0,
        le=0.9,
        json_schema_extra={
            "description": "PV of the outer scenarios: forecast x (1 -/+ spread).",
            "examples": [0.25],
        },
    )
    load_scenario_spread: Optional[float] = Field(
        default=None,
        ge=0,
        le=0.9,
        json_schema_extra={
            "description": (
                "Load of the outer scenarios: forecast x (1 +/- spread). None = the "
                "quantile width published by LoadML, else 0.2."
            ),
            "examples": [None, 0.2],
        },
    )
    uncertainty_budget_hours: float = Field(
        default=0.0,
        ge=0,
        le=24,
        json_schema_extra={
            "description": (
                "RL29 fork: hedge against forecast errors (evcc risk profile, budget of "
                "Bertsimas and Sim). At most this many hours per day deliver "
                "`uncertainty_pv_dev` less PV and `uncertainty_load_dev` more load than "
                "forecast; the plan prices the reserve and import that would cover it. "
                "0 = off. A quarter of a day covers the usual bad afternoon."
            ),
            "examples": [0.0, 6.0],
        },
    )
    uncertainty_pv_dev: float = Field(
        default=0.3,
        ge=0,
        le=1,
        json_schema_extra={
            "description": "RL29 fork: share of the PV forecast that may fall short per slot.",
            "examples": [0.3],
        },
    )
    uncertainty_load_dev: Optional[float] = Field(
        default=None,
        ge=0,
        le=2,
        json_schema_extra={
            "description": (
                "RL29 fork: share by which the base load may exceed the forecast per slot. "
                "None = upper quantile width published by LoadML, else 0.2."
            ),
            "examples": [None, 0.2],
        },
    )
    switching: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "RL29 fork: the battery seeds are re-solved with the mode changes priced like "
                "the GENETIC fitness (`genetic.battery_change_penalty_amt`, "
                "`battery_min_hold_minutes`, `battery_hold_penalty_amt`), so the MILP plan "
                "already respects the switching limits. The candidate search stays a pure "
                "cost model."
            ),
            "examples": [True],
        },
    )
    switch_tie_break: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "RL29 fork: after the switching solve of the best seed, a second solve keeps "
                "its objective and minimises the number of battery mode changes (evcc "
                "continuity)."
            ),
            "examples": [True],
        },
    )
    switch_time_limit_s: float = Field(
        default=10.0,
        gt=0,
        le=300,
        json_schema_extra={
            "description": "Time limit of each switching solve and of the tie break [s].",
            "examples": [10.0],
        },
    )
    switch_initial_from_plan: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "RL29 fork: the battery mode of the previous plan for the current slot is "
                "the mode in effect; leaving it in the first slot counts as a change."
            ),
            "examples": [True],
        },
    )
    lock_charge_discharge: Literal["never", "negative_prices", "always"] = Field(
        default="negative_prices",
        json_schema_extra={
            "description": (
                "RL29 fork: forbid charging and discharging the battery in the same slot "
                "(binaries); `negative_prices` only where cycling could earn money."
            ),
            "examples": ["negative_prices"],
        },
    )

    max_grid_import_w: Optional[float] = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "description": "Grid import limit [W] (e.g. main fuse); infeasible plans fall back to no limit.",
            "examples": [None, 24000],
        },
    )
    max_grid_export_w: Optional[float] = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "description": "Grid feed-in limit [W]; PV above it is curtailed.",
            "examples": [None, 12600],
        },
    )
