"""AwoX / EGLO connect Telink mesh lights over the Home Assistant Bluetooth stack."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from .const import (
    BLUEZ_MODE_STABLE,
    CONF_BLUEZ_MODE,
    CONF_DEVICES,
    CONF_GATEWAY_MACS,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    DOMAIN,
)
from .coordinator import AwoxMeshCoordinator
from .mesh import MeshManager

PLATFORMS: list[Platform] = [Platform.LIGHT]


@dataclass(slots=True)
class AwoxRuntimeData:
    coordinator: AwoxMeshCoordinator
    mesh: MeshManager


type AwoxConfigEntry = ConfigEntry[AwoxRuntimeData]


def node_macs(entry: ConfigEntry) -> list[str]:
    macs = [str(d["mac"]) for d in entry.data.get(CONF_DEVICES, []) if d.get("mac")]
    macs += [str(m) for m in entry.data.get(CONF_GATEWAY_MACS, [])]
    return sorted({m.upper() for m in macs})


async def async_setup_entry(hass: HomeAssistant, entry: AwoxConfigEntry) -> bool:
    label = f"{DOMAIN}:{entry.entry_id[-6:]}"
    coordinator = AwoxMeshCoordinator(hass, label)
    mesh = MeshManager(
        hass,
        mesh_name=entry.data[CONF_MESH_NAME],
        mesh_password=entry.data[CONF_MESH_PASSWORD],
        node_macs=node_macs(entry),
        on_status=coordinator.async_handle_status,
        on_connection_change=coordinator.async_handle_connection,
        label=label,
        bluez_mode=entry.options.get(CONF_BLUEZ_MODE, BLUEZ_MODE_STABLE),
    )
    coordinator.mesh = mesh
    coordinator.async_start_grace()
    entry.runtime_data = AwoxRuntimeData(coordinator=coordinator, mesh=mesh)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Background task: owned by the entry, cancelled on unload, never blocks startup.
    mesh.async_start(lambda coro, name: entry.async_create_background_task(hass, coro, name))

    async def _async_on_stop(_event: Event) -> None:
        await mesh.async_stop()
        coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AwoxConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.mesh.async_stop()
        entry.runtime_data.coordinator.async_shutdown()
    return unload_ok
