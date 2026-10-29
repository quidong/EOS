from enum import StrEnum
from typing import Any, Optional

from pydantic import Field, computed_field, model_validator

from akkudoktoreos.config.configabc import SettingsBaseModel
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.core.pydantic import (
    PydanticBaseModel,
    PydanticDateTimeDataFrame,
)
from akkudoktoreos.optimization.genetic0.genetic0settings import Genetic0CommonSettings
from akkudoktoreos.optimization.genetic.geneticsettings import (
    GeneticCommonSettings,
    normalize_genetic_settings,
)
from akkudoktoreos.optimization.milp.milpsettings import MilpCommonSettings
from akkudoktoreos.utils.datetimeutil import DateTime
from akkudoktoreos.utils.gridinterpolator import GridInterpolatorBackend


class OptimizationAlgorithm(StrEnum):
    """Optimization Algorithm."""

    GENETIC = "GENETIC"
    GENETIC0 = "GENETIC0"
    MILP = "MILP"
    """RL29 fork: MILP dispatch + heat pump search, checked and polished by GENETIC."""


def optimization_default_algorithm() -> OptimizationAlgorithm:
    """Provide default optimization algorithm."""
    return OptimizationAlgorithm.GENETIC


class GridConnectionSettings(SettingsBaseModel):
    """Limits of the grid connection (fork), used by GENETIC and MILP.

    ``max_export_w``: feed-in cap, e.g. 60 % of the PV peak power for plants without a
    smart meter (Germany, Solarspitzengesetz) or a dynamic export limit. PV above it is
    curtailed and earns nothing, so the optimizer charges the battery (or runs loads)
    during the peak instead of filling it in the morning.

    ``max_import_w``: import cap (main fuse, contracted power, peak shaving). Energy
    above it costs ``import_excess_penalty_amt_kwh`` on top of the price, so the
    optimizer spreads charging and loads.
    """

    max_export_w: Optional[float] = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "description": "Feed-in limit [W]; None = unlimited.",
            "examples": [None, 6000],
        },
    )
    max_import_w: Optional[float] = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "description": "Import limit [W]; None = unlimited.",
            "examples": [None, 11000],
        },
    )
    import_excess_penalty_amt_kwh: float = Field(
        default=1.0,
        ge=0,
        json_schema_extra={
            "description": "Cost of energy above the import limit [currency/kWh] (GENETIC).",
            "examples": [1.0],
        },
    )


class OptimizationCommonSettings(SettingsBaseModel):
    """General Optimization Configuration."""

    @model_validator(mode="before")
    @classmethod
    def normalize_old_genetic_settings(cls, value: Any) -> Any:
        """Accept the feature branch's flat GENETIC settings without losing values."""
        return normalize_genetic_settings(value)

    algorithm: OptimizationAlgorithm = Field(
        default_factory=optimization_default_algorithm,
        json_schema_extra={
            "description": (
                f"Optimization algorithm "
                f"[{' | '.join(mode.value for mode in OptimizationAlgorithm)}]. "
                f"Defaults to {optimization_default_algorithm()}."
            ),
            "examples": ["GENETIC", "GENETIC0", "MILP"],
        },
    )

    genetic: GeneticCommonSettings = Field(
        default_factory=GeneticCommonSettings,
        json_schema_extra={
            "description": "GENETIC optimization algorithm configuration.",
            "examples": [{"individuals": 400, "seed": None, "penalties": {"ev_soc_miss": 10}}],
        },
    )

    genetic0: Genetic0CommonSettings = Field(
        default_factory=Genetic0CommonSettings,
        json_schema_extra={
            "description": "GENETIC0 optimization algorithm configuration.",
            "examples": [{"individuals": 400, "seed": None, "penalties": {"ev_soc_miss": 10}}],
        },
    )

    alternate_algorithms: list[OptimizationAlgorithm] = Field(
        default_factory=list,
        json_schema_extra={
            "description": (
                "RL29 fork: A/B operation - when set, the algorithm changes every local day "
                "in this order (day number modulo the list length) and `algorithm` is "
                "ignored for automatic runs. The history records which algorithm planned "
                "each run, so realised costs can be compared."
            ),
            "examples": [[], ["GENETIC", "MILP"]],
        },
    )

    milp: MilpCommonSettings = Field(
        default_factory=MilpCommonSettings,
        json_schema_extra={
            "description": (
                "MILP optimization configuration (RL29 fork). MILP uses the GENETIC "
                "settings (slots, horizons, terminal value) and adds these."
            ),
            "examples": [{"hp_search_seconds": 20, "polish_generations": 30}],
        },
    )

    grid: GridConnectionSettings = Field(
        default_factory=GridConnectionSettings,
        json_schema_extra={
            "description": (
                "Fork: grid connection limits (feed-in cap, import cap) for GENETIC and MILP; "
                "milp.max_grid_*_w override them for MILP."
            )
        },
    )

    self_consumption_interpolator: GridInterpolatorBackend = Field(
        default=GridInterpolatorBackend.SCIPY,
        json_schema_extra={
            "description": (
                "How the inverter's self-consumption probability table is evaluated "
                "[scipy | numpy]. 'scipy' (default) uses scipy.interpolate. 'numpy' gives "
                "the same values without importing SciPy, which saves about 35 MB RSS "
                "after the first optimization run (useful on devices with little RAM); "
                "a run takes about 2.5 % longer."
            ),
            "examples": ["scipy", "numpy"],
        },
    )

    # Computed fields
    @computed_field  # type: ignore[prop-decorator]
    @property
    def algorithms(self) -> list[str]:
        """Available optimization algorithms."""
        return [algo.value for algo in OptimizationAlgorithm]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def keys(self) -> list[str]:
        """The keys of the solution."""
        try:
            ems_eos = get_ems()
        except Exception:
            # ems might not be initialized
            return []

        key_list = []
        optimization_solution = ems_eos.optimization_solution()
        if optimization_solution:
            # Prepare mapping
            df = optimization_solution.solution.to_dataframe()
            key_list = df.columns.tolist()
        return sorted(set(key_list))


class OptimizationSolution(PydanticBaseModel):
    """General Optimization Solution."""

    id: str = Field(
        ..., json_schema_extra={"description": "Unique ID for the optimization solution."}
    )

    generated_at: DateTime = Field(
        ..., json_schema_extra={"description": "Timestamp when the solution was generated."}
    )

    comment: Optional[str] = Field(
        default=None,
        json_schema_extra={"description": "Optional comment or annotation for the solution."},
    )

    valid_from: Optional[DateTime] = Field(
        default=None, json_schema_extra={"description": "Start time of the optimization solution."}
    )

    valid_until: Optional[DateTime] = Field(
        default=None, json_schema_extra={"description": "End time of the optimization solution."}
    )

    total_losses_energy_wh: float = Field(
        json_schema_extra={"description": "The total losses in watt-hours over the entire period."}
    )

    total_revenues_amt: float = Field(
        json_schema_extra={"description": "The total revenues [money amount]."}
    )

    total_costs_amt: float = Field(
        json_schema_extra={"description": "The total costs [money amount]."}
    )

    fitness_score: set[float] = Field(
        json_schema_extra={"description": "The fitness score as a set of fitness values."}
    )

    prediction: PydanticDateTimeDataFrame = Field(
        json_schema_extra={
            "description": (
                "Datetime data frame with time series prediction data per optimization interval:"
                "- pv_energy_wh: PV energy prediction (positive) in wh"
                "- elec_price_amt_kwh: Electricity price prediction in money per kwh"
                "- feed_in_tariff_amt_kwh: Feed in tariff prediction in money per kwh"
                "- weather_temp_air_celcius: Temperature in °C"
                "- loadforecast_energy_wh: Load mean energy prediction in wh"
                "- loadakkudoktor_std_energy_wh: Load energy standard deviation prediction in wh"
                "- loadakkudoktor_mean_energy_wh: Load mean energy prediction in wh"
            )
        }
    )

    solution: PydanticDateTimeDataFrame = Field(
        json_schema_extra={
            "description": (
                "Datetime data frame with time series solution data per optimization interval:"
                "- load_energy_wh: Load of all energy consumers in wh"
                "- grid_energy_wh: Grid energy feed in (negative) or consumption (positive) in wh"
                "- costs_amt: Costs in money amount"
                "- revenue_amt: Revenue in money amount"
                "- losses_energy_wh: Energy losses in wh"
                "- <device-id>_operation_mode_id: Operation mode id of the device."
                "- <device-id>_operation_mode_factor: Operation mode factor of the device."
                "- <device-id>_soc_factor: State of charge of a battery/ electric vehicle device as factor of total capacity."
                "- <device-id>_energy_wh: Energy consumption (positive) of a device in wh."
            )
        }
    )


def active_algorithm(settings: Any, moment: Any = None) -> OptimizationAlgorithm:
    """Algorithm for an automatic run at ``moment`` (RL29 fork: A/B by local day).

    ``moment`` is a datetime in the site time zone; None = configured algorithm.
    """
    order = list(getattr(settings, "alternate_algorithms", None) or [])
    if not order or moment is None:
        return settings.algorithm
    day = moment.date().toordinal()
    return OptimizationAlgorithm(order[day % len(order)])
