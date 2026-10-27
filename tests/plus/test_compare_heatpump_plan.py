"""Plan-vs-actual comparison of the heat pump (scripts/plus/compare_heatpump_plan.py)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "plus" / "compare_heatpump_plan.py"
spec = importlib.util.spec_from_file_location("compare_heatpump_plan", SCRIPT)
assert spec and spec.loader
script = importlib.util.module_from_spec(spec)
sys.modules["compare_heatpump_plan"] = script
spec.loader.exec_module(script)

T0 = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)


def rows(n: int = 6, offset_k: float = 0.3, factor: float = 1.2):
    out = []
    for i in range(n):
        mode = 4 if i < 2 else 1
        plan_energy = 1800.0 if mode == 4 else 200.0
        out.append(
            script.Row(
                timestamp=T0 + timedelta(hours=i),
                mode=float(mode),
                plan_room_c=21.0 + 0.1 * i,
                plan_energy_wh=plan_energy,
                # actual room of hour i is the plan of hour i-1 plus the offset
                room_c=(21.0 + 0.1 * (i - 1) + offset_k) if i > 0 else 20.9,
                electric_w=plan_energy * factor,
            )
        )
    return out


def test_room_bias_and_error():
    report = script.compare(rows(offset_k=0.3))
    assert report.room_pairs == 5
    assert report.room_bias_k == pytest.approx(0.3)
    assert report.room_mae_k == pytest.approx(0.3)


def test_energy_totals_and_per_mode():
    report = script.compare(rows(factor=1.2))
    assert report.plan_energy_kwh == pytest.approx(2 * 1.8 + 4 * 0.2)
    assert report.energy_ratio == pytest.approx(1.2)
    count, plan, actual = report.per_mode[4]
    assert count == 2 and plan == pytest.approx(3.6) and actual == pytest.approx(4.32)


def test_missing_values_are_skipped():
    data = rows()
    data[3].electric_w = None
    data[4].room_c = None
    report = script.compare(data)
    assert report.energy_hours == 5
    assert report.room_pairs == 4


def test_csv_roundtrip_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "plan_ist.csv"
    script.write_csv(path, rows())
    assert len(script.read_csv(path)) == 6
    assert script.main(["compare", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Raumtemperatur (5 h)" in out
    assert "Forciert: 2 h" in out


def test_empty_input():
    report = script.compare([])
    assert report.room_mae_k is None and report.energy_ratio is None
    assert "keine Paare" in "\n".join(report.lines())
