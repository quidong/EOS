"""Tests for the RL29 fork tooling and the production configuration.

These tests guard what the fork adds on top of upstream EOS:

- the production configuration of the "energy" VM must load unchanged,
- the snapshot/diff tool used as the acceptance check must detect differences,
- the CI workflow and the smoke test script must stay well-formed.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTDATA = Path(__file__).parent / "testdata"
SCRIPTS = REPO_ROOT / "scripts" / "rl29"


def _load_snapshot_module():
    spec = importlib.util.spec_from_file_location("eos_snapshot", SCRIPTS / "eos_snapshot.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eos_snapshot = _load_snapshot_module()


# ---------------------------------------------------------------------------
# Production configuration
# ---------------------------------------------------------------------------


@pytest.fixture
def production_config() -> dict:
    return json.loads((TESTDATA / "eos_config_rl29_production.json").read_text(encoding="utf-8"))


def test_production_config_loads(config_eos, production_config):
    """The live configuration of the energy VM is accepted by this code base."""
    config_eos.merge_settings_from_dict(production_config)

    assert config_eos.ems.mode == "PREDICTION"
    assert config_eos.ems.interval == 900
    assert config_eos.load.provider == "LoadAkkudoktor"
    assert config_eos.pvforecast.provider == "PVForecastPVLib"
    assert len(config_eos.pvforecast.planes) == 2
    assert config_eos.pvforecast.planes[0].surface_azimuth == pytest.approx(115.3)
    assert config_eos.pvforecast.planes[1].surface_azimuth == pytest.approx(295.3)
    assert config_eos.elecprice.provider == "ElecPriceEnergyCharts"
    assert config_eos.feedintariff.provider == "FeedInTariffFixed"
    assert "fronius" in config_eos.devices.inverters
    assert config_eos.devices.inverters["fronius"].max_power_w == pytest.approx(20000.0)
    assert config_eos.adapter.homeassistant.load_emr_entity_ids == [
        "sensor.energie_hausverbrauch_gesamt"
    ]


def test_production_config_default_algorithm_is_genetic(config_eos, production_config):
    """No optimization section is configured, the fork relies on GENETIC as default."""
    config_eos.merge_settings_from_dict(production_config)
    assert config_eos.optimization.algorithm == "GENETIC"


# ---------------------------------------------------------------------------
# Snapshot / diff tool
# ---------------------------------------------------------------------------


def _snapshot(**overrides) -> dict:
    snap = {
        "health": {"status": "alive", "version": "0.3.0.dev", "energy-management": {}},
        "config": {"general": {"version": "x", "latitude": 49.5}, "ems": {"mode": "PREDICTION"}},
        "prediction_keys": ["pvforecast_ac_power", "elecprice_marketprice_wh"],
        "measurement_keys": ["sensor.energie_hausverbrauch_gesamt"],
        "series": {
            "pvforecast_ac_power": {"count": 96, "numeric": 96, "min": 0.0, "max": 9000.0, "mean": 2100.0},
            "elecprice_marketprice_wh": {"count": 96, "numeric": 96, "min": 0.0001, "max": 0.0003, "mean": 0.0002},
        },
    }
    snap.update(overrides)
    return snap


def test_diff_identical_snapshots_is_empty():
    assert eos_snapshot.diff(_snapshot(), _snapshot()) == []


def test_diff_ignores_volatile_config():
    a = _snapshot()
    b = copy.deepcopy(a)
    b["config"]["general"]["version"] = "other"
    a["config"] = eos_snapshot._strip_volatile(a["config"])
    b["config"] = eos_snapshot._strip_volatile(b["config"])
    assert eos_snapshot.diff(a, b) == []


def test_diff_detects_config_change():
    b = _snapshot()
    b["config"] = {"general": {"version": "x", "latitude": 49.5}, "ems": {"mode": "OPTIMIZATION"}}
    problems = eos_snapshot.diff(_snapshot(), b)
    assert any("ems.mode" in p for p in problems)


def test_diff_detects_missing_prediction_key():
    b = _snapshot(prediction_keys=["pvforecast_ac_power"])
    problems = eos_snapshot.diff(_snapshot(), b)
    assert any("prediction_keys missing" in p for p in problems)


def test_diff_detects_lost_series_data():
    b = _snapshot()
    b["series"]["pvforecast_ac_power"] = {"count": 96, "numeric": 0}
    problems = eos_snapshot.diff(_snapshot(), b)
    assert any("had data before, none after" in p for p in problems)


def test_diff_respects_tolerance():
    b = _snapshot()
    b["series"]["pvforecast_ac_power"]["mean"] = 2100.0 * 1.01  # within 2 %
    assert eos_snapshot.diff(_snapshot(), b) == []
    b["series"]["pvforecast_ac_power"]["mean"] = 2100.0 * 1.10  # outside 2 %
    assert any("pvforecast_ac_power.mean" in p for p in eos_snapshot.diff(_snapshot(), b))


def test_diff_flags_dead_server():
    b = _snapshot()
    b["health"]["status"] = "dead"
    assert any("health status" in p for p in eos_snapshot.diff(_snapshot(), b))


def test_new_config_keys_are_warnings_only(tmp_path, capsys):
    a = _snapshot()
    b = copy.deepcopy(a)
    b["config"]["ems"]["new_option"] = 1
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a))
    pb.write_text(json.dumps(b))
    rc = eos_snapshot.main(["diff", str(pa), str(pb)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARN" in out and "EQUIVALENT" in out


def test_series_stats_handles_gaps():
    stats = eos_snapshot._series_stats([1.0, None, float("nan"), 3.0])
    assert stats == {"count": 4, "numeric": 2, "min": 1.0, "max": 3.0, "mean": 2.0}
    assert eos_snapshot._series_stats([None, None]) == {"count": 2, "numeric": 0}


# ---------------------------------------------------------------------------
# CI workflow and smoke test script
# ---------------------------------------------------------------------------


def test_workflow_is_valid_and_complete():
    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "rl29-image.yml").read_text())
    # PyYAML parses the bare key "on" as boolean True.
    triggers = wf.get("on", wf.get(True))
    assert triggers["push"]["branches"] == ["rl29/main"]
    assert "workflow_dispatch" in triggers
    assert set(wf["jobs"]) == {"test", "build", "smoke"}
    assert wf["jobs"]["build"]["needs"] == "test"
    assert wf["jobs"]["smoke"]["needs"] == "build"
    steps = " ".join(str(s.get("run", "")) for s in wf["jobs"]["test"]["steps"])
    assert "tests/rl29" in steps
    labels = next(s for s in wf["jobs"]["build"]["steps"] if s.get("id") == "build")["with"]["labels"]
    assert "com.centurylinklabs.watchtower.enable=false" in labels


def test_upstream_workflows_untouched():
    """The fork adds its own workflow and leaves upstream workflows alone (rebase friendly)."""
    upstream = {"bump-version.yml", "codeql.yml", "docker-build.yml", "pre-commit.yml", "pytest.yml", "stale.yml"}
    present = {p.name for p in (REPO_ROOT / ".github" / "workflows").glob("*.yml")}
    assert upstream <= present
    assert present - upstream == {"rl29-image.yml"}


def test_smoke_script_syntax():
    script = SCRIPTS / "smoke_test_image.sh"
    assert script.exists()
    subprocess.run(["bash", "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", "-S", "warning", str(script)], check=True)


# ---------------------------------------------------------------------------
# Snapshot tool against a real EOS server process
# ---------------------------------------------------------------------------


class TestSnapshotAgainstServer:
    def test_snapshot_roundtrip(self, server_setup_for_class, tmp_path):
        server = server_setup_for_class["server"]
        first = eos_snapshot.snapshot(server, hours=6)
        second = eos_snapshot.snapshot(server, hours=6)

        assert first["health"]["status"] == "alive"
        assert "ems" in first["config"]
        problems = [p for p in eos_snapshot.diff(first, second) if "(check)" not in p]
        assert problems == []

        out = tmp_path / "snap.json"
        assert eos_snapshot.main(["snapshot", server, "-o", str(out), "--hours", "2"]) == 0
        assert json.loads(out.read_text())["health"]["status"] == "alive"


def test_snapshot_window_from_reuses_hours(tmp_path, monkeypatch):
    """--window-from takes start and length of an earlier snapshot."""
    calls = {}

    def fake_snapshot(base, hours, start_at=None):
        calls["args"] = (base, hours, start_at)
        return {"prediction_keys": []}

    monkeypatch.setattr(eos_snapshot, "snapshot", fake_snapshot)
    earlier = tmp_path / "before.json"
    earlier.write_text(
        json.dumps(
            {"series_window": {"start": "2026-10-20T21:00:00+00:00", "end": "2026-10-21T21:00:00+00:00"}}
        )
    )
    out = tmp_path / "after.json"
    rc = eos_snapshot.main(
        ["snapshot", "http://eos:8503", "-o", str(out), "--window-from", str(earlier)]
    )
    assert rc == 0
    assert calls["args"] == ("http://eos:8503", 24, "2026-10-20T21:00:00+00:00")
