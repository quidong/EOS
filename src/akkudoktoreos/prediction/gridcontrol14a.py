"""§14a EnWG grid fee modules for controllable consumers (RL29 fork).

Controllable consumers (heat pumps, wallboxes, batteries) in Germany choose
one of these grid fee modules:

- ``none``: no §14a participation (installations before 2024 without
  controllable device): no fee change, no dimming.
- ``module1``: flat reduction of the yearly network fee. Does not depend on
  the time of use, so it does not change the optimization. Dimming applies.
- ``module2``: the controllable device has its own meter and its network
  energy price is reduced by a percentage (60 %). The device then cannot use
  PV or battery energy of the household meter. Dimming applies.
- ``module1_3``: module 1 plus time-variable network energy prices (low,
  standard, high bands, only in some quarters) on the household meter.
  Dimming applies.

The configured consumption fee (``elecfee.elecfeefixed.consumption_amt_kwh``)
keeps the **regular** network energy price. Module 3 replaces it by the band
price: the difference ``band - regular`` is added per time slot. Module 2
lowers the price of the separately metered device by
``regular x module2_reduction_percent``.

All amounts are net (before VAT) in currency per kWh.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from akkudoktoreos.config.configabc import SettingsBaseModel, ValueTimeWindowSequence
from akkudoktoreos.utils.datetimeutil import DateTime

GridModule14a = Literal["none", "module1", "module2", "module1_3"]


class GridControl14aSettings(SettingsBaseModel):
    """§14a EnWG grid fee module and dimming of controllable consumers."""

    module: GridModule14a = Field(
        default="none",
        json_schema_extra={
            "description": (
                "Grid fee module: 'none', 'module1' (flat reduction), 'module2' (separate "
                "meter, reduced network energy price), 'module1_3' (module 1 plus "
                "time-variable network energy price)."
            ),
            "examples": ["none", "module1", "module2", "module1_3"],
        },
    )
    controllable_device_ids: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "description": (
                "Device ids registered as controllable consumers (e.g. the heat pump). "
                "They are dimmed on a grid operator signal; with module 2 they are "
                "separately metered."
            ),
            "examples": [["wbb12"]],
        },
    )
    regular_network_amt_kwh: Optional[float] = Field(
        default=None,
        ge=0,
        json_schema_extra={
            "description": (
                "Regular network energy price contained in the configured consumption "
                "fee [currency/kWh, net]. Needed for module 2 and module 3."
            ),
            "examples": [0.075],
        },
    )
    module2_reduction_percent: float = Field(
        default=60.0,
        ge=0,
        le=100,
        json_schema_extra={
            "description": "Module 2: reduction of the network energy price of the device [%]."
        },
    )
    module3_network_amt_kwh: ValueTimeWindowSequence = Field(
        default_factory=ValueTimeWindowSequence,
        json_schema_extra={
            "description": (
                "Module 3: network energy price by time window [currency/kWh, net]; use "
                "'month' to restrict windows to the quarters with module 3 prices, with one "
                "window per band and month (the example shows January; repeat it for the "
                "other months). Times without a window keep the regular network energy price."
            ),
            "examples": [
                {
                    "windows": [
                        {"start_time": start, "end_time": end, "month": month, "value": value}
                        for month in (1,)
                        for start, end, value in (
                            ("00:00", "02:00", 0.0432),
                            ("02:00", "06:00", 0.0173),
                            ("06:00", "17:00", 0.0432),
                            ("17:00", "20:00", 0.0596),
                            ("20:00", "00:00", 0.0432),
                        )
                    ]
                }
            ],
        },
    )
    dimming_power_w: float = Field(
        default=4200.0,
        gt=0,
        json_schema_extra={
            "description": (
                "Power a controllable device may still draw while dimmed [W] (4.2 kW, or "
                "0.4 x connected power for larger devices)."
            )
        },
    )
    dimming_hold_hours: float = Field(
        default=1.0,
        gt=0,
        json_schema_extra={
            "description": (
                "While a dimming signal is active, assume it lasts this long from now [h]."
            )
        },
    )

    @model_validator(mode="after")
    def validate_module_prices(self) -> "GridControl14aSettings":
        if self.module in ("module2", "module1_3") and self.regular_network_amt_kwh is None:
            raise ValueError(f"{self.module} needs regular_network_amt_kwh.")
        if self.module == "module1_3" and not self.module3_network_amt_kwh.windows:
            raise ValueError("module1_3 needs module3_network_amt_kwh windows.")
        return self

    # ------------------------------------------------------------------

    @property
    def dims(self) -> bool:
        """Controllable devices are dimmed on a grid operator signal."""
        return self.module != "none"

    def is_controllable(self, device_id: str) -> bool:
        return self.dims and device_id in self.controllable_device_ids

    def separately_metered(self, device_id: str) -> bool:
        """Module 2: the device has its own meter."""
        return self.module == "module2" and device_id in self.controllable_device_ids

    def module3_delta_amt_kwh(self, dt: DateTime) -> float:
        """Module 3 band price minus regular network price at ``dt`` [currency/kWh, net]."""
        if self.module != "module1_3" or self.regular_network_amt_kwh is None:
            return 0.0
        for window in self.module3_network_amt_kwh.windows:
            if window.contains(dt) and window.value is not None:
                return float(window.value) - self.regular_network_amt_kwh
        return 0.0

    def module2_reduction_amt_kwh(self) -> float:
        """Module 2 reduction of the device's energy price [currency/kWh, net]."""
        if self.module != "module2" or self.regular_network_amt_kwh is None:
            return 0.0
        return self.regular_network_amt_kwh * self.module2_reduction_percent / 100.0
