"""Settings of the MILP optimization (RL29 fork); kept light for the config import."""

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
