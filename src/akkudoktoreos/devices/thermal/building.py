"""Two-node thermal model of a building heated by underfloor heating.

Nodes:

- ``slab``: screed of the underfloor heating plus heating water and buffer tank.
  The heat pump feeds this node.
- ``room``: air, furniture and the inner mass of the building. It loses heat to
  the outside and receives solar and internal gains.

Continuous model (temperatures in degC, heat flows in W, capacities in Wh/K, time in h)::

    C_s dT_s/dt = Q_hp - H_sr (T_s - T_r)
    C_r dT_r/dt = H_sr (T_s - T_r) - H_loss (T_r - T_out) + Q_gain

For a slot of fixed length the inputs are constant, so the model is discretised
exactly with the matrix exponential (zero-order hold) once at construction. A
simulation step is then two multiply-adds, which keeps the optimizer fast.

The module is solver independent and has no EOS dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.linalg import expm


@dataclass(frozen=True)
class BuildingParameters:
    """Parameters of the two-node model."""

    slab_capacity_wh_per_k: float
    """Heat capacity of screed, heating water and buffer [Wh/K]."""

    room_capacity_wh_per_k: float
    """Effective heat capacity of air, furniture and inner building mass [Wh/K]."""

    loss_w_per_k: float
    """Heat loss coefficient room to outside, transmission plus ventilation [W/K]."""

    slab_room_w_per_k: float
    """Heat transfer coefficient underfloor heating to room [W/K]."""

    emitter_w_per_k: float
    """Heat transfer coefficient heating water to screed [W/K]; sets the flow temperature."""

    solar_aperture_m2: float = 0.0
    """Effective solar aperture: solar gain [W] = aperture [m²] x global irradiance [W/m²]."""

    internal_gains_w: float = 0.0
    """Constant internal gains from people and appliances [W]."""

    def validate(self) -> None:
        """Raise ValueError on physically meaningless values."""
        for name in (
            "slab_capacity_wh_per_k",
            "room_capacity_wh_per_k",
            "loss_w_per_k",
            "slab_room_w_per_k",
            "emitter_w_per_k",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.solar_aperture_m2 < 0 or self.internal_gains_w < 0:
            raise ValueError("Gains must not be negative.")


class BuildingModel:
    """Exact discrete-time two-node building model for a fixed slot length."""

    def __init__(self, params: BuildingParameters, slot_hours: float) -> None:
        params.validate()
        if slot_hours <= 0:
            raise ValueError("slot_hours must be positive.")
        self.params = params
        self.slot_hours = float(slot_hours)
        cs, cr = params.slab_capacity_wh_per_k, params.room_capacity_wh_per_k
        hsr, hl = params.slab_room_w_per_k, params.loss_w_per_k
        # dx/dt = A x + B u, x = [T_s, T_r], u = [Q_hp, T_out, Q_gain]
        a = np.array([[-hsr / cs, hsr / cs], [hsr / cr, -(hsr + hl) / cr]])
        b = np.array([[1.0 / cs, 0.0, 0.0], [0.0, hl / cr, 1.0 / cr]])
        # Zero-order hold via the augmented matrix exponential.
        n, m = a.shape[0], b.shape[1]
        aug = np.zeros((n + m, n + m))
        aug[:n, :n] = a * self.slot_hours
        aug[:n, n:] = b * self.slot_hours
        phi = expm(aug)
        self.ad = phi[:n, :n]
        self.bd = phi[:n, n:]
        # Plain floats for the hot path.
        self._a = [[float(v) for v in row] for row in self.ad]
        self._b = [[float(v) for v in row] for row in self.bd]

    def gains_w(self, ghi_w_per_m2: float = 0.0) -> float:
        """Solar plus internal gains [W]."""
        return self.params.internal_gains_w + self.params.solar_aperture_m2 * max(0.0, ghi_w_per_m2)

    def step(
        self, slab_c: float, room_c: float, heat_w: float, outdoor_c: float, gains_w: float
    ) -> tuple[float, float]:
        """Temperatures at the end of one slot for constant inputs."""
        a, b = self._a, self._b
        s = a[0][0] * slab_c + a[0][1] * room_c + b[0][0] * heat_w + b[0][1] * outdoor_c + b[0][2] * gains_w
        r = a[1][0] * slab_c + a[1][1] * room_c + b[1][0] * heat_w + b[1][1] * outdoor_c + b[1][2] * gains_w
        return s, r

    def heat_for_room_target(
        self, slab_c: float, room_c: float, outdoor_c: float, gains_w: float, room_target_c: float
    ) -> float:
        """Constant heat [W] that brings the room to ``room_target_c`` at slot end.

        The result can be negative (room would overshoot without heating) or
        exceed any heat pump limit; callers clip it.
        """
        r0 = self._a[1][0] * slab_c + self._a[1][1] * room_c + self._b[1][1] * outdoor_c + self._b[1][2] * gains_w
        gain = self._b[1][0]
        return (room_target_c - r0) / gain

    def steady_slab_c(self, room_c: float, outdoor_c: float, gains_w: float) -> float:
        """Slab temperature that holds the room at ``room_c`` in steady state."""
        p = self.params
        demand = p.loss_w_per_k * (room_c - outdoor_c) - gains_w
        return room_c + max(0.0, demand) / p.slab_room_w_per_k

    def steady_heat_w(self, room_c: float, outdoor_c: float, gains_w: float) -> float:
        """Heat demand to hold ``room_c`` in steady state [W]."""
        return max(0.0, self.params.loss_w_per_k * (room_c - outdoor_c) - gains_w)

    def flow_temperature_c(self, slab_c: float, heat_w: float, spread_k: float) -> float:
        """Supply (flow) temperature needed to push ``heat_w`` into the slab."""
        mean_water = slab_c + max(0.0, heat_w) / self.params.emitter_w_per_k
        return mean_water + spread_k / 2.0

    def stored_heat_wh(self, slab_c: float, room_c: float, reference_c: float) -> float:
        """Heat stored above ``reference_c`` in both nodes [Wh]."""
        p = self.params
        return p.slab_capacity_wh_per_k * (slab_c - reference_c) + p.room_capacity_wh_per_k * (
            room_c - reference_c
        )
