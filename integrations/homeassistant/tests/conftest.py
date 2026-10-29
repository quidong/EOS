"""Test setup: Home Assistant tests only when pytest-homeassistant-custom-component is installed."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import pytest_homeassistant_custom_component  # noqa: F401

    HAS_HA = True
except ImportError:  # logic tests still run
    HAS_HA = False

if HAS_HA:
    pytest_plugins = ["pytest_homeassistant_custom_component"]


def pytest_ignore_collect(collection_path: Path, config) -> bool | None:
    if not HAS_HA and collection_path.name.startswith("test_ha_"):
        return True
    return None
