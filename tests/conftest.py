"""Shared fixtures: a fake Bluetooth environment around the real protocol."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from .fake_device import FakeLight, FakeMeshClient, ble_device

MAC_1 = "A4:C1:38:12:34:56"
MAC_2 = "A4:C1:38:00:00:02"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def bluetooth_ready(hass: HomeAssistant) -> None:
    """Pretend the HA Bluetooth stack is set up (we fake its API below)."""
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})


@dataclass
class FakeBluetooth:
    """Nodes in range + what establish_connection does for each."""

    lights: dict[int, FakeLight] = field(default_factory=lambda: {1: FakeLight(1), 2: FakeLight(2)})
    in_range: dict[str, tuple[int, str]] = field(default_factory=dict)  # mac -> (rssi, source)
    connect_errors: dict[str, list[BaseException]] = field(default_factory=dict)
    clients: list[FakeMeshClient] = field(default_factory=list)
    connect_calls: list[str] = field(default_factory=list)
    client_kwargs: dict[str, Any] = field(default_factory=dict)

    def last_service_info(self, _hass: Any, mac: str, connectable: bool = True) -> Any:
        if mac not in self.in_range:
            return None
        rssi, source = self.in_range[mac]
        return SimpleNamespace(address=mac, rssi=rssi, source=source)

    def ble_device_from_address(self, _hass: Any, mac: str, connectable: bool = True) -> Any:
        return ble_device(mac) if mac in self.in_range else None

    async def establish_connection(
        self, _cls: Any, device: Any, _name: str, disconnected_callback: Any = None, **_: Any
    ) -> FakeMeshClient:
        self.connect_calls.append(device.address)
        errors = self.connect_errors.get(device.address)
        if errors:
            raise errors.pop(0)
        client = FakeMeshClient(
            device.address,
            self.lights,
            disconnected_callback=disconnected_callback,
            **self.client_kwargs,
        )
        self.clients.append(client)
        return client


@pytest.fixture
def fake_bt() -> Generator[FakeBluetooth]:
    fake = FakeBluetooth()
    fake.in_range[MAC_1] = (-60, "hci0")
    with (
        patch(
            "custom_components.awox_ble.mesh.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.awox_ble.mesh.bluetooth.async_last_service_info",
            side_effect=fake.last_service_info,
        ),
        patch(
            "custom_components.awox_ble.mesh.bluetooth.async_ble_device_from_address",
            side_effect=fake.ble_device_from_address,
        ),
        patch(
            "custom_components.awox_ble.mesh.establish_connection",
            side_effect=fake.establish_connection,
        ),
    ):
        yield fake
