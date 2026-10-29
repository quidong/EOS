"""Settings for energy management.

Kept in an extra module to avoid cyclic dependencies on package import.
"""

from enum import StrEnum
from typing import Optional

from pydantic import Field, computed_field, field_validator

from akkudoktoreos.config.configabc import SettingsBaseModel, is_home_assistant_addon


class EnergyManagementMode(StrEnum):
    """Energy management mode."""

    DISABLED = "DISABLED"
    PREDICTION = "PREDICTION"
    OPTIMIZATION = "OPTIMIZATION"


def ems_default_mode() -> EnergyManagementMode:
    """Provide default EMS mode.

    Returns OPTIMIZATION when running under Home Assistant, else DISABLED.
    """
    if is_home_assistant_addon():
        return EnergyManagementMode.OPTIMIZATION
    return EnergyManagementMode.DISABLED


class EnergyManagementCommonSettings(SettingsBaseModel):
    """Energy Management Configuration."""

    startup_delay: float = Field(
        default=5,
        ge=1,
        json_schema_extra={
            "description": "Startup delay in seconds for EOS energy management runs."
        },
    )

    interval: float = Field(
        default=300.0,
        ge=60.0,
        json_schema_extra={
            "description": "Intervall between EOS energy management runs [seconds].",
            "examples": ["300"],
        },
    )

    mode: EnergyManagementMode = Field(
        default_factory=ems_default_mode,
        json_schema_extra={
            "description": (
                f"Energy management mode "
                f"[{' | '.join(mode.value for mode in EnergyManagementMode)}]. "
                f"Defaults to {ems_default_mode()}."
            ),
            "examples": ["OPTIMIZATION"],
        },
    )

    # RL29 fork: rolling horizon aligned to the optimization slots.
    run_on_slot_start: bool = Field(
        default=False,
        json_schema_extra={
            "description": (
                "Run the energy management right after every optimization slot starts "
                "(plus slot_start_offset_sec) instead of every `interval` seconds since start-up. "
                "`interval` then is only the polling period (60 s recommended), so the first "
                "slot of every plan is (almost) complete instead of partly elapsed."
            ),
            "examples": [True],
        },
    )

    slot_start_offset_sec: float = Field(
        default=20.0,
        ge=0.0,
        le=600.0,
        json_schema_extra={
            "description": (
                "Delay after the slot start before the aligned run [seconds]; gives the "
                "measurements of the slot boundary time to arrive."
            ),
            "examples": [20.0],
        },
    )

    replan_on_deviation: bool = Field(
        default=False,
        json_schema_extra={
            "description": (
                "RL29 fork: start an extra run between slot starts when the battery SoC or "
                "a heat pump temperature deviates from the plan (polled every `interval`)."
            ),
            "examples": [True],
        },
    )

    replan_soc_deviation_pct: float = Field(
        default=10.0,
        gt=0,
        json_schema_extra={
            "description": "SoC deviation that triggers a run [%-points].",
            "examples": [10.0],
        },
    )

    replan_temperature_deviation_k: float = Field(
        default=0.8,
        gt=0,
        json_schema_extra={
            "description": (
                "Room temperature deviation that triggers a run [K]; hot water uses four times "
                "this value."
            ),
            "examples": [0.8],
        },
    )

    replan_pv_deviation_w: Optional[float] = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "description": (
                "Fork: mean PV power of the last complete slot (PV production meters) "
                "differs from the forecast by more than this [W] -> run. None = off."
            ),
            "examples": [None, 1500.0],
        },
    )

    replan_min_interval_sec: float = Field(
        default=300.0,
        ge=60.0,
        json_schema_extra={
            "description": "Minimum time between two runs [seconds].",
            "examples": [300.0],
        },
    )

    notify_url: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "description": (
                "URL that receives an HTTP POST with a small JSON event after every "
                "completed optimization, so a client can fetch the new solution right "
                "away instead of polling for it. None = off."
            ),
            "examples": [None, "http://127.0.0.1:8080/api/eos/solution-ready"],
        },
    )

    @field_validator("notify_url")
    @classmethod
    def _validate_notify_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value.strip() == "":
            return None
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("notify_url must be an http:// or https:// URL")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def modes(self) -> list[str]:
        """Available energy management modes."""
        return [mode.value for mode in EnergyManagementMode]
