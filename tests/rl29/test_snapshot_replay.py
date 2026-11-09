"""Optimization input snapshots and their replay (RL29 fork)."""

import gzip
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from akkudoktoreos.core.snapshot import (
    build_snapshot,
    config_payload,
    iter_snapshots,
    load_snapshot,
    prune_snapshots,
    snapshot_path,
    write_snapshot,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rl29" / "replay_snapshots.py"


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("replay_snapshots", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_snapshot(issued: datetime, algorithm: str = "MILP") -> dict[str, Any]:
    return build_snapshot(
        issued_at=issued,
        algorithm=algorithm,
        start_hour=issued.hour,
        seed=None,
        parameters={"ems": {"pv_prognose_wh": [1.0]}},
        config={"general": {"timezone": "Europe/Berlin"}},
    )


def test_write_load_iter_and_prune(tmp_path):
    now = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
    old = write_snapshot(tmp_path, make_snapshot(now - timedelta(days=40), "GENETIC"))
    recent = write_snapshot(tmp_path, make_snapshot(now - timedelta(minutes=15)))
    assert recent == snapshot_path(tmp_path, make_snapshot(now - timedelta(minutes=15)))
    assert recent.name == "20261109T194500Z_MILP.json.gz"
    with gzip.open(recent, "rt") as handle:  # plain gzip JSON, readable without EOS
        assert json.load(handle)["algorithm"] == "MILP"
    assert load_snapshot(recent)["parameters"] == {"ems": {"pv_prognose_wh": [1.0]}}

    assert list(iter_snapshots(tmp_path)) == [old, recent]
    assert list(iter_snapshots(tmp_path, start=now - timedelta(days=1))) == [recent]
    assert prune_snapshots(tmp_path, retention_days=30, now=now) == 1
    assert not old.exists() and not old.parent.exists()
    assert list(iter_snapshots(tmp_path)) == [recent]
    assert prune_snapshots(tmp_path / "missing", 30) == 0


def test_config_payload_masks_secrets(config_eos):
    config_eos.merge_settings_from_dict(
        {"elecprice": {"tibber": {"access_token": "secret-token-123"}}}
    )
    payload = config_payload(config_eos)
    text = json.dumps(payload)
    assert "secret-token-123" not in text
    assert payload["elecprice"]["tibber"]["access_token"] != "secret-token-123"


def test_parse_variant_and_summary():
    script = load_script()
    algorithm, overrides = script.parse_variant(
        "milp:optimization/milp/time_limit_s=60,optimization/milp/scenarios=false"
    )
    assert algorithm == "MILP"
    assert overrides == [
        ("optimization/milp/time_limit_s", 60),
        ("optimization/milp/scenarios", False),
    ]
    assert script.parse_variant("GENETIC") == ("GENETIC", [])
    with pytest.raises(Exception):
        script.parse_variant("MILP:broken")

    rows = [
        {"variant": "MILP", "objective": 9.0, "delta_vs_recorded": -0.5, "duration_s": 30.0},
        {"variant": "MILP", "objective": 11.0, "delta_vs_recorded": 0.1, "duration_s": 40.0},
        {"variant": "GENETIC", "objective": None, "delta_vs_recorded": None, "duration_s": 0.0},
    ]
    summary = script.summarize(rows)
    assert summary["MILP"] == {
        "runs": 2,
        "mean_objective": 10.0,
        "mean_delta_vs_executed": -0.2,
        "better_than_executed_share": 0.5,
        "mean_duration_s": 35.0,
    }
    assert summary["GENETIC"]["runs"] == 0


@pytest.mark.asyncio
async def test_automatic_run_stores_a_snapshot_that_replays(
    configured_request, monkeypatch, tmp_path
):
    """End to end: an automatic run leaves a snapshot; replaying it reproduces the plan
    score with the same algorithm and seed, and scores another algorithm on the same inputs."""
    from unittest.mock import AsyncMock, Mock

    from akkudoktoreos.core import ems as ems_module
    from akkudoktoreos.core.ems import EnergyManagement
    from akkudoktoreos.core.emsettings import EnergyManagementMode
    from akkudoktoreos.core.historyrecorder import HistoryRecorder
    from akkudoktoreos.core.snapshot import apply_snapshot_config, replay_snapshot, snapshot_dir
    from akkudoktoreos.optimization.genetic import configrequest
    from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
    from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters

    config, fake, _, data = configured_request
    config.merge_settings_from_dict(
        {
            "general": {"data_folder_path": str(tmp_path)},
            "optimization": {
                "algorithm": "GENETIC",
                "snapshots": {"enabled": True},
                "milp": {"polish_generations": 2, "polish_individuals": 10},
            },
        }
    )
    ems = EnergyManagement()
    for name in ("_genetic_solution", "_optimization_solution", "_plan"):
        monkeypatch.setattr(EnergyManagement, name, None)
    monkeypatch.setattr(configrequest, "get_ems", lambda: ems)
    monkeypatch.setattr(EnergyManagement, "prediction", Mock(update_data=AsyncMock()))
    monkeypatch.setattr(EnergyManagement, "adapter", Mock(update_data=AsyncMock()))
    monkeypatch.setattr(HistoryRecorder, "schedule", lambda self, *args: None)
    request = ConfigOptimizationRequest.model_validate(data)

    async def prepare(cls):
        return await request.resolve()

    monkeypatch.setattr(GeneticOptimizationParameters, "prepare", classmethod(prepare))

    solution = await ems.run(
        start_datetime=fake.start_datetime.add(minutes=2),
        mode=EnergyManagementMode.OPTIMIZATION,
    )
    assert solution is not None
    for task in list(ems_module._snapshot_tasks):
        await task

    directory = snapshot_dir(config)
    files = list(iter_snapshots(directory))
    assert len(files) == 1 and files[0].name.endswith("_GENETIC.json.gz")
    snapshot = load_snapshot(files[0])
    assert snapshot["result"]["objective"] is not None
    assert snapshot["parameters"]["ems"]["pv_prognose_wh"]
    assert snapshot["config"]["optimization"]["snapshots"]["enabled"] is True

    apply_snapshot_config(config, snapshot, data_folder_path=str(tmp_path / "replay"))
    same = replay_snapshot(snapshot, "GENETIC", seed=snapshot["seed"])
    assert same["objective"] == pytest.approx(snapshot["result"]["objective"], abs=1e-6)
    assert same["delta_vs_recorded"] == pytest.approx(0.0, abs=1e-6)
    other = replay_snapshot(snapshot, "MILP", seed=1)
    assert other["algorithm"] == "MILP" and other["objective"] is not None

    # Overrides change the replayed configuration only.
    apply_snapshot_config(
        config, snapshot, [("optimization/milp/time_limit_s", 3.0)], str(tmp_path / "replay")
    )
    assert config.optimization.milp.time_limit_s == 3.0

    # The script runs the same replay from the command line.
    script = load_script()
    assert (
        script.main(
            [
                "--dir",
                str(directory),
                "--variant",
                "GENETIC",
                "--csv",
                str(tmp_path / "out.csv"),
            ]
        )
        == 0
    )
    assert (tmp_path / "out.csv").read_text().startswith("issued_at,")
