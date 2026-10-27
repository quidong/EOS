"""Data folder of the eos-plus fork (model reports, calibration, learned profiles).

Everything the fork learns or reports lives in ``<data folder>/plus``. Earlier versions
used ``<data folder>/rl29``; such a folder is renamed once and replaced by a symbolic link
to the new one, so tools that still read the old path (e.g. ``EOS_MCP_REPORT_DIR``) keep
working.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from loguru import logger

FORK_DIR_NAME = "plus"
LEGACY_DIR_NAMES = ("rl29",)


def migrate_legacy_dir(data_folder: Union[str, Path]) -> None:
    """Move a legacy fork folder to ``<data folder>/plus`` (once, best effort)."""
    base = Path(data_folder)
    target = base / FORK_DIR_NAME
    for name in LEGACY_DIR_NAMES:
        legacy = base / name
        if legacy.is_symlink() or not legacy.is_dir() or target.exists():
            continue
        try:
            legacy.rename(target)
            os.symlink(FORK_DIR_NAME, legacy, target_is_directory=True)
            logger.info("Moved fork data folder {} to {} (link left behind).", legacy, target)
        except OSError as exc:  # never block EOS because of the data folder
            logger.warning("Could not move fork data folder {} to {}: {}", legacy, target, exc)


def fork_data_dir(data_folder: Union[str, Path]) -> Path:
    """``<data folder>/plus``; migrates a legacy folder first. The folder is not created."""
    migrate_legacy_dir(data_folder)
    return Path(data_folder) / FORK_DIR_NAME
