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

from typing import Any, Optional

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
        default=22.0,
        ge=0,
        json_schema_extra={
            "description": (
                "Power while the heat pump is ready but not running: controller, outdoor "
                "unit, circulation pump [W]."
            )
        },
    )
    off_w: Optional[float] = Field(
        default=None,
        ge=0,
        json_schema_extra={
            "description": (
                "RL29 fork: power in the heat pump's own standby operating mode (frost "
                "protection, pumps off), used in holiday mode [W]; None = standby_w."
            ),
            "examples": [10.0, None],
        },
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
    rod_power_steps_w: Optional[list[float]] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Fork: the rod switches in fixed power steps (relay stages), e.g. "
                "[1000, 2000, 3000] W. In surplus mode it uses the highest step the "
                "surplus covers. None = stepless (e.g. a PV power diverter)."
            ),
            "examples": [None, [1000, 2000, 3000]],
        },
    )
    rod_surplus_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Fork: the rod has its own surplus controller (e.g. a PV power diverter "
                "reading the grid meter) that heats with PV surplus up to this temperature "
                "[degC]. EOS models it: the rod uses the forecast surplus left after the "
                "heat pump, so EOS plans no heat pump charge the rod covers anyway. A "
                "planned ROD slot (negative prices, legionella) is a boost from the grid "
                "that the controller has to start. None = the rod runs only as planned."
            ),
            "examples": [None, 60.0],
        },
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
        json_schema_extra={
            "description": (
                "Hot water tapped per hour of day, 24 values [Wh]; Monday to Friday when "
                "draw_profile_weekend_wh is set. Prior of the learned profile (calibration)."
            )
        },
    )
    draw_profile_weekend_wh: Optional[list[float]] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Hot water tapped per hour on Saturdays and Sundays, 24 values [Wh]; None = "
                "draw_profile_wh holds every day."
            )
        },
    )

    @field_validator("draw_profile_wh", "draw_profile_weekend_wh")
    @classmethod
    def validate_profile(cls, value: Optional[list[float]]) -> Optional[list[float]]:
        if value is None:
            return value
        if len(value) != 24 or any(v < 0 for v in value):
            raise ValueError("A draw profile needs 24 non-negative hourly values.")
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
        """Tapped energy in the slot starting at ``start`` [Wh] (local hour and weekday)."""
        profile = self.draw_profile_wh
        if self.draw_profile_weekend_wh is not None and start.weekday() >= 5:
            profile = self.draw_profile_weekend_wh
        return profile[start.hour] * slot_hours


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


class HeatPumpAwaySettings(SettingsBaseModel):
    """Holiday mode (RL29 fork): reduced comfort until the return, then back on time.

    The controller reports ``<device_id>-away`` (1 = away) and optionally
    ``<device_id>-return-ts`` (Unix time of the return, 0 = unknown) and
    ``<device_id>-away-room-temperature`` (room temperature to hold while away,
    e.g. the reduced setpoint of the heating circuit). Until the return the
    comfort minimum and the NORMAL setpoint are the away temperature and hot water
    can be switched off; from the return on the normal comfort band and hot water
    minimum apply again. The optimizer therefore heats the building and the tank
    up before the return in the cheapest hours (PREHEAT/FORCED, HEAT_PUMP), as
    soon as the return lies within the horizon. With an unknown return the whole
    horizon counts as away.
    """

    room_c: float = Field(
        default=20.0,
        json_schema_extra={
            "description": (
                "Room temperature held while away [degC]; overridden by the measurement "
                "'<device_id>-away-room-temperature'."
            ),
            "examples": [20.0, 18.0],
        },
    )
    dhw_off: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "Hot water is switched off while away: no reheating by the heat pump "
                "controller, no tank minimum, no tapping; the tank is charged back to the "
                "setpoint before the return."
            )
        },
    )
    standby_min_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "Lowest room temperature while away when the controller puts the heat pump "
                "into standby for REDUCED [degC]; overridden by the measurement "
                "'<device_id>-away-min-temperature'. REDUCED then lets the house cool down to "
                "this floor, NORMAL holds the away temperature. None = the away temperature "
                "is the floor."
            ),
            "examples": [16.0, None],
        },
    )
    standby_exit_dhw_c: Optional[float] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "With standby (standby_min_c set): a restart of the heat pump from standby "
                "charges the tank to this temperature on its own [degC]. None = no charge "
                "on restart (e.g. the controller lowers the hot water setpoints while away, "
                "so heating out of standby produces no hot water)."
            ),
            "examples": [41.0, None],
        },
    )
    internal_gains_share: float = Field(
        default=1.0,
        ge=0,
        le=1,
        json_schema_extra={
            "description": (
                "Share of building.internal_gains_w present while away (people, cooking, "
                "devices are gone; fridge and standby stay) [-]."
            ),
            "examples": [0.4, 1.0],
        },
    )
    cooling_max_c: float = Field(
        default=28.0,
        json_schema_extra={
            "description": "Cooling season: room temperature tolerated while away [degC]."
        },
    )
    max_age_hours: float = Field(
        default=48.0,
        gt=0,
        json_schema_extra={
            "description": "An away measurement older than this is ignored (treated as at home) [h]."
        },
    )


def _default_setback_windows() -> ValueTimeWindowSequence:
    """Nights every day, working-day mornings until noon (local time)."""
    windows: list[dict[str, Any]] = [{"start_time": "22:00", "duration": "2 hours"}]
    for day in range(7):
        windows.append(
            {
                "start_time": "00:00",
                "duration": "12 hours" if day < 5 else "8 hours",
                "day_of_week": day,
            }
        )
    return ValueTimeWindowSequence.model_validate({"windows": windows})


class HeatPumpZoneSetbackSettings(SettingsBaseModel):
    """Setback of one zone of the building (RL29 fork), e.g. the bedrooms upstairs.

    EOS models the building as one zone; the measured room temperature is the
    mean of all rooms. While the zone is set back, its rooms only need the setback
    temperature (``<device_id>-setback-room-temperature``, e.g. the reduced
    setpoint of the heating circuit, fallback ``room_c``). The comfort minimum and
    the NORMAL setpoint of the whole house drop by the zone's ``share`` of the
    difference, so EOS neither plans to heat the zone up against the controller
    nor counts the cooler zone as a comfort violation. The solution column
    ``<device_id>_zone_setback`` (1 = set back) tells the controller when to lower
    the zone's room setpoints.
    """

    enabled: bool = Field(
        default=False,
        json_schema_extra={"description": "Lower the zone in the configured windows."},
    )
    share: float = Field(
        default=0.4,
        ge=0,
        le=1,
        json_schema_extra={
            "description": (
                "Share of the zone in the measured mean room temperature (rooms of the zone "
                "/ all rooms), e.g. 6 of 15 rooms = 0.4."
            ),
            "examples": [0.4],
        },
    )
    room_c: float = Field(
        default=20.0,
        json_schema_extra={
            "description": (
                "Zone temperature while set back [degC]; overridden by the measurement "
                "'<device_id>-setback-room-temperature'."
            ),
            "examples": [20.0],
        },
    )
    min_offset_k: float = Field(
        default=0.5,
        ge=0,
        json_schema_extra={
            "description": "The zone may fall this far below its setback temperature [K]."
        },
    )
    windows: ValueTimeWindowSequence = Field(
        default_factory=_default_setback_windows,
        json_schema_extra={
            "description": (
                "When the zone is set back (local time); default nights 22-24 and 0-12 on "
                "working days, 0-8 at weekends."
            )
        },
    )

    def active_at(self, dt: DateTime) -> bool:
        return self.enabled and self.windows.contains(dt)


def _default_ventilation_profile() -> list[float]:
    """Open-window share x hours per hour of day: airing in the morning and evening."""
    profile = [0.0] * 24
    profile[7] = 0.1  # e.g. 40 % of the windows for 15 minutes
    profile[21] = 0.1
    return profile


class HeatPumpVentilationSettings(SettingsBaseModel):
    """Window ventilation (RL29 fork).

    The controller reports ``<device_id>-window-open``: the share of the monitored
    windows that are open (0..1). Airing removes heat with ``loss_w_per_k`` times
    the open share times (room - outdoor temperature). The expected airing per
    hour of day is the profile (open share x hours); the calibration learns it
    from the window measurement and takes the measured airing out of the building
    balance, so ``building.loss_w_per_k`` stays the closed-window loss. Slots with
    open windows are left out of the plan-versus-actual room comparison.

    With ``recommend`` EOS picks one slot per day in the midday window
    (warmest outdoor temperature, ties by PV surplus) and plans the airing there;
    the solution column ``<device_id>_ventilate`` is 1 in that slot, for a
    notification.
    """

    enabled: bool = Field(
        default=False,
        json_schema_extra={"description": "Model window ventilation."},
    )
    loss_w_per_k: float = Field(
        default=1000.0,
        ge=0,
        json_schema_extra={
            "description": (
                "Heat loss while all monitored windows are wide open [W/K] (air exchange; "
                "about 0.34 Wh/m3K x air volume x exchanges per hour)."
            ),
            "examples": [1000.0, 600.0],
        },
    )
    profile: list[float] = Field(
        default_factory=_default_ventilation_profile,
        json_schema_extra={
            "description": (
                "Expected airing per hour of day: open share x hours, 24 values (0.1 = 40 % "
                "of the windows for 15 minutes); Monday to Friday when profile_weekend is "
                "set. Prior of the learned profile."
            )
        },
    )
    profile_weekend: Optional[list[float]] = Field(
        default=None,
        json_schema_extra={
            "description": "Expected airing on Saturdays and Sundays; None = profile."
        },
    )
    recommend: bool = Field(
        default=False,
        json_schema_extra={"description": "Recommend one airing slot per day around midday."},
    )
    recommend_start_hour: int = Field(default=11, ge=0, le=23)
    recommend_end_hour: int = Field(
        default=15,
        ge=1,
        le=24,
        json_schema_extra={"description": "End (exclusive) of the recommendation window [h]."},
    )
    recommend_open_hours: float = Field(
        default=0.15,
        ge=0,
        json_schema_extra={
            "description": "Airing planned in the recommended slot: open share x hours.",
            "examples": [0.15],
        },
    )

    @field_validator("profile", "profile_weekend")
    @classmethod
    def validate_profile(cls, value: Optional[list[float]]) -> Optional[list[float]]:
        if value is None:
            return value
        if len(value) != 24 or any(v < 0 or v > 1 for v in value):
            raise ValueError("A ventilation profile needs 24 values between 0 and 1.")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "HeatPumpVentilationSettings":
        if self.recommend_end_hour <= self.recommend_start_hour:
            raise ValueError("recommend_end_hour must be after recommend_start_hour.")
        return self

    def open_hours(self, start: DateTime) -> float:
        """Expected open share x hours in the hour of ``start`` (local hour and weekday)."""
        profile = self.profile
        if self.profile_weekend is not None and start.weekday() >= 5:
            profile = self.profile_weekend
        return profile[start.hour]

    def slot_loss_w_per_k(self, start: DateTime) -> float:
        """Mean extra loss of the slot from airing [W/K]."""
        return self.loss_w_per_k * self.open_hours(start) if self.enabled else 0.0


class HeatPumpRoomLimitsSettings(SettingsBaseModel):
    """Per-room limits reported by the controller (fork).

    EOS models one room node, the mean of all rooms. Single rooms can be colder or
    warmer than the mean. The controller knows the rooms and their own limits and
    reports two margins (rooms with open windows or just aired left out):

    - ``<device_id>-room-min-margin``: smallest ``room - lower limit of the room`` [K];
    - ``<device_id>-room-max-margin``: smallest ``upper limit of the room - room`` [K].

    For the first ``horizon_hours`` the rooms are assumed to move with the mean: the
    comfort minimum of the mean becomes at least ``room now - min margin`` (at most
    ``max_raise_k`` above the band and not above the NORMAL setpoint), the comfort
    maximum and the RAISED target at most ``room now + max margin`` (not below the
    NORMAL setpoint). Both measurements are optional.
    """

    enabled: bool = Field(
        default=False,
        json_schema_extra={"description": "Use the room margins reported by the controller."},
    )
    horizon_hours: float = Field(
        default=6.0,
        gt=0,
        json_schema_extra={
            "description": "How long the measured margins apply from now [h].",
            "examples": [6.0],
        },
    )
    max_raise_k: float = Field(
        default=2.0,
        ge=0,
        json_schema_extra={
            "description": (
                "Largest increase of the comfort minimum by a cold room [K]; limits the "
                "effect of a room that is cold by design."
            ),
            "examples": [2.0],
        },
    )


class HeatPumpHumiditySettings(SettingsBaseModel):
    """Humidity protection (fork).

    While the relative humidity ``<device_id>-humidity`` (the highest of the
    monitored rooms, %) is at or above ``max_pct``, REDUCED must not cool the rooms
    down (cold walls with high humidity grow mould): for the next ``hold_hours`` the
    comfort minimum is at most ``max_drop_k`` below the NORMAL setpoint. A debounced
    flag from the controller (100 = high, 0 = normal) works as well.
    """

    enabled: bool = Field(
        default=False,
        json_schema_extra={"description": "Hold the room temperature while humidity is high."},
    )
    max_pct: float = Field(
        default=65.0,
        gt=0,
        le=100,
        json_schema_extra={
            "description": "Relative humidity from which the rooms are held warm [%].",
            "examples": [65.0, 70.0],
        },
    )
    hold_hours: float = Field(
        default=2.0,
        gt=0,
        json_schema_extra={"description": "How long a high reading applies from now [h]."},
    )
    max_drop_k: float = Field(
        default=0.5,
        ge=0,
        json_schema_extra={
            "description": "Allowed drop below the NORMAL setpoint while humidity is high [K]."
        },
    )


class HeatPumpWeatherWarningSettings(SettingsBaseModel):
    """Heat reserve before a weather warning (fork).

    A storm, ice or frost warning can mean a power cut. The controller reports the
    warned period as ``<device_id>-warning-start-ts`` and ``<device_id>-warning-end-ts``
    (Unix times, 0 = no warning; e.g. from the DWD warnings in Home Assistant).
    For the slots in this period the comfort minimum rises by ``raise_room_k`` (at most
    to the comfort maximum) and the hot water minimum to ``dhw_min_c``, so EOS charges
    the building and the tank beforehand, in the cheapest hours before the warning.
    While away with hot water off only the room part applies.
    """

    enabled: bool = Field(
        default=False,
        json_schema_extra={"description": "Build up a heat reserve before weather warnings."},
    )
    raise_room_k: float = Field(
        default=1.0,
        ge=0,
        le=3,
        json_schema_extra={
            "description": "Raise of the comfort minimum during the warning [K].",
            "examples": [1.0],
        },
    )
    dhw_min_c: Optional[float] = Field(
        default=50.0,
        json_schema_extra={
            "description": (
                "Hot water minimum during the warning [degC]; None = unchanged. At most "
                "what the heat pump reaches."
            ),
            "examples": [50.0, None],
        },
    )
    max_hours: float = Field(
        default=48.0,
        gt=0,
        json_schema_extra={
            "description": "Longest warning period that is applied, from its start [h].",
            "examples": [48.0],
        },
    )


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
    fit_draw_profile: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "Learn dhw.draw_profile_wh (and a weekend profile) from the tank temperature; "
                "takes over once draw_min_days usable days exist."
            )
        },
    )
    draw_min_days: int = Field(
        default=14,
        ge=3,
        json_schema_extra={
            "description": "Usable days before the learned draw profile replaces the configured one.",
            "examples": [14],
        },
    )
    draw_quantile: Optional[float] = Field(
        default=None,
        gt=0,
        lt=1,
        json_schema_extra={
            "description": (
                "Statistic of the draw per hour over the days: None = mean (right for "
                "tapping at varying times), else this quantile."
            ),
            "examples": [None, 0.5],
        },
    )
    fit_ventilation_profile: bool = Field(
        default=True,
        json_schema_extra={
            "description": (
                "Learn ventilation.profile (and a weekend profile) from '<id>-window-open' "
                "once 14 days with window data exist; needs ventilation.enabled."
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
            draw_min_days=self.draw_min_days,
            draw_quantile=self.draw_quantile,
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
    away: HeatPumpAwaySettings = Field(default_factory=HeatPumpAwaySettings)
    zone_setback: HeatPumpZoneSetbackSettings = Field(default_factory=HeatPumpZoneSetbackSettings)
    ventilation: HeatPumpVentilationSettings = Field(default_factory=HeatPumpVentilationSettings)
    room_limits: HeatPumpRoomLimitsSettings = Field(default_factory=HeatPumpRoomLimitsSettings)
    humidity: HeatPumpHumiditySettings = Field(default_factory=HeatPumpHumiditySettings)
    weather_warning: HeatPumpWeatherWarningSettings = Field(
        default_factory=HeatPumpWeatherWarningSettings
    )
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
        keys += [
            self.measurement_key_away,
            self.measurement_key_return_ts,
            self.measurement_key_away_room_temperature,
            self.measurement_key_away_min_temperature,
            self.measurement_key_reduce_lock_until,
        ]
        if self.zone_setback.enabled:
            keys.append(self.measurement_key_setback_room_temperature)
        if self.ventilation.enabled:
            keys.append(self.measurement_key_window_open)
        if self.room_limits.enabled:
            keys += [self.measurement_key_room_min_margin, self.measurement_key_room_max_margin]
        if self.humidity.enabled:
            keys.append(self.measurement_key_humidity)
        if self.weather_warning.enabled:
            keys += [self.measurement_key_warning_start, self.measurement_key_warning_end]
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
            "window_open": self.measurement_key_window_open,
            "away": self.measurement_key_away,
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_cooling_season(self) -> str:
        """1 = cooling season (the controller has switched the system to cooling)."""
        return f"{self.device_id}-cooling-season"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_away(self) -> str:
        """1 = holiday mode (nobody at home until the return)."""
        return f"{self.device_id}-away"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_return_ts(self) -> str:
        """Unix time of the return from holiday, 0 = unknown."""
        return f"{self.device_id}-return-ts"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_away_room_temperature(self) -> str:
        """Room temperature to hold while away [degC]."""
        return f"{self.device_id}-away-room-temperature"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_away_min_temperature(self) -> str:
        """Room temperature floor while away with the heat pump in standby [degC]."""
        return f"{self.device_id}-away-min-temperature"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_reduce_lock_until(self) -> str:
        """Unix time until which the controller does not reduce, 0 = no lock (fork)."""
        return f"{self.device_id}-reduce-lock-until-ts"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_setback_room_temperature(self) -> str:
        """Temperature of the set-back zone [degC] (zone setback)."""
        return f"{self.device_id}-setback-room-temperature"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_window_open(self) -> str:
        """Share of the monitored windows that are open, 0..1 (ventilation)."""
        return f"{self.device_id}-window-open"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_dewpoint(self) -> str:
        """Room dew point [degC]."""
        return f"{self.device_id}-dewpoint"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_room_min_margin(self) -> str:
        """Smallest margin of a room above its lower limit [K] (room limits)."""
        return f"{self.device_id}-room-min-margin"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_room_max_margin(self) -> str:
        """Smallest margin of a room below its upper limit [K] (room limits)."""
        return f"{self.device_id}-room-max-margin"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_humidity(self) -> str:
        """Highest relative humidity of the monitored rooms [%] (humidity protection)."""
        return f"{self.device_id}-humidity"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_warning_start(self) -> str:
        """Unix time a weather warning starts, 0 = none (weather warning)."""
        return f"{self.device_id}-warning-start-ts"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_key_warning_end(self) -> str:
        """Unix time a weather warning ends, 0 = none (weather warning)."""
        return f"{self.device_id}-warning-end-ts"

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
            draw_profile_wh=tuple(self.dhw.draw_profile_wh),
            draw_profile_weekend_wh=tuple(self.dhw.draw_profile_weekend_wh or ()),
            ventilation_w_per_k=(
                self.ventilation.loss_w_per_k if self.ventilation.enabled else 0.0
            ),
            ventilation_profile=tuple(self.ventilation.profile),
            ventilation_profile_weekend=tuple(self.ventilation.profile_weekend or ()),
        )

    def calibrated(self, result: Optional[CalibrationResult]) -> "HeatPumpCommonSettings":
        """A copy of these settings with the fitted parameters applied (RL29 fork).

        The configuration itself is never changed: it stays the prior of the
        next fit. Missing fits keep the configured value.
        """
        if result is None or not (
            result.building
            or result.cop
            or result.dhw
            or result.emitter
            or result.draw
            or result.ventilation
        ):
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
        if result.draw:
            data["dhw"]["draw_profile_wh"] = list(result.draw.profile_wh)
            if result.draw.weekend_wh is not None:
                data["dhw"]["draw_profile_weekend_wh"] = list(result.draw.weekend_wh)
        if result.ventilation:
            data["ventilation"]["profile"] = list(result.ventilation.profile)
            if result.ventilation.weekend is not None:
                data["ventilation"]["profile_weekend"] = list(result.ventilation.weekend)
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
                off_w=unit.off_w,
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
                rod_surplus_c=dhw.rod_surplus_c,
                rod_power_steps_w=(
                    tuple(sorted(float(w) for w in dhw.rod_power_steps_w if w > 0))
                    if dhw.rod_power_steps_w
                    else None
                ),
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
                    ventilation_w_per_k=self.ventilation.slot_loss_w_per_k(start),
                )
            )
        return result
