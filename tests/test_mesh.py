"""MeshManager against the fake Telink node (real crypto, mocked bleak)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import patch

import pytest
from bleak_retry_connector import BleakOutOfConnectionSlotsError
from homeassistant.core import HomeAssistant

from custom_components.awox_ble import mesh as mesh_mod
from custom_components.awox_ble.mesh import MeshManager
from custom_components.awox_ble.protocol import LightStatus, MeshAuthError, Opcode
from custom_components.awox_ble.protocol.constants import COMMAND_CHAR_UUID, STATUS_CHAR_UUID
from custom_components.awox_ble.protocol.packets import power_data

from .conftest import MAC_1, MAC_2, FakeBluetooth
from .fake_device import MESH_NAME, MESH_PASSWORD


class Recorder:
    def __init__(self) -> None:
        self.statuses: list[LightStatus] = []
        self.connection: list[bool] = []
        self.changed = asyncio.Event()

    def on_status(self, status: LightStatus) -> None:
        self.statuses.append(status)
        self.changed.set()

    def on_connection(self, connected: bool) -> None:
        self.connection.append(connected)
        self.changed.set()

    async def wait_for(self, pred: Callable[[], bool], timeout: float = 3.0) -> None:
        async with asyncio.timeout(timeout):
            while not pred():
                self.changed.clear()
                await self.changed.wait()


@pytest.fixture
async def manager(
    hass: HomeAssistant, fake_bt: FakeBluetooth
) -> AsyncGenerator[tuple[MeshManager, Recorder]]:
    rec = Recorder()
    mgr = MeshManager(
        hass,
        mesh_name=MESH_NAME,
        mesh_password=MESH_PASSWORD,
        node_macs=[MAC_1, MAC_2],
        on_status=rec.on_status,
        on_connection_change=rec.on_connection,
        label="test",
    )
    mgr.async_start(lambda coro, name: hass.async_create_background_task(coro, name))
    yield mgr, rec
    await mgr.async_stop()


async def test_connect_login_and_initial_status(
    manager: tuple[MeshManager, Recorder], fake_bt: FakeBluetooth
) -> None:
    mgr, rec = manager
    await rec.wait_for(lambda: {s.mesh_id for s in rec.statuses} == {1, 2})
    client = fake_bt.clients[0]
    assert mgr.connected
    assert mgr.gateway_mac == MAC_1
    assert client.notifications_enabled
    assert (STATUS_CHAR_UUID, b"\x01") in client.written
    # exactly one status request (the only "poll"), sent to every node
    status_requests = [c for c in client.commands if c[2] == Opcode.REQUEST_STATUS]
    assert len(status_requests) == 1
    assert status_requests[0][0:2] == b"\xff\xff"
    assert all(s.source == "status" for s in rec.statuses)


async def test_command_is_encrypted_and_state_pushed_back(
    manager: tuple[MeshManager, Recorder], fake_bt: FakeBluetooth
) -> None:
    mgr, rec = manager
    await rec.wait_for(lambda: mgr.connected)
    t0 = time.monotonic()
    mgr.async_send(2, Opcode.POWER, power_data(True))
    await rec.wait_for(lambda: any(s.mesh_id == 2 and s.state for s in rec.statuses))
    assert time.monotonic() - t0 < 0.3
    client = fake_bt.clients[0]
    assert client.commands[-1][0:3] == bytes([2, 0, Opcode.POWER])
    assert fake_bt.lights[2].on
    assert rec.statuses[-1].source == "notification"


async def test_unsolicited_notification_updates_state(
    manager: tuple[MeshManager, Recorder], fake_bt: FakeBluetooth
) -> None:
    """Change made from the AwoX app: the node pushes 0xDC, no polling involved."""
    mgr, rec = manager
    await rec.wait_for(lambda: mgr.connected and len(rec.statuses) >= 2)
    light = fake_bt.lights[1]
    light.on, light.color_mode, light.rgb = True, True, (255, 0, 0)
    fake_bt.clients[0].emit_online_status(light)
    await rec.wait_for(lambda: rec.statuses[-1].red == 255)
    assert rec.statuses[-1].color_mode
    assert rec.statuses[-1].mesh_id == 1


async def test_reconnects_quickly_after_drop(
    manager: tuple[MeshManager, Recorder], fake_bt: FakeBluetooth
) -> None:
    mgr, rec = manager
    await rec.wait_for(lambda: mgr.connected)
    t0 = time.monotonic()
    fake_bt.clients[0].drop()
    await rec.wait_for(lambda: rec.connection[-2:] == [False, True], timeout=5)
    assert time.monotonic() - t0 < 5
    assert len(fake_bt.clients) == 2
    assert mgr.stats.last_reconnect_gap_seconds is not None
    assert mgr.stats.last_reconnect_gap_seconds < 5


async def test_command_queued_while_disconnected_is_sent_after_reconnect(
    manager: tuple[MeshManager, Recorder], fake_bt: FakeBluetooth
) -> None:
    mgr, rec = manager
    await rec.wait_for(lambda: mgr.connected)
    fake_bt.clients[0].drop()
    await rec.wait_for(lambda: not mgr.connected)
    mgr.async_send(1, Opcode.POWER, power_data(True))  # must not block
    await rec.wait_for(lambda: fake_bt.lights[1].on, timeout=5)


async def test_failed_write_keeps_command_and_reconnects(
    manager: tuple[MeshManager, Recorder], fake_bt: FakeBluetooth
) -> None:
    mgr, rec = manager
    await rec.wait_for(lambda: mgr.connected)
    fake_bt.clients[0].fail_writes = True
    mgr.async_send(1, Opcode.POWER, power_data(True))
    await rec.wait_for(lambda: fake_bt.lights[1].on, timeout=5)
    assert len(fake_bt.clients) == 2


async def test_prefers_best_rssi_and_fails_over(
    hass: HomeAssistant, fake_bt: FakeBluetooth
) -> None:
    fake_bt.in_range[MAC_2] = (-40, "esphome-proxy")  # better than MAC_1 (-60)
    fake_bt.connect_errors[MAC_2] = [BleakOutOfConnectionSlotsError("no slot")]
    rec = Recorder()
    mgr = MeshManager(
        hass,
        mesh_name=MESH_NAME,
        mesh_password=MESH_PASSWORD,
        node_macs=[MAC_1, MAC_2],
        on_status=rec.on_status,
        on_connection_change=rec.on_connection,
    )
    mgr.async_start(lambda coro, name: hass.async_create_background_task(coro, name))
    try:
        await rec.wait_for(lambda: mgr.connected)
        assert fake_bt.connect_calls[:2] == [MAC_2, MAC_1]
        assert mgr.gateway_mac == MAC_1
        assert mgr.stats.slot_exhausted == 1
    finally:
        await mgr.async_stop()


async def test_auth_failure_bans_node(hass: HomeAssistant, fake_bt: FakeBluetooth) -> None:
    fake_bt.in_range[MAC_2] = (-40, "hci0")
    fake_bt.connect_errors[MAC_2] = [MeshAuthError("refused")]
    rec = Recorder()
    mgr = MeshManager(
        hass,
        mesh_name=MESH_NAME,
        mesh_password=MESH_PASSWORD,
        node_macs=[MAC_1, MAC_2],
        on_status=rec.on_status,
        on_connection_change=rec.on_connection,
    )
    mgr.async_start(lambda coro, name: hass.async_create_background_task(coro, name))
    try:
        await rec.wait_for(lambda: mgr.connected)
        assert mgr.gateway_mac == MAC_1
        assert MAC_2 in mgr.diagnostics()["nodes_banned"]
    finally:
        await mgr.async_stop()


async def test_wrong_password_is_reported(hass: HomeAssistant, fake_bt: FakeBluetooth) -> None:
    rec = Recorder()
    mgr = MeshManager(
        hass,
        mesh_name=MESH_NAME,
        mesh_password="wrong",
        node_macs=[MAC_1],
        on_status=rec.on_status,
        on_connection_change=rec.on_connection,
    )
    mgr.async_start(lambda coro, name: hass.async_create_background_task(coro, name))
    try:
        async with asyncio.timeout(3):
            while mgr.stats.auth_failures == 0:
                await asyncio.sleep(0.01)
        assert not mgr.connected
    finally:
        await mgr.async_stop()


async def test_queue_coalesces_and_expires(hass: HomeAssistant) -> None:
    mgr = MeshManager(
        hass,
        mesh_name=MESH_NAME,
        mesh_password=MESH_PASSWORD,
        node_macs=[],
        on_status=lambda s: None,
        on_connection_change=lambda c: None,
    )
    mgr.async_send(1, Opcode.WHITE_BRIGHTNESS, b"\x10")
    mgr.async_send(1, Opcode.WHITE_BRIGHTNESS, b"\x20")
    mgr.async_send(2, Opcode.WHITE_BRIGHTNESS, b"\x30")
    assert [c.data for c in mgr._queue] == [b"\x20", b"\x30"]
    assert mgr.stats.commands_coalesced == 1


async def test_stale_commands_are_dropped(hass: HomeAssistant, fake_bt: FakeBluetooth) -> None:
    fake_bt.in_range.clear()
    rec = Recorder()
    mgr = MeshManager(
        hass,
        mesh_name=MESH_NAME,
        mesh_password=MESH_PASSWORD,
        node_macs=[MAC_1],
        on_status=rec.on_status,
        on_connection_change=rec.on_connection,
    )
    with patch.object(mesh_mod, "COMMAND_TTL", 0.1):
        mgr.async_send(1, Opcode.POWER, power_data(True))
        mgr.async_start(lambda coro, name: hass.async_create_background_task(coro, name))
        await asyncio.sleep(0.2)  # the command ages while no node is in range
        fake_bt.in_range[MAC_1] = (-50, "hci0")
        mgr._wake.set()
        try:
            await rec.wait_for(lambda: mgr.connected)
            await asyncio.sleep(0.2)
            assert not fake_bt.lights[1].on
            assert mgr.stats.commands_dropped == 1
        finally:
            await mgr.async_stop()


async def test_keepalive_detects_dead_link(hass: HomeAssistant, fake_bt: FakeBluetooth) -> None:
    rec = Recorder()
    with patch.object(mesh_mod, "KEEPALIVE_INTERVAL", 0.05):
        mgr = MeshManager(
            hass,
            mesh_name=MESH_NAME,
            mesh_password=MESH_PASSWORD,
            node_macs=[MAC_1],
            on_status=rec.on_status,
            on_connection_change=rec.on_connection,
        )
        mgr.async_start(lambda coro, name: hass.async_create_background_task(coro, name))
        try:
            await rec.wait_for(lambda: mgr.connected)
            fake_bt.clients[0].connected = False  # zombie: no disconnect callback
            await rec.wait_for(lambda: rec.connection[-2:] == [False, True], timeout=5)
        finally:
            await mgr.async_stop()


async def test_stop_cancels_cleanly(manager: tuple[MeshManager, Recorder], fake_bt: Any) -> None:
    mgr, rec = manager
    await rec.wait_for(lambda: mgr.connected)
    await mgr.async_stop()
    assert not mgr.connected
    assert not fake_bt.clients[0].connected
    assert (COMMAND_CHAR_UUID, b"") not in fake_bt.clients[0].written
