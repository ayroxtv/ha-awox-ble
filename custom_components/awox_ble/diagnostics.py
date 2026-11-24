"""Diagnostics with mesh credentials redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AwoxConfigEntry
from .const import REDACT_KEYS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AwoxConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), REDACT_KEYS),
        "mesh": runtime.mesh.diagnostics(),
        "available": runtime.coordinator.available,
        "statuses": {str(mid): asdict(st) for mid, st in runtime.coordinator.statuses.items()},
    }
