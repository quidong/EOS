"""Heat pump device settings (RL29 fork).

A heat pump device bundles the building it heats, the domestic hot water tank
with an optional heating rod and the heat pump unit itself. The physical models
live in :mod:`akkudoktoreos.devices.thermal`; this module only holds the
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

from pydantic import Field, computed_field, field_validator

from akkudoktoreos.config.configabc import (
    SettingsBaseModel,
    ValueTimeWindowSequence,
)
from akkudoktoreos.devices.settings.devicebasesettings import DevicesBaseSettings
from akkudoktoreos.devices.thermal.building import BuildingParameters
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel, CopPoint
from akkudoktoreos.devices.thermal.dhw import DhwTankParameters
from akkudoktoreos.devices.thermal.heatpump import (
    ComfortParameters,
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
    rod_power_w: float = Field(
        default=0.0, ge=0, json_schema_extra={"description": "Heating rod power [W]; 0 = no rod."}
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
            "description": "Reach legionella_c at least every N days (by the rod); None disables."
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


class HeatPumpOptimizationSettings(SettingsBaseModel):
    """How the optimizer may operate the heat pump and what deviations cost."""

    heating_modes: list[int] = Field(
        default_factory=lambda: [0, 1, 2],
        min_length=1,
        json_schema_extra={
            "description": (
                "Space heating modes the optimizer may choose: 0 REDUCED (SG Ready 1, "
                "blocked), 1 NORMAL (SG Ready 2), 2 RAISED (SG Ready 3), 3 PREHEAT "
                "(SG Ready 3 with all valves forced open; needs the KNX forced position)."
            ),
            "examples": [[0, 1, 2], [0, 1, 2, 3]],
        },
    )
    dhw_modes: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3],
        min_length=1,
        json_schema_extra={
            "description": (
                "Hot water modes the optimizer may choose: 0 HOLD (heat pump controller), "
                "1 HEAT_PUMP (Warmwasser PUSH), 2 ROD_60, 3 ROD_70. Rod modes are dropped "
                "while dhw.rod_power_w is 0."
            ),
        },
    )
    comfort_penalty_amt_per_kh: float = Field(
        default=1.0,
        ge=0,
        json_schema_extra={
            "description": "Cost of a room below its comfort minimum [currency per K*h]."
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

    @field_validator("heating_modes", "dhw_modes")
    @classmethod
    def validate_modes(cls, value: list[int]) -> list[int]:
        if any(mode not in (0, 1, 2, 3) for mode in value):
            raise ValueError("Modes are 0..3.")
        return sorted(set(value))


class HeatPumpCommonSettings(DevicesBaseSettings):
    """Heat pump with building, hot water tank and heating rod (RL29 fork)."""

    building: HeatPumpBuildingSettings = Field(default_factory=HeatPumpBuildingSettings)
    unit: HeatPumpUnitSettings = Field(default_factory=HeatPumpUnitSettings)
    dhw: HeatPumpDhwSettings = Field(default_factory=HeatPumpDhwSettings)
    comfort: HeatPumpComfortSettings = Field(default_factory=HeatPumpComfortSettings)
    optimization: HeatPumpOptimizationSettings = Field(default_factory=HeatPumpOptimizationSettings)
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
        return [
            self.measurement_key_room_temperature,
            self.measurement_key_slab_temperature,
            self.measurement_key_dhw_temperature,
            self.measurement_key_dhw_setpoint,
            self.measurement_key_power_limited,
        ]

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def cop_model(self) -> CopModel:
        points = [CopPoint(p.outdoor_c, p.flow_c, p.cop) for p in self.unit.cop_points]
        return CopModel(points, scale=self.unit.cop_scale)

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
            ),
            comfort=ComfortParameters(raised_offset_k=self.comfort.raised_offset_k),
            dhw=DhwControlParameters(
                setpoint_c=dhw_setpoint_c if dhw_setpoint_c is not None else dhw.setpoint_c,
                hysteresis_k=dhw.hysteresis_k,
                min_c=dhw.min_c,
                rod_power_w=dhw.rod_power_w,
                rod_max_c=dhw.rod_max_c,
            ),
            slot_hours=slot_hours,
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
