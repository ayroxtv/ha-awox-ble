"""Config flow: AwoX cloud login (once) or manual mesh credentials, plus BLE discovery."""

from __future__ import annotations

import logging
from typing import Any

import probatio as vol
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_MAC, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud import AwoxCloud, CloudAuthError, CloudError
from .const import (
    CONF_DEVICES,
    CONF_GATEWAY_MACS,
    CONF_MESH_KEY,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    DOMAIN,
    mesh_unique_id,
)
from .mesh import NodeNotFoundError, async_probe_mesh
from .protocol import MeshAuthError, ProtocolError
from .protocol.constants import MESH_CREDENTIAL_MAX_LEN

_LOGGER = logging.getLogger(__name__)


def _normalize_mac(value: str) -> str:
    raw = value.strip().replace("-", ":").upper()
    parts = raw.split(":")
    if len(parts) != 6 or not all(len(p) == 2 for p in parts):
        raise vol.Invalid("invalid_mac")
    int("".join(parts), 16)
    return raw


class AwoxBleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered_mac: str | None = None
        self._discovered_name: str | None = None

    def _known_macs(self) -> set[str]:
        macs: set[str] = set()
        for entry in self._async_current_entries(include_ignore=False):
            macs |= {str(d.get("mac", "")).upper() for d in entry.data.get(CONF_DEVICES, [])}
            macs |= {str(m).upper() for m in entry.data.get(CONF_GATEWAY_MACS, [])}
        return macs

    # -- discovery ----------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        mac = discovery_info.address.upper()
        if mac in self._known_macs():
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(f"discovery-{mac}")
        self._abort_if_unique_id_configured()
        self._discovered_mac = mac
        self._discovered_name = discovery_info.name or mac
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_user()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovered_name or "",
                "mac": self._discovered_mac or "",
            },
        )

    # -- user -----------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["cloud", "manual"])

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cloud = AwoxCloud(async_get_clientsession(self.hass))
            try:
                await cloud.login(
                    user_input[CONF_USERNAME].strip().lower(), user_input[CONF_PASSWORD]
                )
                mesh = await cloud.fetch_mesh()
            except CloudAuthError:
                errors["base"] = "invalid_auth"
            except CloudError as err:
                _LOGGER.warning("AwoX cloud error: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not mesh.devices:
                    return self.async_abort(reason="no_devices_found")
                await self.async_set_unique_id(
                    mesh_unique_id(mesh.mesh_name), raise_on_progress=False
                )
                self._abort_if_unique_id_configured()
                # The AwoX account password is never stored: only the mesh credentials.
                return self.async_create_entry(
                    title=f"AwoX mesh ({len(mesh.devices)})",
                    data={
                        CONF_MESH_NAME: mesh.mesh_name,
                        CONF_MESH_PASSWORD: mesh.mesh_password,
                        CONF_MESH_KEY: mesh.mesh_key,
                        CONF_DEVICES: [d.as_dict() for d in mesh.devices],
                        CONF_GATEWAY_MACS: [self._discovered_mac] if self._discovered_mac else [],
                    },
                )
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_MESH_NAME]
            password = user_input[CONF_MESH_PASSWORD]
            mac = ""
            if len(name.encode()) > MESH_CREDENTIAL_MAX_LEN:
                errors[CONF_MESH_NAME] = "max_length_16"
            if len(password.encode()) > MESH_CREDENTIAL_MAX_LEN:
                errors[CONF_MESH_PASSWORD] = "max_length_16"
            try:
                mac = _normalize_mac(user_input[CONF_MAC])
            except (vol.Invalid, ValueError):
                errors[CONF_MAC] = "invalid_mac"
            if not errors:
                await self.async_set_unique_id(mesh_unique_id(name), raise_on_progress=False)
                self._abort_if_unique_id_configured()
                try:
                    mesh_ids = await async_probe_mesh(self.hass, mac, name, password)
                except NodeNotFoundError:
                    errors["base"] = "device_not_found"
                except MeshAuthError:
                    errors["base"] = "invalid_auth"
                except (BleakError, TimeoutError, ProtocolError) as err:
                    _LOGGER.warning("Probe of %s failed: %s", mac, err)
                    errors["base"] = "cannot_connect"
                else:
                    if not mesh_ids:
                        errors["base"] = "no_status"
                    else:
                        return self.async_create_entry(
                            title=f"AwoX mesh ({len(mesh_ids)})",
                            data={
                                CONF_MESH_NAME: name,
                                CONF_MESH_PASSWORD: password,
                                CONF_MESH_KEY: user_input.get(CONF_MESH_KEY, ""),
                                CONF_DEVICES: [
                                    {
                                        "mesh_id": mid,
                                        "name": f"AwoX {mid}",
                                        "mac": None,
                                        "type": "",
                                    }
                                    for mid in sorted(mesh_ids)
                                ],
                                CONF_GATEWAY_MACS: [mac],
                            },
                        )
        defaults = user_input or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MESH_NAME, default=defaults.get(CONF_MESH_NAME, "")): str,
                    vol.Required(CONF_MESH_PASSWORD): str,
                    vol.Optional(CONF_MESH_KEY): str,
                    vol.Required(
                        CONF_MAC, default=defaults.get(CONF_MAC, self._discovered_mac or "")
                    ): str,
                }
            ),
            errors=errors,
        )
