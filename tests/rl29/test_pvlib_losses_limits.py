"""PVLib provider: system losses and AC limits (issue #1233, RL29 fork)."""

from __future__ import annotations

import pandas as pd
import pytest

from akkudoktoreos.prediction.pvforecastpvlib import PVForecastPVLib

PLANE = {
    "surface_tilt": 20,
    "surface_azimuth": 180,
    "peakpower": 4.9,
    "module_model": "AXITEC_AC_410MH_144S",
    "inverter_model": "Sungrow__SH25T",
    "modules_per_string": 12,
    "strings_per_inverter": 1,
}


def _weather(ghi=900.0, dni=800.0, dhi=100.0, periods=8):
    index = pd.date_range("2024-06-21 11:00", periods=periods, freq="15min", tz="Europe/Berlin")
    return pd.DataFrame(
        {
            "ghi": [ghi] * periods,
            "dni": [dni] * periods,
            "dhi": [dhi] * periods,
            "temp_air": [20.0] * periods,
            "wind_speed": [2.0] * periods,
            "relative_humidity": [45.0] * periods,
            "precipitable_water": [1.5] * periods,
            "cloud_cover": [0.0] * periods,
        },
        index=index,
    )


def _run(config_eos, planes, pvlib=None):
    settings = {
        "general": {"latitude": 52.52, "longitude": 13.40},
        "prediction": {"hours": 48, "historic_hours": 24},
        "pvforecast": {"provider": "PVForecastPVLib", "max_planes": 4, "planes": planes},
    }
    if pvlib is not None:
        settings["pvforecast"]["pvlib"] = pvlib
    config_eos.merge_settings_from_dict(settings)
    PVForecastPVLib.reset_instance()
    return PVForecastPVLib()._calculate_pvlib_power(_weather())


def test_loss_reduces_dc_power_by_configured_percentage(config_eos):
    base = _run(config_eos, [{**PLANE, "loss": 0.0}])
    lossy = _run(config_eos, [{**PLANE, "loss": 14.0}])
    ratio = lossy["pv_dc_power"].sum() / base["pv_dc_power"].sum()
    assert ratio == pytest.approx(0.86, abs=1e-6)
    # AC follows DC through the (slightly non-linear) inverter model.
    ac_ratio = lossy["ac_power"].sum() / base["ac_power"].sum()
    assert 0.84 < ac_ratio < 0.88


def test_zero_loss_unchanged_by_loss_model(config_eos):
    a = _run(config_eos, [{**PLANE, "loss": 0.0}])
    b = _run(config_eos, [{**PLANE, "loss": None}])
    pd.testing.assert_frame_equal(a, b)


def test_invalid_loss_rejected(config_eos):
    with pytest.raises(ValueError):
        _run(config_eos, [{**PLANE, "loss": 100.0}])


def test_inverter_paco_clips_plane_ac(config_eos):
    unlimited = _run(config_eos, [{**PLANE, "loss": 0.0}])
    peak = unlimited["ac_power"].max()
    assert peak > 2000
    limited = _run(config_eos, [{**PLANE, "loss": 0.0, "inverter_paco": 2000}])
    assert limited["ac_power"].max() == pytest.approx(2000.0)
    # DC is not affected by the AC rating.
    pd.testing.assert_series_equal(limited["pv_dc_power"], unlimited["pv_dc_power"])


def test_system_ac_limit_for_shared_inverter(config_eos):
    east = {**PLANE, "loss": 0.0, "surface_azimuth": 115}
    west = {**PLANE, "loss": 0.0, "surface_azimuth": 295}
    free = _run(config_eos, [east, west])
    cap = 0.8 * free["ac_power"].max()
    limited = _run(config_eos, [east, west], pvlib={"max_ac_power_w": cap})
    assert limited["ac_power"].max() == pytest.approx(cap)
    assert (limited["ac_power"] <= free["ac_power"] + 1e-9).all()


def test_no_warning_for_supported_options(config_eos, caplog):
    PVForecastPVLib._warned_features.clear()
    _run(config_eos, [{**PLANE, "loss": 10.0, "inverter_paco": 5000}])
    assert "loss is currently not supported" not in caplog.text
    assert "inverter_paco is currently not supported" not in caplog.text
