"""End-to-end: entry setup, light entity, push updates, diagnostics, unload."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.awox_ble.const import DOMAIN, mesh_unique_id
from custom_components.awox_ble.diagnostics import async_get_config_entry_diagnostics
from custom_components.awox_ble.protocol import Opcode

from .conftest import MAC_1, FakeBluetooth
from .fake_device import MESH_NAME, MESH_PASSWORD

pytestmark = pytest.mark.usefixtures("bluetooth_ready")

ENTITY = "light.salon"


def _state(hass: HomeAssistant, entity_id: str = ENTITY) -> State:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def _wait(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


@pytest.fixture
async def entry(hass: HomeAssistant, fake_bt: FakeBluetooth) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=mesh_unique_id(MESH_NAME),
        title="AwoX mesh (1)",
        data={
            "mesh_name": MESH_NAME,
            "mesh_password": MESH_PASSWORD,
            "mesh_key": "ltk",
            "devices": [
                {
                    "mesh_id": 1,
                    "name": "Salon",
                    "mac": MAC_1,
                    "model": "EPanel_120",
                    "manufacturer": "EGLO",
                    "firmware": "1.2",
                    "type": ".ble.tlmesh.light.dimming.temperature.color",
                },
                {"mesh_id": 9, "name": "Plug", "mac": None, "type": ".ble.tlmesh.plug"},
            ],
            "gateway_macs": [],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_push_state_and_commands(
    hass: HomeAssistant, entry: MockConfigEntry, fake_bt: FakeBluetooth
) -> None:
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("light.plug") is None  # plugs are skipped
    await _wait(lambda: _state(hass).state == STATE_OFF)

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": ENTITY, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    # optimistic immediately
    state = _state(hass)
    assert state.state == STATE_ON
    assert state.attributes["color_mode"] == "rgb"
    await _wait(lambda: fake_bt.lights[1].rgb == (255, 0, 0) and fake_bt.lights[1].on)
    await _wait(lambda: fake_bt.lights[1].color_brightness == 0x64)
    opcodes = [c[2] for c in fake_bt.clients[0].commands]
    assert Opcode.COLOR in opcodes
    assert Opcode.COLOR_BRIGHTNESS in opcodes

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ENTITY, "color_temp_kelvin": 6500}, blocking=True
    )
    await _wait(lambda: fake_bt.lights[1].white_temperature == 0)
    await _wait(lambda: _state(hass).attributes.get("color_mode") == "color_temp")

    await hass.services.async_call("light", "turn_off", {"entity_id": ENTITY}, blocking=True)
    await _wait(lambda: not fake_bt.lights[1].on)
    assert _state(hass).state == STATE_OFF


async def test_change_from_app_is_pushed(
    hass: HomeAssistant, entry: MockConfigEntry, fake_bt: FakeBluetooth
) -> None:
    await _wait(lambda: _state(hass).state == STATE_OFF)
    light = fake_bt.lights[1]
    light.on, light.white_brightness = True, 0x7F
    fake_bt.clients[0].emit_online_status(light)
    await _wait(lambda: _state(hass).state == STATE_ON)
    assert _state(hass).attributes["brightness"] == 255


async def test_unavailable_only_after_grace(
    hass: HomeAssistant, entry: MockConfigEntry, fake_bt: FakeBluetooth
) -> None:
    await _wait(lambda: _state(hass).state == STATE_OFF)
    fake_bt.in_range.clear()
    coord = entry.runtime_data.coordinator
    with patch("custom_components.awox_ble.coordinator.UNAVAILABLE_GRACE", 0.05):
        fake_bt.clients[0].drop()
        await _wait(lambda: _state(hass).state == STATE_UNAVAILABLE)
    assert not coord.available


async def test_diagnostics_redacts_secrets(
    hass: HomeAssistant, entry: MockConfigEntry, fake_bt: FakeBluetooth
) -> None:
    await _wait(lambda: entry.runtime_data.mesh.connected)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diag, default=str)
    assert MESH_NAME not in dumped
    assert MESH_PASSWORD not in dumped
    assert "ltk" not in dumped
    assert diag["mesh"]["connected"] is True
    assert diag["mesh"]["gateway_mac"] == MAC_1


async def test_secrets_never_logged(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    fake_bt: FakeBluetooth,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    await _wait(lambda: entry.runtime_data.mesh.connected)
    await hass.services.async_call("light", "turn_on", {"entity_id": ENTITY}, blocking=True)
    await _wait(lambda: fake_bt.lights[1].on)
    assert MESH_PASSWORD not in caplog.text
    assert MESH_NAME not in caplog.text
    key = fake_bt.clients[0].session_key
    assert key is not None
    assert key.hex() not in caplog.text


async def test_unload(hass: HomeAssistant, entry: MockConfigEntry, fake_bt: FakeBluetooth) -> None:
    await _wait(lambda: entry.runtime_data.mesh.connected)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not fake_bt.clients[0].connected
