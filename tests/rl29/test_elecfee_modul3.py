"""§14a Modul 3 fee snippet: bands by time of day, only in the Modul 3 quarters."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pendulum
import pytest

from akkudoktoreos.config.configabc import ValueTimeWindowSequence

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rl29" / "elecfee_modul3.py"
spec = importlib.util.spec_from_file_location("elecfee_modul3", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules["elecfee_modul3"] = mod
spec.loader.exec_module(mod)

OTHER = 0.0948
REGULAR = 0.0750


def windows() -> ValueTimeWindowSequence:
    return ValueTimeWindowSequence.model_validate(
        mod.consumption_windows(OTHER, REGULAR, 0.0173, 0.0432, 0.0596)
    )


def value_at(seq: ValueTimeWindowSequence, when: pendulum.DateTime) -> float | None:
    for window in seq.windows:
        if window.contains(when):
            return window.value
    return None


@pytest.mark.parametrize(
    ("when", "network"),
    [
        ("2026-01-15T01:00", 0.0432),
        ("2026-01-15T03:00", 0.0173),
        ("2026-11-15T12:00", 0.0432),
        ("2026-11-15T18:30", 0.0596),
        ("2026-12-31T23:45", 0.0432),
        ("2026-05-15T03:00", REGULAR),
        ("2026-07-15T18:30", REGULAR),
    ],
)
def test_band_by_time_and_month(when: str, network: float):
    moment = pendulum.parse(when, tz="Europe/Berlin")
    assert value_at(windows(), moment) == pytest.approx(OTHER + network)


def test_every_quarter_hour_of_a_year_is_covered_once():
    seq = windows()
    moment = pendulum.datetime(2026, 1, 1, tz="Europe/Berlin")
    end = moment.add(years=1)
    while moment < end:
        hits = [w for w in seq.windows if w.contains(moment)]
        assert len(hits) == 1, moment
        moment = moment.add(minutes=15 * 7 + 1)  # sample across days and times


def test_series_follows_the_months():
    seq = windows()
    start = pendulum.datetime(2026, 3, 31, 0, tz="Europe/Berlin")
    series = seq.to_series(
        start_datetime=start,
        end_datetime=start.add(days=2),
        interval=pendulum.duration(hours=1),
        dropna=True,
        boundary="context",
        align_to_interval=True,
    )
    march_night = series[series.index == start.add(hours=3)].iloc[0]
    april_night = series[series.index == start.add(days=1, hours=3)].iloc[0]
    assert march_night == pytest.approx(OTHER + 0.0173)
    assert april_night == pytest.approx(OTHER + REGULAR)


def test_cli_snippet_is_a_valid_eos_config(config_eos, capsys):
    assert mod.main(["--other-amt-kwh", str(OTHER), "--regular-network-amt-kwh", str(REGULAR)]) == 0
    data = json.loads(capsys.readouterr().out)
    config_eos.merge_settings_from_dict(data)
    seq = config_eos.elecfee.elecfeefixed.consumption_amt_kwh
    assert len(seq.windows) == 6
    assert config_eos.elecfee.elecfeefixed.consumption_percent_amt.windows[0].value == 19.0
