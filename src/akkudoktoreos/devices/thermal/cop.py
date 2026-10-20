"""Coefficient of performance model for air/water heat pumps.

The COP is modelled as a Carnot efficiency (exergy efficiency) ``eta`` times the
Carnot COP of the heating process:

    COP = eta(T_out, T_flow) * T_flow_K / (T_flow - T_out)

``eta`` is fitted as a linear function of outdoor and flow temperature to the
manufacturer's rating points (EN 14511), e.g. A7/W35, A2/W35, A-7/W35, A7/W55,
A-7/W55. A scale factor lets the calibration align the model with measured
daily COPs (pumps, defrost and controller consumption are part of real COPs).

The module is solver independent and has no EOS dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

KELVIN = 273.15


@dataclass(frozen=True)
class CopPoint:
    """Manufacturer rating point."""

    outdoor_c: float
    flow_c: float
    cop: float


class CopModel:
    """COP as a function of outdoor and flow temperature.

    Args:
        points: At least one rating point. With one point ``eta`` is constant,
            with two or more points a least-squares plane ``eta = a + b*T_out +
            c*T_flow`` is fitted (with fewer than three distinct points the
            missing slopes are zero).
        scale: Multiplicative calibration factor applied to every COP.
        cop_min: Lower bound of the returned COP.
        cop_max: Upper bound of the returned COP.
        min_lift_k: Minimum temperature lift used in the Carnot term; avoids the
            singularity when outdoor and flow temperature get close.
    """

    def __init__(
        self,
        points: Sequence[CopPoint],
        scale: float = 1.0,
        cop_min: float = 1.0,
        cop_max: float = 8.0,
        min_lift_k: float = 10.0,
    ) -> None:
        if not points:
            raise ValueError("At least one COP rating point is required.")
        if scale <= 0:
            raise ValueError("COP scale must be positive.")
        if not 0 < cop_min <= cop_max:
            raise ValueError("Require 0 < cop_min <= cop_max.")
        self.points = list(points)
        self.scale = float(scale)
        self.cop_min = float(cop_min)
        self.cop_max = float(cop_max)
        self.min_lift_k = float(min_lift_k)
        self._coef = self._fit_eta(self.points)

    def _carnot(self, outdoor_c: float, flow_c: float) -> float:
        lift = max(flow_c - outdoor_c, self.min_lift_k)
        return (flow_c + KELVIN) / lift

    def _fit_eta(self, points: Sequence[CopPoint]) -> np.ndarray:
        etas = np.array([p.cop / self._carnot(p.outdoor_c, p.flow_c) for p in points])
        if len(points) == 1:
            return np.array([etas[0], 0.0, 0.0])
        outdoor = np.array([p.outdoor_c for p in points], dtype=float)
        flow = np.array([p.flow_c for p in points], dtype=float)
        columns = [np.ones_like(outdoor)]
        use_outdoor = np.ptp(outdoor) > 0
        use_flow = np.ptp(flow) > 0
        if use_outdoor:
            columns.append(outdoor)
        if use_flow:
            columns.append(flow)
        matrix = np.vstack(columns).T
        solution, *_ = np.linalg.lstsq(matrix, etas, rcond=None)
        coef = [solution[0], 0.0, 0.0]
        index = 1
        if use_outdoor:
            coef[1] = solution[index]
            index += 1
        if use_flow:
            coef[2] = solution[index]
        return np.array(coef, dtype=float)

    def eta(self, outdoor_c: float, flow_c: float) -> float:
        """Exergy efficiency (unscaled)."""
        a, b, c = self._coef
        return float(a + b * outdoor_c + c * flow_c)

    def cop(self, outdoor_c: float, flow_c: float) -> float:
        """COP including the calibration scale, clipped to [cop_min, cop_max]."""
        value = self.scale * self.eta(outdoor_c, flow_c) * self._carnot(outdoor_c, flow_c)
        return float(min(self.cop_max, max(self.cop_min, value)))

    def residuals(self) -> list[float]:
        """Model COP minus rated COP for every rating point (scale=1 reference)."""
        return [
            self.eta(p.outdoor_c, p.flow_c) * self._carnot(p.outdoor_c, p.flow_c) - p.cop
            for p in self.points
        ]


# Weishaupt WBB 12, EN 14511 rating points (installation manual 83315301, 3.4.6.1).
WBB12_RATING_POINTS: tuple[CopPoint, ...] = (
    CopPoint(7.0, 35.0, 4.85),
    CopPoint(2.0, 35.0, 4.11),
    CopPoint(-7.0, 35.0, 3.28),
    CopPoint(7.0, 55.0, 3.10),
    CopPoint(-7.0, 55.0, 2.47),
)
