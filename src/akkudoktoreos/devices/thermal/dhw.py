"""Domestic hot water tank model.

Single-node tank: the stored water is treated as fully mixed. Losses are
proportional to the temperature difference to the room the tank stands in,
tapping removes a given amount of energy per slot.

    C dT/dt = Q_in - UA (T - T_amb) - P_draw

The module is solver independent and has no EOS dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

WATER_WH_PER_LITRE_K = 1.163


@dataclass(frozen=True)
class DhwTankParameters:
    """Parameters of the hot water tank."""

    volume_l: float
    """Usable water volume [l]."""

    loss_w_per_k: float
    """Standby loss coefficient [W/K]; ~1.5 W/K for a well insulated 200 l tank."""

    ambient_c: float = 18.0
    """Temperature of the room the tank stands in [degC]."""

    def validate(self) -> None:
        """Raise ValueError on physically meaningless values."""
        if self.volume_l <= 0:
            raise ValueError("volume_l must be positive.")
        if self.loss_w_per_k < 0:
            raise ValueError("loss_w_per_k must not be negative.")

    @property
    def capacity_wh_per_k(self) -> float:
        """Heat capacity of the stored water [Wh/K]."""
        return self.volume_l * WATER_WH_PER_LITRE_K


class DhwTank:
    """Hot water tank with exact exponential loss over a slot."""

    def __init__(self, params: DhwTankParameters, slot_hours: float) -> None:
        params.validate()
        if slot_hours <= 0:
            raise ValueError("slot_hours must be positive.")
        self.params = params
        self.slot_hours = float(slot_hours)
        self.capacity = params.capacity_wh_per_k
        rate = params.loss_w_per_k / self.capacity  # 1/h
        self._decay = math.exp(-rate * self.slot_hours)
        # Fraction of a constant net heat input that stays in the tank over one
        # slot, relative to dt/C (exact solution of the linear ODE).
        self._input_gain = (
            (1.0 - self._decay) / params.loss_w_per_k
            if params.loss_w_per_k > 0
            else self.slot_hours / self.capacity
        )

    def step(self, temp_c: float, heat_w: float, draw_wh: float) -> float:
        """Tank temperature at the end of the slot."""
        p = self.params
        draw_w = draw_wh / self.slot_hours
        equilibrium_shift = p.ambient_c
        return (
            equilibrium_shift
            + (temp_c - equilibrium_shift) * self._decay
            + (heat_w - draw_w) * self._input_gain
        )

    def heat_to_reach(self, temp_c: float, target_c: float, draw_wh: float) -> float:
        """Constant heat [W] that brings the tank to ``target_c`` at the end of the slot."""
        p = self.params
        draw_w = draw_wh / self.slot_hours
        free = p.ambient_c + (temp_c - p.ambient_c) * self._decay - draw_w * self._input_gain
        return (target_c - free) / self._input_gain

    def stored_heat_wh(self, temp_c: float, reference_c: float) -> float:
        """Heat stored above ``reference_c`` [Wh]."""
        return self.capacity * (temp_c - reference_c)
