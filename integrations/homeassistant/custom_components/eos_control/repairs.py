"""Repair issues of EOS Control (Settings -> System -> Repairs)."""

from __future__ import annotations

from typing import Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


def set_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    name: str,
    active: bool,
    placeholders: dict[str, str],
    translation_key: Optional[str] = None,
) -> None:
    """Create or delete the issue ``<entry>_<name>``."""
    issue_id = f"{entry.entry_id}_{name}"
    if not active:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key or name,
        translation_placeholders=placeholders,
    )
