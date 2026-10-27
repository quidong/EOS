"""Data folder of the fork and the move of the legacy ``rl29`` folder."""

from __future__ import annotations

import os

from akkudoktoreos.core.forkpaths import fork_data_dir


def test_fork_data_dir_is_not_created(tmp_path):
    assert fork_data_dir(tmp_path) == tmp_path / "plus"
    assert not (tmp_path / "plus").exists()


def test_legacy_folder_is_moved_and_linked(tmp_path):
    legacy = tmp_path / "rl29"
    legacy.mkdir()
    (legacy / "load_ml.json").write_text("{}")

    folder = fork_data_dir(tmp_path)

    assert folder == tmp_path / "plus"
    assert (folder / "load_ml.json").read_text() == "{}"
    assert legacy.is_symlink() and os.readlink(legacy) == "plus"
    # Readers of the old path (e.g. EOS_MCP_REPORT_DIR=/data/rl29) still find the files.
    assert (legacy / "load_ml.json").exists()
    # A second call changes nothing.
    assert fork_data_dir(tmp_path) == folder and legacy.is_symlink()


def test_existing_new_folder_wins(tmp_path):
    (tmp_path / "rl29").mkdir()
    (tmp_path / "plus").mkdir()
    fork_data_dir(tmp_path)
    assert (tmp_path / "rl29").is_dir() and not (tmp_path / "rl29").is_symlink()
