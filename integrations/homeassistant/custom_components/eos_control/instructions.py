"""Active instructions of the EOS energy management plan (no Home Assistant imports).

``/v1/energy-management/plan`` holds instructions per resource (e.g. ``wbb12_space_heating``,
``wbb12_dhw``, ``waschmaschine``) with an operation mode and a factor, each valid from its
execution time until the next one. The active one is the latest that has started, as in
``EnergyManagementPlan.get_active_instructions`` (mode based instructions run until
replaced).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Optional


def _time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _resource(instruction: Mapping[str, Any]) -> Optional[str]:
    for key in ("resource_id", "id"):
        value = instruction.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def active_instructions(plan: Any, now: datetime) -> dict[str, dict[str, Any]]:
    """Per resource: ``mode`` (lower case), ``factor``, ``since``, ``next_mode``, ``next_change``.

    Only instructions with an operation mode (OMBC, FRBC, DDBC) count.
    """
    if not isinstance(plan, Mapping):
        return {}
    by_resource: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = {}
    for instruction in plan.get("instructions") or []:
        if not isinstance(instruction, Mapping) or not instruction.get("operation_mode_id"):
            continue
        resource = _resource(instruction)
        start = _time(instruction.get("execution_time"))
        if resource is None or start is None:
            continue
        by_resource.setdefault(resource, []).append((start, instruction))
    result: dict[str, dict[str, Any]] = {}
    for resource, items in by_resource.items():
        items.sort(key=lambda item: item[0])
        started = [item for item in items if item[0] <= now]
        if not started:
            continue
        since, current = started[-1]
        mode = str(current["operation_mode_id"]).lower()
        factor = current.get("operation_mode_factor")
        entry: dict[str, Any] = {
            "mode": mode,
            "factor": float(factor) if isinstance(factor, (int, float)) else None,
            "since": since,
            "next_mode": None,
            "next_change": None,
        }
        for start, later in items:
            if start > now and str(later["operation_mode_id"]).lower() != mode:
                entry["next_mode"] = str(later["operation_mode_id"]).lower()
                entry["next_change"] = start
                break
        result[resource] = entry
    return result
