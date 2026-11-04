"""EOS log file reading (RL29 fork): Loguru-serialized lines, tail with filters."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from loguru import logger

from akkudoktoreos.core.logging import normalize_log_entry, read_file_log


@pytest.fixture
def loguru_file(tmp_path):
    """A real log file written by Loguru with serialize=True, as EOS writes it."""
    path = tmp_path / "eos.log"
    handler = logger.add(path, serialize=True, level="DEBUG", enqueue=False)
    try:
        logger.info("Heat pump wbb12: {}", {"extra_cost_amt": 1.5, "stored_heat_credit_amt": 0.4})
        for i in range(300):
            logger.debug("noise {}", i)
        logger.warning("Forecast stale")
        logger.info("Heat pump wbb12: {}", {"extra_cost_amt": 0.2})
        logger.info("done")
    finally:
        logger.remove(handler)
    return path


def test_normalize_flattens_loguru_record(loguru_file):
    line = loguru_file.read_text(encoding="utf-8").splitlines()[0]
    entry = normalize_log_entry(json.loads(line))
    assert entry["level"]["name"] == "INFO"
    assert entry["message"].startswith("Heat pump wbb12:")
    assert "T" in entry["time"] and entry["time"][10] == "T"
    assert entry["function"] == "loguru_file"


def test_flat_entries_pass_unchanged():
    flat = {"time": "2026-11-04T10:00:00+01:00", "level": {"name": "INFO"}, "message": "x"}
    assert normalize_log_entry(flat) is flat


def test_level_and_contains_filters_work_on_loguru_lines(loguru_file):
    warnings = read_file_log(loguru_file, level="WARNING")
    assert [e["message"] for e in warnings] == ["Forecast stale"]
    hp = read_file_log(loguru_file, contains="heat pump wbb12")
    assert len(hp) == 2


def test_tail_finds_rare_matches_beyond_five_times_limit(loguru_file):
    # The first summary is > 300 lines back: upstream tail (last 5 * limit lines) missed it.
    hp = read_file_log(loguru_file, contains="Heat pump wbb12", limit=2, tail=True)
    assert len(hp) == 2
    assert "1.5" in hp[0]["message"] and "0.2" in hp[1]["message"]  # oldest first
    last = read_file_log(loguru_file, contains="Heat pump wbb12", limit=1, tail=True)
    assert "0.2" in last[0]["message"]


def test_tail_without_filter_returns_the_last_lines(loguru_file):
    last = read_file_log(loguru_file, limit=3, tail=True)
    assert [e["message"] for e in last][-1] == "done"
    assert len(last) == 3


def test_tail_stops_at_from_time(tmp_path):
    path = tmp_path / "eos.log"
    now = datetime.now(timezone.utc)
    lines = []
    for minutes, msg in ((120, "old"), (30, "recent"), (5, "new")):
        t = (now - timedelta(minutes=minutes)).isoformat()
        lines.append(json.dumps({"time": t, "level": {"name": "INFO"}, "message": msg}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    since = (now - timedelta(minutes=60)).isoformat()
    got = read_file_log(path, from_time=since, tail=True)
    assert [e["message"] for e in got] == ["recent", "new"]


def test_tail_handles_lines_across_chunk_borders(tmp_path):
    path = tmp_path / "eos.log"
    entries = [
        {
            "time": "2026-11-04T10:00:00+00:00",
            "level": {"name": "INFO"},
            "message": f"m{i} " + "x" * 5000,
        }
        for i in range(40)
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")  # no final newline
    got = read_file_log(path, limit=40, tail=True)
    assert [e["message"].split()[0] for e in got] == [f"m{i}" for i in range(40)]


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_file_log(tmp_path / "none.log")


@pytest.mark.asyncio
async def test_endpoint_answers_404_with_hint_when_file_logging_is_off(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import akkudoktoreos.server.eos as eos_server
    from akkudoktoreos.server.rest.error import EOSProblem

    fake = SimpleNamespace(logging=SimpleNamespace(file_path=tmp_path / "eos.log"))
    monkeypatch.setattr(eos_server, "get_config", lambda: fake)
    with pytest.raises(EOSProblem) as info:
        await eos_server.fastapi_logging_get_log(
            limit=10, level=None, contains=None, regex=None, from_time=None, to_time=None, tail=True
        )
    assert info.value.status == 404
    assert "logging/file_level" in info.value.detail
