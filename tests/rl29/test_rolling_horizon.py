"""Rolling horizon (RL29 fork): slot-aligned runs and the planned-SoC fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from akkudoktoreos.core import ems as ems_module
from akkudoktoreos.core.ems import slot_run_due
from akkudoktoreos.core.emsettings import EnergyManagementCommonSettings
from akkudoktoreos.optimization.genetic.configrequest import planned_soc_percentage
from akkudoktoreos.utils.datetimeutil import to_datetime

SLOT = 900.0
BASE = 1_800_000_000.0 - (1_800_000_000.0 % SLOT)  # a slot start


@pytest.mark.parametrize(
    ("offset_in_slot", "last", "expected"),
    [
        (5.0, None, None),  # before the offset
        (20.0, None, 0),  # exactly at the offset
        (600.0, None, 0),  # late in the slot: still run
        (25.0, 0, None),  # this slot already ran
        (25.0, -1, 0),  # previous slot ran
    ],
)
def test_slot_run_due(offset_in_slot, last, expected):
    slot0 = int(BASE // SLOT)
    last_slot = None if last is None else slot0 + last
    due = slot_run_due(BASE + offset_in_slot, SLOT, 20.0, last_slot)
    assert due == (None if expected is None else slot0 + expected)


def test_slot_run_due_invalid_slot_length():
    assert slot_run_due(BASE, 0, 20.0, None) is None


def test_settings_defaults_keep_upstream_behaviour():
    settings = EnergyManagementCommonSettings()
    assert settings.run_on_slot_start is False
    assert settings.slot_start_offset_sec == 20.0
    with pytest.raises(ValueError):
        EnergyManagementCommonSettings(slot_start_offset_sec=-1)


@pytest.mark.asyncio
async def test_ems_manage_energy_runs_once_per_slot(monkeypatch):
    calls = []

    class FakeEMS:
        config = SimpleNamespace(
            ems=SimpleNamespace(run_on_slot_start=True, slot_start_offset_sec=20.0),
            optimization=SimpleNamespace(
                algorithm="GENETIC", genetic=SimpleNamespace(interval_sec=900)
            ),
        )

        async def run(self):
            calls.append(1)

    now = {"ts": BASE + 30}
    monkeypatch.setattr(ems_module, "EnergyManagement", FakeEMS)
    monkeypatch.setattr(
        ems_module, "to_datetime", lambda *a, **k: SimpleNamespace(timestamp=lambda: now["ts"])
    )
    monkeypatch.setattr(ems_module, "_aligned_last_slot", None)
    await ems_module.ems_manage_energy()
    await ems_module.ems_manage_energy()  # same slot: skipped
    now["ts"] = BASE + SLOT + 10  # next slot, before the offset
    await ems_module.ems_manage_energy()
    now["ts"] = BASE + SLOT + 21
    await ems_module.ems_manage_energy()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_ems_manage_energy_unaligned_runs_every_call(monkeypatch):
    calls = []

    class FakeEMS:
        config = SimpleNamespace(ems=SimpleNamespace(run_on_slot_start=False))

        async def run(self):
            calls.append(1)

    monkeypatch.setattr(ems_module, "EnergyManagement", FakeEMS)
    await ems_module.ems_manage_energy()
    await ems_module.ems_manage_energy()
    assert len(calls) == 2


def _ems_with_plan(generated_at, soc_values, column="battery1_soc_factor"):
    index = pd.date_range(generated_at.isoformat(), periods=len(soc_values), freq="15min")
    frame = pd.DataFrame({column: soc_values}, index=index)
    solution = SimpleNamespace(
        generated_at=generated_at, solution=SimpleNamespace(to_dataframe=lambda: frame)
    )
    return SimpleNamespace(optimization_solution=lambda: solution)


def test_planned_soc_picks_slot_containing_moment():
    generated = to_datetime("2026-10-26T10:00:00Z")
    ems = _ems_with_plan(generated, [0.40, 0.45, 0.50, 0.55])
    assert planned_soc_percentage(ems, "battery1", to_datetime("2026-10-26T10:20:00Z")) == 45
    assert planned_soc_percentage(ems, "battery1", to_datetime("2026-10-26T10:00:00Z")) == 40


def test_planned_soc_rejects_old_plan_outside_slots_and_bad_values():
    generated = to_datetime("2026-10-26T10:00:00Z")
    ems = _ems_with_plan(generated, [0.40, 0.45])
    # Before the plan starts.
    assert planned_soc_percentage(ems, "battery1", to_datetime("2026-10-26T09:50:00Z")) is None
    # More than one slot after the last slot.
    assert planned_soc_percentage(ems, "battery1", to_datetime("2026-10-26T10:40:00Z")) is None
    # Plan made after the moment (another run's state) or older than three hours.
    assert planned_soc_percentage(ems, "battery1", to_datetime("2026-10-26T09:58:00Z")) is None
    assert planned_soc_percentage(ems, "battery1", to_datetime("2026-10-26T13:10:00Z")) is None
    # Unknown device, broken value, no solution, broken EMS.
    assert planned_soc_percentage(ems, "other", to_datetime("2026-10-26T10:10:00Z")) is None
    bad = _ems_with_plan(generated, [1.5, float("nan")])
    assert planned_soc_percentage(bad, "battery1", to_datetime("2026-10-26T10:05:00Z")) is None
    assert (
        planned_soc_percentage(
            SimpleNamespace(optimization_solution=lambda: None), "battery1", generated
        )
        is None
    )
    assert planned_soc_percentage(SimpleNamespace(), "battery1", generated) is None
