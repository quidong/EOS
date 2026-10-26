"""Settings of the MILP optimization (RL29 fork); kept light for the config import."""

from typing import Optional

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
