#!/usr/bin/env python3
"""Stand-alone test tool for AwoX / EGLO Telink mesh lights (bleak only, no HA).

Examples
--------
  # 1. list BLE adverts (also shows which ones carry the light's MAC)
  python tools/awox_cli.py scan --mac a4:c1:38:12:34:56

  # 2. fetch mesh credentials once from the AwoX cloud (saved chmod 600)
  python tools/awox_cli.py cloud --username me@example.com --save mesh.json

  # 3. drive the light and measure latency
  python tools/awox_cli.py run --config mesh.json --mac a4:c1:38:12:34:56 \
      on brightness 128 color 255 0 0 temp 3000 off listen 5
  python tools/awox_cli.py bench --config mesh.json --mac a4:c1:38:12:34:56 --count 20

Credentials can also come from AWOX_MESH_NAME / AWOX_MESH_PASSWORD.

macOS: CoreBluetooth hides MAC addresses, but the Telink nonce needs the MAC of
the node we connect to. Pass ``--mac`` (from the cloud data / the HA config);
the tool finds the matching device in the adverts (the manufacturer data of
Telink nodes carries the low 4 MAC bytes) or by ``--name`` / ``--address``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "awox_ble"))

from cloud import AwoxCloud
from protocol import DEST_ALL, LoginHandshake, MeshSession, Opcode, convert, packets, parse_status
from protocol.constants import (
    COMMAND_CHAR_UUID,
    ENABLE_NOTIFICATIONS,
    PAIR_CHAR_UUID,
    STATUS_CHAR_UUID,
)
from protocol.crypto import mac_to_reversed_bytes
from protocol.packets import LightStatus


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


Predicate = Callable[[LightStatus], bool]


def field_is(name: str, value: object) -> Predicate:
    def check(status: LightStatus) -> bool:
        return bool(getattr(status, name) == value)

    return check


def mac_hint(mac: str) -> bytes:
    return mac_to_reversed_bytes(mac)[0:4]


def adv_matches_mac(adv: AdvertisementData, mac: str) -> bool:
    hint = mac_hint(mac)
    return any(hint in bytes(v) for v in adv.manufacturer_data.values())


# --------------------------------------------------------------------- scan


async def cmd_scan(args: argparse.Namespace) -> int:
    found: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

    def cb(device: BLEDevice, adv: AdvertisementData) -> None:
        found[device.address] = (device, adv)

    log(f"Scanning {args.seconds}s…")
    async with BleakScanner(cb):
        await asyncio.sleep(args.seconds)
    rows = sorted(found.values(), key=lambda x: x[1].rssi, reverse=True)
    for device, adv in rows:
        telink = device.address.upper().startswith("A4:C1") or any(
            k in (0x0211, 0x0160) for k in adv.manufacturer_data
        )
        match = bool(args.mac) and adv_matches_mac(adv, args.mac)
        if not (args.all or telink or match or (args.name and adv.local_name == args.name)):
            continue
        flag = " <== MAC match" if match else ""
        print(f"{device.address}  rssi={adv.rssi:4d}  name={adv.local_name!r}{flag}")
        for company, data in adv.manufacturer_data.items():
            print(f"    manufacturer 0x{company:04x}: {bytes(data).hex(' ')}")
        for svc in adv.service_uuids:
            print(f"    service {svc}")
        for svc, data in adv.service_data.items():
            print(f"    service_data {svc}: {bytes(data).hex(' ')}")
    return 0


# -------------------------------------------------------------------- cloud


async def cmd_cloud(args: argparse.Namespace) -> int:
    password = os.environ.get("AWOX_PASSWORD") or getpass.getpass("AwoX password: ")
    async with aiohttp.ClientSession() as session:
        cloud = AwoxCloud(session)
        await cloud.login(args.username.lower(), password)
        mesh = await cloud.fetch_mesh()

    def secret(value: str) -> str:
        if args.show_secrets:
            return value
        return value[:2] + "…" if value else ""

    print(f"mesh name: {secret(mesh.mesh_name)}  password: {secret(mesh.mesh_password)}")
    for dev in mesh.devices:
        print(f"  mesh_id={dev.mesh_id:<5} mac={dev.mac} {dev.name!r} {dev.model} {dev.type}")
    if args.save:
        path = Path(args.save)
        path.write_text(
            json.dumps(
                {
                    "mesh_name": mesh.mesh_name,
                    "mesh_password": mesh.mesh_password,
                    "mesh_key": mesh.mesh_key,
                    "devices": [d.as_dict() for d in mesh.devices],
                },
                indent=2,
            )
        )
        path.chmod(0o600)
        log(f"saved to {path} (chmod 600, keep it out of git)")
    return 0


# ---------------------------------------------------------------------- run


class Link:
    """Connected, logged-in gateway with notification bookkeeping."""

    def __init__(self, client: BleakClient, session: MeshSession) -> None:
        self.client = client
        self.session = session
        self.statuses: dict[int, LightStatus] = {}
        self._waiters: list[tuple[Callable[[LightStatus], bool], asyncio.Future[LightStatus]]] = []

    def on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        clear = self.session.decode(bytes(data))
        if clear is None:
            log(f"  notify (bad checksum) {bytes(data).hex()}")
            return
        status = parse_status(clear)
        if status is None:
            log(f"  notify opcode 0x{clear[7]:02x}: {clear.hex(' ')}")
            return
        self.statuses[status.mesh_id] = status
        log(
            f"  {status.source:12s} mesh_id={status.mesh_id} on={status.state} "
            f"color={status.color_mode} wb={status.white_brightness} "
            f"temp={status.white_temperature} cb={status.color_brightness} "
            f"rgb=({status.red},{status.green},{status.blue}) online={status.online}"
        )
        for pred, fut in list(self._waiters):
            if not fut.done() and pred(status):
                fut.set_result(status)

    async def send(
        self,
        dest: int,
        opcode: int,
        data: bytes,
        expect: Callable[[LightStatus], bool] | None = None,
        timeout: float = 2.0,
    ) -> tuple[float, float | None]:
        """Return (write_ms, notify_ms or None)."""
        fut: asyncio.Future[LightStatus] = asyncio.get_running_loop().create_future()
        if expect:
            self._waiters.append((lambda s: s.mesh_id == dest and expect(s), fut))
        t0 = time.perf_counter()
        await self.client.write_gatt_char(
            COMMAND_CHAR_UUID, self.session.encode(dest, opcode, data), response=False
        )
        t_write = (time.perf_counter() - t0) * 1000
        t_notify = None
        if expect:
            try:
                await asyncio.wait_for(fut, timeout)
                t_notify = (time.perf_counter() - t0) * 1000
            except TimeoutError:
                pass
            finally:
                self._waiters = [w for w in self._waiters if w[1] is not fut]
        return t_write, t_notify


def load_credentials(args: argparse.Namespace) -> tuple[bytes, bytes, int | None]:
    name = os.environ.get("AWOX_MESH_NAME")
    password = os.environ.get("AWOX_MESH_PASSWORD")
    mesh_id: int | None = args.mesh_id
    if args.config:
        cfg: dict[str, Any] = json.loads(Path(args.config).read_text())
        name, password = cfg["mesh_name"], cfg["mesh_password"]
        if mesh_id is None:
            mesh_id = next(
                (
                    d["mesh_id"]
                    for d in cfg.get("devices", [])
                    if d["mac"].upper() == args.mac.upper()
                ),
                None,
            )
    if not name or not password:
        sys.exit("missing mesh credentials (--config or AWOX_MESH_NAME/AWOX_MESH_PASSWORD)")
    return name.encode(), password.encode(), mesh_id


async def find_device(args: argparse.Namespace) -> BLEDevice:
    target = args.address or (args.mac if sys.platform != "darwin" else None)
    t0 = time.perf_counter()

    def match(device: BLEDevice, adv: AdvertisementData) -> bool:
        if target and device.address.upper() == target.upper():
            return True
        if args.name and adv.local_name == args.name:
            return True
        return adv_matches_mac(adv, args.mac)

    device = await BleakScanner.find_device_by_filter(match, timeout=args.scan_timeout)
    if device is None:
        sys.exit("device not found (try `scan --mac …` and pass --address or --name)")
    log(f"found {device.address} ({device.name}) in {(time.perf_counter() - t0) * 1000:.0f} ms")
    return device


async def open_link(args: argparse.Namespace) -> tuple[Link, int | None]:
    name, password, mesh_id = load_credentials(args)
    device = await find_device(args)
    t0 = time.perf_counter()
    client = await establish_connection(
        BleakClientWithServiceCache, device, "awox-cli", max_attempts=3
    )
    t_conn = time.perf_counter()
    handshake = LoginHandshake(name, password)
    await client.write_gatt_char(PAIR_CHAR_UUID, handshake.request(), response=True)
    reply = await client.read_gatt_char(PAIR_CHAR_UUID)
    session = MeshSession(handshake.complete(bytes(reply)), args.mac)
    t_login = time.perf_counter()
    link = Link(client, session)
    await client.start_notify(STATUS_CHAR_UUID, link.on_notify)
    await client.write_gatt_char(STATUS_CHAR_UUID, ENABLE_NOTIFICATIONS, response=True)
    log(f"connected in {(t_conn - t0) * 1000:.0f} ms, login in {(t_login - t_conn) * 1000:.0f} ms")
    await link.send(DEST_ALL, Opcode.REQUEST_STATUS, packets.request_status_data(), expect=None)
    await asyncio.sleep(1.0)
    if mesh_id is None and link.statuses:
        mesh_id = next(iter(link.statuses))
        log(f"using mesh_id {mesh_id} (first status received)")
    return link, mesh_id


async def cmd_run(args: argparse.Namespace) -> int:
    link, mesh_id = await open_link(args)
    if mesh_id is None:
        sys.exit("unknown mesh id: pass --mesh-id")
    actions = list(args.actions)
    try:
        while actions:
            action = actions.pop(0)
            write_ms: float
            notify_ms: float | None
            if action in ("on", "off"):
                on = action == "on"
                write_ms, notify_ms = await link.send(
                    mesh_id, Opcode.POWER, packets.power_data(on), field_is("state", on)
                )
            elif action == "brightness":
                value = int(actions.pop(0))
                st = link.statuses.get(mesh_id)
                if st and st.color_mode:
                    dev = convert.ha_to_color_brightness(value)
                    write_ms, notify_ms = await link.send(
                        mesh_id,
                        Opcode.COLOR_BRIGHTNESS,
                        packets.byte_data(dev),
                        field_is("color_brightness", dev),
                    )
                else:
                    dev = convert.ha_to_white_brightness(value)
                    write_ms, notify_ms = await link.send(
                        mesh_id,
                        Opcode.WHITE_BRIGHTNESS,
                        packets.byte_data(dev),
                        field_is("white_brightness", dev),
                    )
            elif action == "color":
                r, g, b = (int(actions.pop(0)) for _ in range(3))
                write_ms, notify_ms = await link.send(
                    mesh_id, Opcode.COLOR, packets.color_data(r, g, b), field_is("color_mode", True)
                )
            elif action == "temp":
                dev = convert.kelvin_to_white_temperature(int(actions.pop(0)))
                write_ms, notify_ms = await link.send(
                    mesh_id,
                    Opcode.WHITE_TEMPERATURE,
                    packets.byte_data(dev),
                    field_is("white_temperature", dev),
                )
            elif action == "status":
                write_ms, notify_ms = await link.send(
                    mesh_id,
                    Opcode.REQUEST_STATUS,
                    packets.request_status_data(),
                    field_is("source", "status"),
                )
            elif action == "listen":
                seconds = float(actions.pop(0))
                log(f"listening {seconds}s for notifications (change the light from the app)…")
                await asyncio.sleep(seconds)
                continue
            else:
                sys.exit(f"unknown action {action!r}")
            note = f"{notify_ms:.0f} ms" if notify_ms is not None else "no matching notification"
            log(f"{action}: write {write_ms:.0f} ms, confirmed by notification after {note}")
            await asyncio.sleep(args.pause)
    finally:
        await link.client.disconnect()
    return 0


async def cmd_bench(args: argparse.Namespace) -> int:
    link, mesh_id = await open_link(args)
    if mesh_id is None:
        sys.exit("unknown mesh id: pass --mesh-id")
    samples: list[float] = []
    misses = 0
    try:
        for i in range(args.count):
            on = i % 2 == 0
            _, notify_ms = await link.send(
                mesh_id, Opcode.POWER, packets.power_data(on), field_is("state", on)
            )
            if notify_ms is None:
                misses += 1
            else:
                samples.append(notify_ms)
            await asyncio.sleep(args.pause)
    finally:
        await link.client.disconnect()
    if samples:
        samples.sort()
        p95 = samples[max(0, round(0.95 * len(samples)) - 1)]
        print(
            f"n={len(samples)} misses={misses} min={samples[0]:.0f} ms "
            f"median={statistics.median(samples):.0f} ms p95={p95:.0f} ms max={samples[-1]:.0f} ms"
        )
        print("target < 300 ms:", "OK" if p95 < 300 else "NOT MET")
    else:
        print(f"no confirmed command ({misses} misses)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="list BLE adverts")
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--mac", help="highlight adverts carrying this MAC")
    p.add_argument("--name")
    p.add_argument("--all", action="store_true", help="show every device")

    p = sub.add_parser("cloud", help="fetch mesh credentials from the AwoX cloud")
    p.add_argument("--username", required=True)
    p.add_argument("--save", help="write credentials + devices to this JSON file")
    p.add_argument("--show-secrets", action="store_true")

    for name in ("run", "bench"):
        p = sub.add_parser(name)
        p.add_argument("--mac", required=True, help="real MAC of the node to connect to")
        p.add_argument("--address", help="bleak address (CoreBluetooth UUID on macOS)")
        p.add_argument("--name", help="advertised local name")
        p.add_argument("--config", help="JSON from `cloud --save`")
        p.add_argument("--mesh-id", type=int)
        p.add_argument("--scan-timeout", type=float, default=15)
        p.add_argument("--pause", type=float, default=0.5)
        if name == "run":
            p.add_argument("actions", nargs="*", default=["status"])
        else:
            p.add_argument("--count", type=int, default=20)

    args = parser.parse_args()
    handler = {"scan": cmd_scan, "cloud": cmd_cloud, "run": cmd_run, "bench": cmd_bench}[args.cmd]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
