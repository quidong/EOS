"""Heat pump device settings (RL29 fork).

A heat pump device bundles the building it heats, the domestic hot water tank
with an optional heating rod and the heat pump unit itself. The physical models
live in ``akkudoktoreos.devices.thermal``; this module only holds the
configuration and converts it.

Example::

    devices:
      heat_pumps:
        wbb12:
          building:
            slab_capacity_wh_per_k: 3450
            room_capacity_wh_per_k: 10000
            loss_w_per_k: 180
            slab_room_w_per_k: 1500
            emitter_w_per_k: 1000
          dhw:
            volume_l: 190
            rod_power_w: 3000
          comfort:
            setpoint_c: 21.0
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field, computed_field, field_validator, model_validator

from akkudoktoreos.config.configabc import (
    SettingsBaseModel,
    ValueTimeWindowSequence,
)
from akkudoktoreos.devices.settings.devicebasesettings import DevicesBaseSettings
from akkudoktoreos.devices.thermal.building import BuildingParameters
from akkudoktoreos.devices.thermal.calibration import (
    CalibrationOptions,
    CalibrationPriors,
    CalibrationResult,
)
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel, CopPoint
from akkudoktoreos.devices.thermal.dhw import DhwTankParameters
from akkudoktoreos.devices.thermal.heatpump import (
    ComfortParameters,
    CoolingParameters,
    DhwControlParameters,
    HeatPumpParameters,
    HeatPumpSystem,
    SlotInputs,
)
from akkudoktoreos.utils.datetimeutil import DateTime


class HeatPumpBuildingSettings(SettingsBaseModel):
    """Two-node building model (screed + room)."""

    slab_capacity_wh_per_k: float = Field(
        default=3500.0,
        gt=0,
        json_schema_extra={
            "description": "Heat capacity of screed, heating water and buffer [Wh/K]."
        },
    )
    room_capacity_wh_per_k: float = Field(
        default=10000.0,
        gt=0,
        json_schema_extra={
            "description": "Effective heat capacity of air and inner building mass [Wh/K]."
        },
    )
    loss_w_per_k: float = Field(
        default=180.0,
        gt=0,
        json_schema_extra={"description": "Heat loss coefficient room to outside [W/K]."},
    )
    slab_room_w_per_k: float = Field(
        default=1500.0,
        gt=0,
        json_schema_extra={"description": "Heat transfer underfloor heating to room [W/K]."},
    )
    emitter_w_per_k: float = Field(
        default=1000.0,
        gt=0,
        json_schema_extra={"description": "Heat transfer heating water to screed [W/K]."},
    )
    solar_aperture_m2: float = Field(
        default=0.0,
        ge=0,
        json_schema_extra={
            "description": "Effective solar aperture [m²]: gain = aperture x global irradiance."
        },
    )
    internal_gains_w: float = Field(
        default=0.0,
        ge=0,
        json_schema_extra={"description": "Constant internal gains [W]."},
    )

    def to_parameters(self) -> BuildingParameters:
        return BuildingParameters(**self.model_dump())


class HeatPumpCopPointSettings(SettingsBaseModel):
    """Rating point of the heat pump (EN 14511)."""

    outdoor_c: float = Field(
        ..., json_schema_extra={"description": "Outdoor air temperature [degC]."}
    )
    flow_c: float = Field(..., json_schema_extra={"description": "Flow temperature [degC]."})
    cop: float = Field(..., gt=0, json_schema_extra={"description": "Rated COP."})


def _default_cop_points() -> list[HeatPumpCopPointSettings]:
    return [
        HeatPumpCopPointSettings(outdoor_c=p.outdoor_c, flow_c=p.flow_c, cop=p.cop)
        for p in WBB12_RATING_POINTS
    ]


class HeatPumpUnitSettings(SettingsBaseModel):
    """Heat pump unit."""

    cop_points: list[HeatPumpCopPointSettings] = Field(
        default_factory=_default_cop_points,
        min_length=1,
        json_schema_extra={"description": "Rating points; default Weishaupt WBB 12."},
    )
    cop_scale: float = Field(
        default=1.0,
        gt=0,
        json_schema_extra={
            "description": "Calibration factor on the rated COP (pumps, defrost, controller). "
            "Determined by scripts/rl29/calibrate_heatpump.py."
        },
    )
    cop_max: float = Field(
        default=7.0,
        gt=1,
        json_schema_extra={
            "description": (
                "Ceiling of the modelled COP [-]. The rating points cover W35/W55; at mild "
                "weather with a low flow temperature the Carnot extrapolation overshoots, "
                "and a too generous ceiling makes heating in mild hours look nearly free. "
                "Air/water units rarely exceed 6-7 even at A12/W25."
            ),
            "examples": [7.0, 6.5],
        },
    )
    max_heat_w: float = Field(
        default=10000.0, gt=0, json_schema_extra={"description": "Maximum thermal output [W]."}
    )
    max_electric_w: float = Field(
        default=5500.0, gt=0, json_schema_extra={"description": "Maximum compressor power [W]."}
    )
    standby_w: float = Field(
        default=22.0, ge=0, json_schema_extra={"description": "Standby power [W]."}
    )
    max_flow_c: float = Field(
        default=45.0,
        json_schema_extra={"description": "Highest flow temperature for space heating [degC]."},
    )
    flow_spread_k: float = Field(
        default=3.0, gt=0, json_schema_extra={"description": "Flow-return spread [K]."}
    )
    dhw_heat_w: float = Field(
        default=5000.0,
        gt=0,
        json_schema_extra={"description": "Thermal power while charging hot water [W]."},
    )
    dhw_flow_overshoot_k: float = Field(
        default=7.0,
        ge=0,
        json_schema_extra={
            "description": "Flow above tank temperature while charging hot water [K]."
        },
    )
    max_dhw_c: float = Field(
        default=60.0,
        json_schema_extra={
            "description": (
                "Highest hot water temperature the heat pump reaches by itself (compressor, "
                "without heating rod) [degC]; caps every hot water setpoint of the heat pump."
            )
        },
    )
    forced_overshoot_k: float = Field(
        default=1.0,
        ge=0,
        json_schema_extra={
            "description": "FORCED mode (SG Ready 4) heats until comfort.max_c + this offset [K]."
        },
    )


def _default_draw_profile() -> list[float]:
    """Typical household tapping: about 4 kWh/day, peaks morning and evening [Wh/h]."""
    profile = [20, 10, 10, 10, 20, 80, 450, 500, 250, 120, 100, 120,
               200, 150, 100, 100, 120, 200, 350, 450, 350, 200, 80, 40]  # fmt: skip
    return [float(v) for v in profile]


class HeatPumpDhwSettings(SettingsBaseModel):
    """Domestic hot water tank, heat pump setpoint and heating rod."""

    volume_l: float = Field(
        default=190.0, gt=0, json_schema_extra={"description": "Water volume [l]."}
    )
    loss_w_per_k: float = Field(
        default=1.5, ge=0, json_schema_extra={"description": "Standby loss [W/K]."}
    )
    ambient_c: float = Field(
        default=18.0, json_schema_extra={"description": "Temperature around the tank [degC]."}
    )
    setpoint_c: float = Field(
        default=45.0,
        json_schema_extra={
            "description": "Heat pump hot water setpoint; overridden by measurement '<id>-dhw-setpoint'."
        },
    )
    hysteresis_k: float = Field(
        default=5.0, gt=0, json_schema_extra={"description": "Heat pump switch-on difference [K]."}
    )
    min_c: float = Field(
        default=38.0,
        json_schema_extra={
            "description": (
                "Comfort minimum of the tank [degC]; below the heat pump switch-on "
                "temperature, which the tank briefly undershoots while tapping."
            )
        },
    )
    raised_setpoint_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Hot water setpoint while space heating is RAISED (SG Ready 3), if the heat "
                "pump raises hot water on SG Ready; None = RAISED leaves hot water alone."
            )
        },
    )
    forced_setpoint_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Hot water setpoint while space heating is FORCED (SG Ready 4); None = "
                "FORCED leaves hot water alone."
            )
        },
    )
    rod_power_w: float = Field(
        default=0.0,
        ge=0,
        json_schema_extra={"description": "External heating rod power [W]; 0 = no rod."},
    )
    rod_targets_c: list[float] = Field(
        default_factory=lambda: [60.0, 70.0],
        json_schema_extra={
            "description": (
                "Target temperatures the optimizer may give the heating rod [degC]; one "
                "hot water option per value. Ignored without a rod."
            ),
            "examples": [[60.0, 70.0], [55.0], [65.0]],
        },
    )
    rod_max_c: float = Field(
        default=70.0,
        json_schema_extra={"description": "Highest tank temperature by the rod [degC]."},
    )
    legionella_c: float = Field(
        default=60.0, json_schema_extra={"description": "Legionella protection temperature [degC]."}
    )
    legionella_interval_days: Optional[int] = Field(
        default=7,
        ge=1,
        json_schema_extra={
            "description": (
                "Reach legionella_c at least every N days; None disables (e.g. the heat pump "
                "runs its own legionella program). Planned with whatever reaches the "
                "temperature: a rod target or a raised/forced heat pump setpoint."
            )
        },
    )
    draw_profile_wh: list[float] = Field(
        default_factory=_default_draw_profile,
        json_schema_extra={"description": "Hot water tapped per hour of day, 24 values [Wh]."},
    )

    @field_validator("draw_profile_wh")
    @classmethod
    def validate_profile(cls, value: list[float]) -> list[float]:
        if len(value) != 24 or any(v < 0 for v in value):
            raise ValueError("draw_profile_wh needs 24 non-negative hourly values.")
        return value

    @field_validator("rod_targets_c")
    @classmethod
    def validate_rod_targets(cls, value: list[float]) -> list[float]:
        return sorted(set(float(v) for v in value))

    @model_validator(mode="after")
    def validate_rod_targets_below_max(self) -> "HeatPumpDhwSettings":
        if any(target > self.rod_max_c for target in self.rod_targets_c):
            raise ValueError("rod_targets_c must not exceed rod_max_c.")
        return self

    def draw_wh(self, start: DateTime, slot_hours: float) -> float:
        """Tapped energy in the slot starting at ``start`` [Wh]."""
        return self.draw_profile_wh[start.hour] * slot_hours


def _default_comfort_min() -> ValueTimeWindowSequence:
    return ValueTimeWindowSequence.model_validate(
        {
            "windows": [
                {"start_time": "00:00", "duration": "6 hours", "value": 19.5},
                {"start_time": "06:00", "duration": "16 hours", "value": 20.5},
                {"start_time": "22:00", "duration": "2 hours", "value": 19.5},
            ]
        }
    )


class HeatPumpComfortSettings(SettingsBaseModel):
    """Room comfort band (the same for all rooms)."""

    setpoint_c: float = Field(
        default=21.0, json_schema_extra={"description": "Room setpoint in normal operation [degC]."}
    )
    max_c: float = Field(
        default=22.0,
        json_schema_extra={"description": "Highest room temperature for pre-heating [degC]."},
    )
    min_c: ValueTimeWindowSequence = Field(
        default_factory=_default_comfort_min,
        json_schema_extra={"description": "Comfort minimum by time of day [degC]; fallback 20.0."},
    )
    raised_offset_k: float = Field(
        default=1.0, ge=0, json_schema_extra={"description": "Raised operation above setpoint [K]."}
    )

    def minimum_at(self, dt: DateTime) -> float:
        for window in self.min_c.windows:
            if window.contains(dt) and window.value is not None:
                return float(window.value)
        return 20.0


class HeatPumpCoolingSettings(SettingsBaseModel):
    """Space cooling through the floor heating (RL29 fork).

    In the cooling season (measurement '<device_id>-cooling-season' = 1) the
    optimizer chooses per hour between NORMAL (no active cooling) and COOL. With
    ``pv_only`` cooling may only use the forecast PV surplus of the hour.
    """

    enabled: bool = Field(
        default=False,
        json_schema_extra={"description": "The heat pump can cool through the floor."},
    )
    setpoint_c: float = Field(
        default=24.0,
        json_schema_extra={"description": "Room target while cooling [degC]."},
    )
    max_c: float = Field(
        default=26.0,
        json_schema_extra={
            "description": (
                "Comfort maximum in the cooling season [degC]; above it the overheat "
                "penalty applies."
            )
        },
    )
    max_cool_w: float = Field(
        default=6000.0,
        gt=0,
        json_schema_extra={"description": "Maximum cooling capacity [W]."},
    )
    eer: float = Field(
        default=4.5,
        gt=0,
        json_schema_extra={
            "description": (
                "Rated energy efficiency ratio of cooling at eer_rated_outdoor_c / "
                "eer_rated_flow_c (EN 14511, e.g. A35/W18). Per slot the EER is scaled "
                "with the Carnot efficiency for the forecast outdoor temperature and the "
                "actual cooling flow temperature: lower on hot afternoons, higher in mild "
                "evenings."
            ),
            "examples": [4.5],
        },
    )
    eer_rated_outdoor_c: float = Field(
        default=35.0,
        json_schema_extra={"description": "Outdoor temperature of the rated EER [degC]."},
    )
    eer_rated_flow_c: float = Field(
        default=18.0,
        json_schema_extra={"description": "Flow temperature of the rated EER [degC]."},
    )
    eer_constant: bool = Field(
        default=False,
        json_schema_extra={
            "description": "Use the rated EER in every slot regardless of the temperatures."
        },
    )
    min_flow_c: float = Field(
        default=18.0,
        json_schema_extra={"description": "Lowest flow temperature for floor cooling [degC]."},
    )
    dewpoint_margin_k: float = Field(
        default=2.0,
        ge=0,
        json_schema_extra={
            "description": (
                "Flow temperature at least this far above the room dew point "
                "(measurement '<device_id>-dewpoint') [K]."
            )
        },
    )
    pv_only: bool = Field(
        default=True,
        json_schema_extra={
            "description": "Cool only with the forecast PV surplus (PV minus load) of the hour."
        },
    )
    min_pv_surplus_w: float = Field(
        default=500.0,
        ge=0,
        json_schema_extra={
            "description": "With pv_only: no cooling in hours with less PV surplus than this [W]."
        },
    )

    @model_validator(mode="after")
    def validate_band(self) -> "HeatPumpCoolingSettings":
        if self.max_c < self.setpoint_c:
            raise ValueError("cooling.max_c must not be below cooling.setpoint_c.")
        return self


class HeatPumpOptimizationSettings(SettingsBaseModel):
    """How the optimizer may operate the heat pump and what deviations cost."""

    heating_modes: list[int] = Field(
        default_factory=lambda: [0, 1, 2],
        min_length=1,
        json_schema_extra={
            "description": (
                "Space heating modes the optimizer may choose: 0 REDUCED (room may cool to "
                "the comfort minimum), 1 NORMAL, 2 RAISED (setpoint + raised_offset_k), "
                "3 PREHEAT (comfort maximum with all valves forced open), 4 FORCED (forced "
                "on, full capacity up to comfort maximum + forced_overshoot_k). EOS reports the "
                "mode; how it is executed (e.g. which SG Ready state) is mapped by the "
                "controller."
            ),
            "examples": [[0, 1, 2], [0, 1, 2, 3, 4], [1, 2]],
        },
    )
    dhw_modes: list[int] = Field(
        default_factory=lambda: [0, 1, 2],
        min_length=1,
        json_schema_extra={
            "description": (
                "Hot water modes the optimizer may choose: 0 HOLD (heat pump controller), "
                "1 HEAT_PUMP (charge to the heat pump setpoint now), "
                "2 ROD (heating rod to one of dhw.rod_targets_c; dropped without a rod)."
            ),
            "examples": [[0, 1, 2], [0, 1], [0]],
        },
    )
    comfort_penalty_amt_per_kh: float = Field(
        default=1.0,
        ge=0,
        json_schema_extra={
            "description": "Cost of a room below its comfort minimum [currency per K*h]."
        },
    )
    overheat_penalty_amt_per_kh: float = Field(
        default=0.5,
        ge=0,
        json_schema_extra={
            "description": (
                "Cost of a room above its comfort maximum [currency per K*h]. Keeps "
                "PREHEAT/FORCED from overheating the rooms; heat above the comfort maximum "
                "also earns no stored-heat credit."
            )
        },
    )
    active_mode_penalty_amt_per_h: float = Field(
        default=0.01,
        ge=0,
        json_schema_extra={
            "description": (
                "Cost per hour of an active mode (heating RAISED/PREHEAT/FORCED, hot water "
                "HEAT_PUMP/ROD) [currency/h]. Where a mode changes nothing, the plan then "
                "shows NORMAL/HOLD instead of an arbitrary active mode."
            )
        },
    )
    dhw_penalty_amt_per_kh: float = Field(
        default=0.5,
        ge=0,
        json_schema_extra={
            "description": "Cost of a tank below its comfort minimum [currency per K*h]."
        },
    )
    legionella_penalty_amt: float = Field(
        default=2.0,
        ge=0,
        json_schema_extra={"description": "Cost of a missed legionella heat-up [currency]."},
    )
    mode_change_penalty_amt: float = Field(
        default=0.002,
        ge=0,
        json_schema_extra={
            "description": (
                "Cost per change of the heating or hot water mode between two hours; "
                "keeps relay switching and compressor starts rare [currency]."
            )
        },
    )
    stored_heat_price_quantile: float = Field(
        default=0.25,
        gt=0,
        le=1.0,
        json_schema_extra={
            "description": (
                "Value of the heat stored in building and tank at the end of the horizon: "
                "it displaces heat the optimizer would otherwise produce in the cheapest "
                "hours, so it is valued at the mean of the cheapest share of the horizon's "
                "marginal prices (hours with enough PV surplus count with the feed-in "
                "tariff), divided by the COP. 1.0 values it at the mean price of the whole "
                "horizon, which tends to overheat the building at the end of sunny days."
            ),
            "examples": [0.25, 1.0],
        },
    )
    load_forecast_includes_heat_pump: bool = Field(
        default=False,
        json_schema_extra={
            "description": (
                "The load forecast still contains the heat pump. The consumption of "
                "the status quo operation (NORMAL/HOLD) is then subtracted from the "
                "forecast so the heat pump is not counted twice. Prefer a load "
                "forecast without the heat pump."
            )
        },
    )

    @field_validator("heating_modes")
    @classmethod
    def validate_heating_modes(cls, value: list[int]) -> list[int]:
        if any(mode not in (0, 1, 2, 3, 4) for mode in value):
            raise ValueError("Heating modes are 0..4.")
        return sorted(set(value))

    @field_validator("dhw_modes")
    @classmethod
    def validate_dhw_modes(cls, value: list[int]) -> list[int]:
        if any(mode not in (0, 1, 2) for mode in value):
            raise ValueError("Hot water modes are 0..2.")
        return sorted(set(value))


class HeatPumpCalibrationSettings(SettingsBaseModel):
    """Automatic calibration of the model from measured operation (RL29 fork).

    Same approach as the PV calibration: the configured parameters stay the
    prior, the measurements of a rolling window correct them, and every result
    is clipped to a band around the configuration. The fits need the
    measurements ``<id>-outdoor-temperature``, ``<id>-room-temperature``,
    ``<id>-heat-power`` (thermal output [W]), ``<id>-electric-power`` [W],
    ``<id>-flow-temperature``, ``<id>-dhw-temperature`` and optionally
    ``<id>-dhw-heat-power`` and ``<id>-ghi`` [W/m²]. Hours without an outdoor
    temperature or irradiance measurement are filled from the weather forecast
    provider (``weather_from_prediction``), so the solar term needs no extra
    sensor. Power and temperature keys should be imported with their history
    (``adapter.homeassistant.device_measurement_history_keys``) so the hourly
    means are exact.
    """

    enabled: bool = Field(
        default=False,
        json_schema_extra={
            "description": (
                "Fit building losses, solar aperture, internal gains, COP scale and tank "
                "loss from the measurements and plan with the fitted values. The "
                "configured values stay the prior; see the result in the log and in "
                "'<data folder>/rl29/heatpump_calibration.json'."
            ),
            "examples": [False, True],
        },
    )
    days: int = Field(
        default=30,
        ge=3,
        le=365,
        json_schema_extra={
            "description": "Length of the measurement window used for the fits [days].",
            "examples": [30, 14],
        },
    )
    refit_hours: float = Field(
        default=24.0,
        gt=0,
        json_schema_extra={
            "description": "Refit at most every this many hours; in between the stored fit is used.",
            "examples": [24.0, 6.0],
        },
    )
    fit_building: bool = Field(
        default=True,
        json_schema_extra={
            "description": "Fit building.loss_w_per_k, solar_aperture_m2 and internal_gains_w."
        },
    )
    fit_cop: bool = Field(default=True, json_schema_extra={"description": "Fit unit.cop_scale."})
    fit_dhw: bool = Field(default=True, json_schema_extra={"description": "Fit dhw.loss_w_per_k."})
    fit_emitter: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "Fit building.emitter_w_per_k from the measured flow temperature (flow = "
                "room + heat/slab_room + heat/emitter + spread/2 in steady operation); the "
                "flow temperature sets the COP."
            )
        },
    )
    weather_from_prediction: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "Fill hours without a measured outdoor temperature or irradiance from the "
                "weather provider (the values are stored as measurements, so the history "
                "builds up while EOS runs)."
            ),
            "examples": [True],
        },
    )
    min_days: int = Field(
        default=5,
        ge=2,
        json_schema_extra={
            "description": "Minimum number of complete days for the building fit.",
            "examples": [5],
        },
    )
    prior_weight_days: float = Field(
        default=7.0,
        ge=0,
        json_schema_extra={
            "description": (
                "Shrinkage of the building fit: the configured values weigh like this many "
                "measured days. 0 trusts the measurements alone."
            ),
            "examples": [7.0, 2.0, 0.0],
        },
    )
    outage_threshold: float = Field(
        default=0.4,
        ge=0,
        lt=1,
        json_schema_extra={
            "description": (
                "A day delivering less than this share of the healthy heat per kelvin "
                "although the model expects heating is excluded (heat pump off, manual "
                "operation). 0 disables the filter."
            ),
            "examples": [0.4],
        },
    )
    loss_min_factor: float = Field(
        default=0.3,
        gt=0,
        le=1,
        json_schema_extra={
            "description": "Lower clamp of the fitted loss, relative to the configured value."
        },
    )
    loss_max_factor: float = Field(
        default=3.0,
        ge=1,
        json_schema_extra={
            "description": "Upper clamp of the fitted loss, relative to the configured value."
        },
    )
    cop_min_heat_w: float = Field(
        default=3000.0,
        ge=0,
        json_schema_extra={
            "description": (
                "Hours count for the COP fit only above this mean heat output [W]: the "
                "lowest compressor modulation, so the heat pump ran the whole hour."
            ),
            "examples": [3000.0],
        },
    )
    cop_prior_hours: float = Field(
        default=24.0,
        ge=0,
        json_schema_extra={
            "description": "Shrinkage of the COP scale: the configured value weighs like this many hours.",
            "examples": [24.0],
        },
    )
    cop_min_scale: float = Field(
        default=0.5, gt=0, json_schema_extra={"description": "Lower clamp of the fitted COP scale."}
    )
    cop_max_scale: float = Field(
        default=1.5, gt=0, json_schema_extra={"description": "Upper clamp of the fitted COP scale."}
    )

    @model_validator(mode="after")
    def validate_bands(self) -> "HeatPumpCalibrationSettings":
        if self.cop_max_scale < self.cop_min_scale:
            raise ValueError("calibration.cop_max_scale must not be below cop_min_scale.")
        return self

    def to_options(self) -> CalibrationOptions:
        return CalibrationOptions(
            min_days=self.min_days,
            prior_weight_days=self.prior_weight_days,
            outage_threshold=self.outage_threshold,
            min_healthy_days=3 if self.outage_threshold > 0 else 10**9,
            loss_min_factor=self.loss_min_factor,
            loss_max_factor=self.loss_max_factor,
            cop_min_heat_w=self.cop_min_heat_w,
            cop_prior_hours=self.cop_prior_hours,
            cop_min_scale=self.cop_min_scale,
            cop_max_scale=self.cop_max_scale,
        )


class HeatPumpCommonSettings(DevicesBaseSettings):
    """Heat pump with building, hot water tank and heating rod (RL29 fork)."""

    building: HeatPumpBuildingSettings = Field(default_factory=HeatPumpBuildingSettings)
    unit: HeatPumpUnitSettings = Field(default_factory=HeatPumpUnitSettings)
    dhw: HeatPumpDhwSettings = Field(default_factory=HeatPumpDhwSettings)
    comfort: HeatPumpComfortSettings = Field(default_factory=HeatPumpComfortSettings)
    optimization: HeatPumpOptimizationSettings = Field(default_factory=HeatPumpOptimizationSettings)
    cooling: HeatPumpCoolingSettings = Field(default_factory=HeatPumpCoolingSettings)
    calibration: HeatPumpCalibrationSettings = Field(default_factory=HeatPumpCalibrationSettings)
    power_limit_w: Optional[ValueTimeWindowSequence] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Electric limit for heat pump plus heating rod by time window [W], e.g. the "
                "§14a EnWG dimming window announced by the grid operator."
            ),
            "examples": [
                None,
                {"windows": [{"start_time": "19:00", "duration": "2 hours", "value": 4200}]},
            ],
        },
    )

    # ------------------------------------------------------------------
    # Measurement keys
    # ------------------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_room_temperature(self) -> str:
        return f"{self.device_id}-room-temperature"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_slab_temperature(self) -> str:
        return f"{self.device_id}-slab-temperature"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_dhw_temperature(self) -> str:
        return f"{self.device_id}-dhw-temperature"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_dhw_setpoint(self) -> str:
        return f"{self.device_id}-dhw-setpoint"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_power_limited(self) -> str:
        return f"{self.device_id}-power-limited"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_keys(self) -> list[str]:
        keys = [
            self.measurement_key_room_temperature,
            self.measurement_key_slab_temperature,
            self.measurement_key_dhw_temperature,
            self.measurement_key_dhw_setpoint,
            self.measurement_key_power_limited,
        ]
        if self.cooling.enabled:
            keys += [self.measurement_key_cooling_season, self.measurement_key_dewpoint]
        if self.calibration.enabled:
            keys += [k for k in self.calibration_measurement_keys.values() if k not in keys]
        return keys

    # Calibration measurements (RL29 fork): sample field -> measurement key.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def calibration_measurement_keys(self) -> dict[str, str]:
        """Measurement keys of the calibration by hourly sample field."""
        return {
            "outdoor_c": f"{self.device_id}-outdoor-temperature",
            "room_c": self.measurement_key_room_temperature,
            "heat_w": f"{self.device_id}-heat-power",
            "electric_w": f"{self.device_id}-electric-power",
            "flow_c": f"{self.device_id}-flow-temperature",
            "dhw_c": self.measurement_key_dhw_temperature,
            "dhw_heat_w": f"{self.device_id}-dhw-heat-power",
            "ghi_w_per_m2": f"{self.device_id}-ghi",
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_cooling_season(self) -> str:
        """1 = cooling season (the controller has switched the system to cooling)."""
        return f"{self.device_id}-cooling-season"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_dewpoint(self) -> str:
        """Room dew point [degC]."""
        return f"{self.device_id}-dewpoint"

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def cop_model(self) -> CopModel:
        points = [CopPoint(p.outdoor_c, p.flow_c, p.cop) for p in self.unit.cop_points]
        return CopModel(points, scale=self.unit.cop_scale, cop_max=self.unit.cop_max)

    def calibration_priors(self) -> CalibrationPriors:
        """The configured values as prior of the calibration (RL29 fork)."""
        return CalibrationPriors(
            loss_w_per_k=self.building.loss_w_per_k,
            internal_gains_w=self.building.internal_gains_w,
            solar_aperture_m2=self.building.solar_aperture_m2,
            cop_scale=self.unit.cop_scale,
            emitter_w_per_k=self.building.emitter_w_per_k,
            slab_room_w_per_k=self.building.slab_room_w_per_k,
            flow_spread_k=self.unit.flow_spread_k,
            dhw_loss_w_per_k=self.dhw.loss_w_per_k,
            standby_w=self.unit.standby_w,
            dhw_volume_l=self.dhw.volume_l,
            dhw_ambient_c=self.dhw.ambient_c,
        )

    def calibrated(self, result: Optional[CalibrationResult]) -> "HeatPumpCommonSettings":
        """A copy of these settings with the fitted parameters applied (RL29 fork).

        The configuration itself is never changed: it stays the prior of the
        next fit. Missing fits keep the configured value.
        """
        if result is None or not (result.building or result.cop or result.dhw or result.emitter):
            return self
        data = self.model_dump(exclude=set(type(self).model_computed_fields))
        if result.building:
            data["building"]["loss_w_per_k"] = result.building.loss_w_per_k
            data["building"]["solar_aperture_m2"] = result.building.solar_aperture_m2
            data["building"]["internal_gains_w"] = result.building.internal_gains_w
        if result.cop:
            data["unit"]["cop_scale"] = result.cop.cop_scale
        if result.emitter:
            data["building"]["emitter_w_per_k"] = result.emitter.emitter_w_per_k
        if result.dhw:
            data["dhw"]["loss_w_per_k"] = result.dhw.loss_w_per_k
        return type(self).model_validate(data)

    def to_system(
        self, slot_hours: float, dhw_setpoint_c: Optional[float] = None
    ) -> HeatPumpSystem:
        unit = self.unit
        dhw = self.dhw
        return HeatPumpSystem(
            building=self.building.to_parameters(),
            tank=DhwTankParameters(dhw.volume_l, dhw.loss_w_per_k, dhw.ambient_c),
            cop=self.cop_model(),
            heat_pump=HeatPumpParameters(
                max_heat_w=unit.max_heat_w,
                max_electric_w=unit.max_electric_w,
                standby_w=unit.standby_w,
                max_flow_c=unit.max_flow_c,
                flow_spread_k=unit.flow_spread_k,
                dhw_heat_w=unit.dhw_heat_w,
                dhw_flow_overshoot_k=unit.dhw_flow_overshoot_k,
                max_dhw_c=unit.max_dhw_c,
                forced_overshoot_k=unit.forced_overshoot_k,
            ),
            comfort=ComfortParameters(raised_offset_k=self.comfort.raised_offset_k),
            dhw=DhwControlParameters(
                setpoint_c=dhw_setpoint_c if dhw_setpoint_c is not None else dhw.setpoint_c,
                hysteresis_k=dhw.hysteresis_k,
                min_c=dhw.min_c,
                rod_power_w=dhw.rod_power_w,
                rod_max_c=dhw.rod_max_c,
                raised_setpoint_c=dhw.raised_setpoint_c,
                forced_setpoint_c=dhw.forced_setpoint_c,
            ),
            slot_hours=slot_hours,
            cooling=CoolingParameters(
                max_cool_w=self.cooling.max_cool_w,
                eer=self.cooling.eer,
                eer_rated_outdoor_c=self.cooling.eer_rated_outdoor_c,
                eer_rated_flow_c=self.cooling.eer_rated_flow_c,
                eer_constant=self.cooling.eer_constant,
                min_flow_c=self.cooling.min_flow_c,
                dewpoint_margin_k=self.cooling.dewpoint_margin_k,
                flow_spread_k=unit.flow_spread_k,
            ),
        )

    def slot_inputs(
        self,
        starts: list[DateTime],
        slot_hours: float,
        outdoor_c: list[Optional[float]],
        ghi_w_per_m2: Optional[list[Optional[float]]] = None,
        fallback_outdoor_c: float = 10.0,
    ) -> list[SlotInputs]:
        """Per-slot exogenous inputs. Missing temperatures are carried forward."""
        result: list[SlotInputs] = []
        last = fallback_outdoor_c
        for i, start in enumerate(starts):
            temp = outdoor_c[i] if i < len(outdoor_c) else None
            if temp is not None:
                last = temp
            ghi = ghi_w_per_m2[i] if ghi_w_per_m2 is not None and i < len(ghi_w_per_m2) else None
            limit = None
            if self.power_limit_w is not None:
                for window in self.power_limit_w.windows:
                    if window.contains(start) and window.value is not None:
                        limit = float(window.value)
                        break
            result.append(
                SlotInputs(
                    outdoor_c=last,
                    comfort_min_c=self.comfort.minimum_at(start),
                    setpoint_c=self.comfort.setpoint_c,
                    comfort_max_c=self.comfort.max_c,
                    ghi_w_per_m2=ghi or 0.0,
                    draw_wh=self.dhw.draw_wh(start, slot_hours),
                    max_electric_w=limit,
                )
            )
        return result
