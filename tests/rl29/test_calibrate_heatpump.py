"""Calibration script: parameters are recovered from synthetic measurements."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rl29" / "calibrate_heatpump.py"
spec = importlib.util.spec_from_file_location("calibrate_heatpump", SCRIPT)
assert spec and spec.loader
cal = importlib.util.module_from_spec(spec)
sys.modules["calibrate_heatpump"] = cal  # dataclasses resolve their module
spec.loader.exec_module(cal)

H_TRUE = 180.0
GAINS_TRUE = 300.0
SCALE_TRUE = 0.7
UA_TRUE = 1.6
VOLUME = 190.0
AMBIENT = 18.0


def synthetic_hours(days: int = 10) -> list:
    cop = CopModel(WBB12_RATING_POINTS, scale=SCALE_TRUE)
    capacity = VOLUME * cal.WATER_WH_PER_LITRE_K
    start = datetime(2026, 10, 11, tzinfo=timezone.utc)
    hours = []
    dhw = 46.0
    for i in range(days * 24):
        t = start + timedelta(hours=i)
        day = i // 24
        outdoor = 4.0 + day * 1.2 + 3.0 * math.sin((t.hour - 9) / 24 * 2 * math.pi)
        room = 21.0
        heat = H_TRUE * (room - outdoor) - GAINS_TRUE
        flow = 26.0 + heat / 1000.0
        electric = heat / cop.cop(outdoor, flow)
        dhw_heat = 0.0
        if 0 <= t.hour <= 5:
            dhw = AMBIENT + (dhw - AMBIENT) * math.exp(-UA_TRUE / capacity)
        elif t.hour == 6:
            dhw_heat = (46.0 - dhw) * capacity  # one hour charge back to 46
            dhw = 46.0
        hours.append(
            cal.Hour(
                t,
                {
                    "outdoor_c": outdoor,
                    "room_c": room,
                    "heat_w": heat + dhw_heat,
                    "electric_w": electric,
                    "flow_c": flow,
                    "dhw_c": dhw,
                    "dhw_heat_w": dhw_heat,
                },
            )
        )
    return hours


def test_fit_building_loss_recovers_parameters():
    result = cal.fit_building_loss(synthetic_hours())
    assert result is not None
    assert result["loss_w_per_k"] == pytest.approx(H_TRUE, rel=0.01)
    assert result["internal_gains_w"] == pytest.approx(GAINS_TRUE, rel=0.05)
    assert result["r2"] > 0.99


def test_fit_cop_scale_recovers_scale():
    result = cal.fit_cop_scale(synthetic_hours(), CopModel(WBB12_RATING_POINTS))
    assert result is not None
    assert result["cop_scale"] == pytest.approx(SCALE_TRUE, rel=0.01)


def test_fit_dhw_loss_recovers_ua():
    result = cal.fit_dhw_loss(synthetic_hours(), VOLUME, AMBIENT)
    assert result is not None
    assert result["loss_w_per_k"] == pytest.approx(UA_TRUE, rel=0.02)


def test_too_little_data_returns_none():
    hours = synthetic_hours(days=2)
    assert cal.fit_building_loss(hours) is None
    assert cal.fit_cop_scale(hours[:3], CopModel(WBB12_RATING_POINTS)) is None


def test_csv_roundtrip_and_fit_cli(tmp_path, capsys):
    path = tmp_path / "hp.csv"
    cal.write_csv(path, synthetic_hours())
    again = cal.read_csv(path)
    assert len(again) == 240
    assert cal.main(["fit", str(path), "--device-id", "wbb12"]) == 0
    snippet = json.loads(capsys.readouterr().out)
    hp = snippet["devices"]["heat_pumps"]["wbb12"]
    assert hp["building"]["loss_w_per_k"] == pytest.approx(H_TRUE, rel=0.01)
    assert hp["unit"]["cop_scale"] == pytest.approx(SCALE_TRUE, rel=0.01)
    assert hp["dhw"]["loss_w_per_k"] == pytest.approx(UA_TRUE, rel=0.02)


def test_snippet_is_valid_eos_config(config_eos, tmp_path, capsys):
    path = tmp_path / "hp.csv"
    cal.write_csv(path, synthetic_hours())
    cal.main(["fit", str(path), "--device-id", "wbb12"])
    snippet = json.loads(capsys.readouterr().out)
    config_eos.merge_settings_from_dict(snippet)
    assert config_eos.devices.heat_pumps["wbb12"].unit.cop_scale == pytest.approx(
        SCALE_TRUE, rel=0.01
    )


class TestHourlyMeans:
    def test_step_signal_time_weighted(self):
        t0 = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
        samples = [
            (t0, 0.0),
            (t0 + timedelta(minutes=15), 100.0),
            (t0 + timedelta(minutes=45), 0.0),
        ]
        means = cal.hourly_means(samples, t0, t0 + timedelta(hours=1))
        assert means[t0] == pytest.approx(50.0)

    def test_value_holds_into_following_hours(self):
        t0 = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
        means = cal.hourly_means([(t0, 7.0)], t0, t0 + timedelta(hours=3))
        assert means == {t0: 7.0, t0 + timedelta(hours=1): 7.0, t0 + timedelta(hours=2): 7.0}

    def test_hours_before_first_sample_are_missing(self):
        t0 = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
        means = cal.hourly_means([(t0 + timedelta(hours=2), 1.0)], t0, t0 + timedelta(hours=3))
        assert list(means) == [t0 + timedelta(hours=2)]

    def test_nan_samples_ignored(self):
        t0 = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
        means = cal.hourly_means([(t0, float("nan")), (t0, 3.0)], t0, t0 + timedelta(hours=1))
        assert means[t0] == pytest.approx(3.0)
