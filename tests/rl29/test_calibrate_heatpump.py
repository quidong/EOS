"""Calibration script (RL29 fork): CSV handling, Open-Meteo merge and the fit CLI.

The fits themselves are tested in test_heatpump_calibration_fit.py; here the
script is driven end to end with synthetic hourly data.
"""

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
sys.modules["calibrate_heatpump"] = cal
spec.loader.exec_module(cal)

H_TRUE = 180.0
GAINS_TRUE = 300.0
APERTURE_TRUE = 4.0
SCALE_TRUE = 0.7
UA_TRUE = 1.6
STANDBY = 22.0
VOLUME = 190.0
AMBIENT = 18.0


def synthetic_rows(days: int = 14, *, solar: bool = True) -> list[dict]:
    cop = CopModel(WBB12_RATING_POINTS, scale=SCALE_TRUE)
    capacity = VOLUME * cal.WATER_WH_PER_LITRE_K
    start = datetime(2026, 11, 10, 23, tzinfo=timezone.utc)
    rows = []
    dhw = 46.0
    for i in range(days * 24):
        t = start + timedelta(hours=i)
        day = i // 24
        hour = (t.hour + 1) % 24
        outdoor = -2.0 + day * 0.6 + 3.0 * math.sin((hour - 9) / 24 * 2 * math.pi)
        ghi = 0.0
        if solar and 8 <= hour <= 16:
            ghi = (25.0 + 12.5 * (day % 4)) * math.sin((hour - 8) / 8 * math.pi) * 4
        heat = max(0.0, H_TRUE * (21.0 - outdoor) - APERTURE_TRUE * ghi - GAINS_TRUE)
        flow = 26.0 + heat / 1000.0
        electric = (heat / cop.cop(outdoor, flow) if heat > 0 else 0.0) + STANDBY
        dhw_heat = 0.0
        if 0 <= hour <= 5:
            dhw = AMBIENT + (dhw - AMBIENT) * math.exp(-UA_TRUE / capacity)
        elif hour == 6:
            dhw_heat = (46.0 - dhw) * capacity
            dhw = 46.0
        rows.append(
            {
                "timestamp": t,
                "outdoor_c": outdoor,
                "room_c": 21.0,
                "heat_w": heat + dhw_heat,
                "electric_w": electric + (dhw_heat / 2.5 if dhw_heat else 0.0),
                "flow_c": flow,
                "dhw_c": dhw,
                "dhw_heat_w": dhw_heat,
                "ghi_w_per_m2": ghi if solar else None,
            }
        )
    return rows


FREE = ["--prior-weight-days", "0.05", "--cop-prior-hours", "0", "--dhw-prior-nights", "0"]


def test_csv_roundtrip_and_fit_cli(tmp_path, capsys):
    path = tmp_path / "hp.csv"
    cal.write_csv(path, synthetic_rows())
    again = cal.read_csv(path)
    assert len(again) == 14 * 24
    assert again[0]["ghi_w_per_m2"] == 0.0 and again[10]["ghi_w_per_m2"] > 0
    assert cal.main(["fit", str(path), "--device-id", "wbb12", "--prior-loss", "150", *FREE]) == 0
    out = capsys.readouterr()
    snippet = json.loads(out.out)
    hp = snippet["devices"]["heat_pumps"]["wbb12"]
    assert hp["building"]["loss_w_per_k"] == pytest.approx(H_TRUE, rel=0.03)
    assert hp["building"]["solar_aperture_m2"] == pytest.approx(APERTURE_TRUE, rel=0.1)
    assert hp["building"]["internal_gains_w"] == pytest.approx(GAINS_TRUE, rel=0.15)
    assert hp["unit"]["cop_scale"] == pytest.approx(SCALE_TRUE, rel=0.03)
    assert hp["dhw"]["loss_w_per_k"] == pytest.approx(UA_TRUE, rel=0.05)
    assert "loss 150 ->" in out.err and "solar fitted" in out.err


def test_fit_without_irradiance_column_keeps_the_prior_aperture(tmp_path, capsys):
    path = tmp_path / "hp.csv"
    cal.write_csv(path, synthetic_rows(solar=False))
    assert cal.main(["fit", str(path), "--prior-aperture", "2.5", *FREE]) == 0
    out = capsys.readouterr()
    hp = json.loads(out.out)["devices"]["heat_pumps"]["wbb12"]
    assert hp["building"]["solar_aperture_m2"] == 2.5
    assert "solar not fitted" in out.err


def test_default_priors_shrink_the_fit(tmp_path, capsys):
    path = tmp_path / "hp.csv"
    cal.write_csv(path, synthetic_rows(days=5))
    assert cal.main(["fit", str(path), "--prior-loss", "150", "--prior-weight-days", "1e6"]) == 0
    hp = json.loads(capsys.readouterr().out)["devices"]["heat_pumps"]["wbb12"]
    assert hp["building"]["loss_w_per_k"] == pytest.approx(150.0, abs=1.0)


def test_snippet_is_valid_eos_config(config_eos, tmp_path, capsys):
    path = tmp_path / "hp.csv"
    cal.write_csv(path, synthetic_rows())
    cal.main(["fit", str(path), "--device-id", "wbb12", *FREE])
    snippet = json.loads(capsys.readouterr().out)
    config_eos.merge_settings_from_dict(snippet)
    device = config_eos.devices.heat_pumps["wbb12"]
    assert device.unit.cop_scale == pytest.approx(SCALE_TRUE, rel=0.03)
    assert device.building.solar_aperture_m2 == pytest.approx(APERTURE_TRUE, rel=0.1)


def test_fetch_merges_open_meteo_without_overriding_the_plants_sensor(monkeypatch, tmp_path):
    t0 = datetime(2026, 11, 20, 0, tzinfo=timezone.utc)

    def fake_history(base_url, token, entity_id, start, end):
        value = {"sensor.wh_system_aussentemperatur": 3.0}.get(entity_id, 100.0)
        return [(start, value)]

    def fake_open_meteo(latitude, longitude, start, end):
        hours = [start + timedelta(hours=i) for i in range(48)]
        return {
            "ghi_w_per_m2": {h: 150.0 for h in hours},
            "outdoor_c": {h: 9.0 for h in hours},
        }

    monkeypatch.setattr(cal, "fetch_history", fake_history)
    monkeypatch.setattr(cal, "fetch_open_meteo", fake_open_meteo)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return t0.astimezone(tz) if tz else t0

    monkeypatch.setattr(cal, "datetime", FixedDatetime)
    out = tmp_path / "hp.csv"
    assert (
        cal.main(
            [
                "fetch",
                "--ha-url",
                "http://ha",
                "--token",
                "x",
                "--days",
                "2",
                "--open-meteo",
                "49.49,8.47",
                "-o",
                str(out),
            ]
        )
        == 0
    )
    rows = cal.read_csv(out)
    assert len(rows) == 48
    assert all(r["ghi_w_per_m2"] == 150.0 for r in rows)
    assert all(r["outdoor_c"] == 3.0 for r in rows)  # the plant's sensor wins
    assert all(r["heat_w"] == 100.0 for r in rows)


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
