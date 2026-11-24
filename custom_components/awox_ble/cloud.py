"""AwoX cloud (Parse server) client, used once during configuration.

Ported from home-assistant-awox ``awox_connect.py`` (c) 2020 Frans Saris, MIT,
moved from ``requests`` to async ``aiohttp``. No Home Assistant import so the
CLI can reuse it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp

CONNECT_URL = "https://l4hparse-prod.awox.cloud/parse/"
HOME_CONTROL_URL = "https://l4hparse-hc-prod.awox.cloud/parse/"
APPLICATION_ID = "55O69FLtoxPt67LLwaHGpHmVWndhZGn9Wty8PLrJ"
CLIENT_KEY = "PyR3yV65rytEicteNlQHSVNpAGvCByOrsLiEqJtI"
TIMEOUT = aiohttp.ClientTimeout(total=20)


class CloudError(Exception):
    """Generic cloud failure."""


class CloudAuthError(CloudError):
    """Bad AwoX account credentials."""


@dataclass(frozen=True, slots=True)
class CloudDevice:
    mesh_id: int
    name: str
    mac: str
    model: str
    manufacturer: str
    firmware: str
    hardware: str | None
    type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "mesh_id": self.mesh_id,
            "name": self.name,
            "mac": self.mac,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "firmware": self.firmware,
            "hardware": self.hardware,
            "type": self.type,
        }


@dataclass(frozen=True, slots=True)
class CloudMesh:
    mesh_name: str
    mesh_password: str
    mesh_key: str
    devices: list[CloudDevice]


def parse_devices(raw: list[dict[str, Any]]) -> list[CloudDevice]:
    """Keep Telink mesh devices only (same filter as the legacy integration)."""
    devices: list[CloudDevice] = []
    for item in raw:
        dev_type = item.get("type")
        if not isinstance(dev_type, str) or not dev_type.startswith(".ble.tlmesh"):
            continue
        if not item.get("address") or "macAddress" not in item or "displayName" not in item:
            continue
        devices.append(
            CloudDevice(
                mesh_id=int(item["address"]),
                name=str(item["displayName"]),
                mac=str(item["macAddress"]).upper(),
                model=str(item.get("modelName", "unknown")),
                manufacturer=str(item.get("vendor", "unknown")),
                firmware=str(item.get("version", "unknown")),
                hardware=item.get("hardwareVersion"),
                type=dev_type,
            )
        )
    return devices


class AwoxCloud:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._installation_id = str(uuid.uuid4())
        self._api_url = CONNECT_URL
        self._object_id: str | None = None
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "x-parse-application-id": APPLICATION_ID,
            "x-parse-installation-id": self._installation_id,
            "x-parse-client-key": CLIENT_KEY,
            "content-type": "application/json",
        }
        if self._token:
            headers["x-parse-session-token"] = self._token
        return headers

    async def _post(self, url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            async with self._session.post(
                url, json=payload, headers=self._headers(), timeout=TIMEOUT
            ) as resp:
                data = await resp.json(content_type=None)
                return resp.status, data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CloudError(f"AwoX cloud unreachable: {err}") from err

    async def login(self, username: str, password: str) -> None:
        payload = {"username": username, "password": password, "_method": "GET"}
        for url in (CONNECT_URL, HOME_CONTROL_URL):
            status, data = await self._post(url + "login", payload)
            if status == 200 and "sessionToken" in data:
                self._api_url = url
                self._object_id = str(data["objectId"])
                self._token = str(data["sessionToken"])
                return
        raise CloudAuthError("AwoX login refused")

    async def _fetch_class(self, class_name: str) -> list[dict[str, Any]]:
        if self._object_id is None:
            raise CloudError("not logged in")
        payload = {
            "where": {
                "owner": {"__type": "Pointer", "className": "_User", "objectId": self._object_id}
            },
            "_method": "GET",
        }
        status, data = await self._post(self._api_url + "classes/" + class_name, payload)
        if status != 200:
            raise CloudError(f"loading {class_name} failed (HTTP {status})")
        results = data.get("results", [])
        return [r for r in results if isinstance(r, dict)]

    async def fetch_mesh(self) -> CloudMesh:
        creds = next(
            (c for c in await self._fetch_class("Credential") if c.get("service") == "mesh"), None
        )
        if creds is None:
            raise CloudError("no mesh credentials in this AwoX account")
        return CloudMesh(
            mesh_name=str(creds["client_id"]),
            mesh_password=str(creds["access_token"]),
            mesh_key=str(creds.get("refresh_token", "")),
            devices=parse_devices(await self._fetch_class("Device")),
        )
