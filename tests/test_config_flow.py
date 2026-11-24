"""Config flow tests (cloud, manual, bluetooth discovery)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.awox_ble.cloud import CloudAuthError, CloudDevice, CloudError, CloudMesh
from custom_components.awox_ble.const import DOMAIN, mesh_unique_id
from custom_components.awox_ble.mesh import NodeNotFoundError
from custom_components.awox_ble.protocol import MeshAuthError

from .conftest import MAC_1

pytestmark = pytest.mark.usefixtures("bluetooth_ready")

CLOUD_MESH = CloudMesh(
    mesh_name="cloudmesh",
    mesh_password="cloudpass",
    mesh_key="ltk",
    devices=[
        CloudDevice(
            mesh_id=1,
            name="Salon",
            mac=MAC_1,
            model="EPanel_120",
            manufacturer="EGLO",
            firmware="1.2",
            hardware=None,
            type=".ble.tlmesh.light.dimming.temperature.color",
        )
    ],
)


def _patch_cloud(login: object = None, fetch: object = CLOUD_MESH) -> Any:
    cloud = SimpleNamespace(
        login=AsyncMock(side_effect=login if isinstance(login, Exception) else None),
        fetch_mesh=AsyncMock(
            side_effect=fetch if isinstance(fetch, Exception) else None,
            return_value=None if isinstance(fetch, Exception) else fetch,
        ),
    )
    return patch("custom_components.awox_ble.config_flow.AwoxCloud", return_value=cloud)


async def _start_user(hass: HomeAssistant, option: str) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == option
    return str(result["flow_id"])


async def test_cloud_success(hass: HomeAssistant) -> None:
    flow_id = await _start_user(hass, "cloud")
    with (
        _patch_cloud(),
        patch("custom_components.awox_ble.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"username": "Me@Example.com ", "password": "pw"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["mesh_name"] == "cloudmesh"
    assert data["mesh_password"] == "cloudpass"
    assert data["devices"][0]["mesh_id"] == 1
    assert "password" not in data  # the AwoX account password is not stored
    assert "username" not in data
    assert result["result"].unique_id == mesh_unique_id("cloudmesh")


@pytest.mark.parametrize(
    ("login", "fetch", "error"),
    [
        (CloudAuthError("no"), CLOUD_MESH, "invalid_auth"),
        (None, CloudError("down"), "cannot_connect"),
    ],
)
async def test_cloud_errors(hass: HomeAssistant, login: object, fetch: object, error: str) -> None:
    flow_id = await _start_user(hass, "cloud")
    with _patch_cloud(login, fetch):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"username": "a", "password": "b"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_cloud_already_configured(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=mesh_unique_id("cloudmesh"), data={}).add_to_hass(hass)
    flow_id = await _start_user(hass, "cloud")
    with _patch_cloud():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"username": "a", "password": "b"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_success(hass: HomeAssistant) -> None:
    flow_id = await _start_user(hass, "manual")
    with (
        patch(
            "custom_components.awox_ble.config_flow.async_probe_mesh",
            AsyncMock(return_value={3, 1}),
        ) as probe,
        patch("custom_components.awox_ble.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {"mesh_name": "m", "mesh_password": "p", "mac": "a4-c1-38-12-34-56"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    probe.assert_awaited_once_with(hass, MAC_1, "m", "p")
    assert [d["mesh_id"] for d in result["data"]["devices"]] == [1, 3]
    assert result["data"]["gateway_macs"] == [MAC_1]


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (NodeNotFoundError(MAC_1), "device_not_found"),
        (MeshAuthError("x"), "invalid_auth"),
        (BleakError("x"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
    ],
)
async def test_manual_probe_errors(hass: HomeAssistant, exc: Exception, error: str) -> None:
    flow_id = await _start_user(hass, "manual")
    with patch(
        "custom_components.awox_ble.config_flow.async_probe_mesh", AsyncMock(side_effect=exc)
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"mesh_name": "m", "mesh_password": "p", "mac": MAC_1}
        )
    assert result["errors"] == {"base": error}


async def test_manual_validation(hass: HomeAssistant) -> None:
    flow_id = await _start_user(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"mesh_name": "x" * 17, "mesh_password": "p", "mac": "nope"}
    )
    assert result["errors"] == {"mesh_name": "max_length_16", "mac": "invalid_mac"}


async def test_manual_no_status(hass: HomeAssistant) -> None:
    flow_id = await _start_user(hass, "manual")
    with patch(
        "custom_components.awox_ble.config_flow.async_probe_mesh", AsyncMock(return_value=set())
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"mesh_name": "m", "mesh_password": "p", "mac": MAC_1}
        )
    assert result["errors"] == {"base": "no_status"}


def _service_info(mac: str = MAC_1) -> SimpleNamespace:
    return SimpleNamespace(address=mac, name="EPanel_120")


async def test_bluetooth_discovery_leads_to_menu(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_service_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    schema = result["data_schema"]
    assert schema is not None
    assert schema({"mesh_name": "m", "mesh_password": "p"})["mac"] == MAC_1


async def test_bluetooth_discovery_of_known_node_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="abc",
        data={"devices": [{"mesh_id": 1, "mac": MAC_1}]},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_service_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_sets_bluez_mode(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="abc", data={"devices": []})
    entry.add_to_hass(hass)
    with patch("custom_components.awox_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"bluez_mode": "cycle"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"bluez_mode": "cycle"}
