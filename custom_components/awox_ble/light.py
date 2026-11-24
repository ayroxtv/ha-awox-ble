"""Light platform: optimistic update, then confirmed by mesh notifications."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AwoxConfigEntry
from .const import CONF_DEVICES, DOMAIN, MANUFACTURER
from .coordinator import AwoxMeshCoordinator
from .protocol import LightStatus, Opcode
from .protocol import convert as cv
from .protocol.constants import KELVIN_MAX, KELVIN_MIN
from .protocol.packets import byte_data, color_data, power_data

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def color_modes_for_type(type_string: str) -> set[ColorMode]:
    """Same mapping as the legacy integration; unknown type -> RGB + CT."""
    if not type_string:
        return {ColorMode.RGB, ColorMode.COLOR_TEMP}
    modes: set[ColorMode] = set()
    if "color" in type_string:
        modes.add(ColorMode.RGB)
    if "temperature" in type_string:
        modes.add(ColorMode.COLOR_TEMP)
    if not modes and "dimming" in type_string:
        modes.add(ColorMode.BRIGHTNESS)
    return modes or {ColorMode.ONOFF}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AwoxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = []
    for device in entry.data.get(CONF_DEVICES, []):
        dev_type = str(device.get("type") or "")
        if dev_type and "light" not in dev_type:
            continue  # plugs etc. are not handled (yet)
        entities.append(AwoxLight(coordinator, entry, device))
    async_add_entities(entities)


class AwoxLight(LightEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_min_color_temp_kelvin = KELVIN_MIN
    _attr_max_color_temp_kelvin = KELVIN_MAX

    def __init__(
        self, coordinator: AwoxMeshCoordinator, entry: AwoxConfigEntry, device: dict[str, Any]
    ) -> None:
        self._coordinator = coordinator
        self._mesh_id = int(device["mesh_id"])
        self._attr_unique_id = f"{entry.unique_id}-{self._mesh_id}"
        self._attr_supported_color_modes = color_modes_for_type(str(device.get("type") or ""))
        mac = device.get("mac")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            connections={(CONNECTION_BLUETOOTH, str(mac).upper())} if mac else set(),
            name=str(device.get("name") or f"AwoX {self._mesh_id}"),
            manufacturer=str(device.get("manufacturer") or MANUFACTURER),
            model=str(device.get("model") or "").replace("_", " ") or None,
            sw_version=device.get("firmware"),
            hw_version=device.get("hardware"),
        )
        self._online = True
        self._attr_is_on = None
        self._attr_color_mode = self._default_white_mode()

    # -- state ------------------------------------------------------------------

    def _default_white_mode(self) -> ColorMode:
        modes = self.supported_color_modes or set()
        for mode in (ColorMode.COLOR_TEMP, ColorMode.BRIGHTNESS, ColorMode.RGB):
            if mode in modes:
                return mode
        return ColorMode.ONOFF

    @property
    def available(self) -> bool:
        return self._coordinator.available and self._online

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self._mesh_id, self._handle_update)
        )
        if (status := self._coordinator.statuses.get(self._mesh_id)) is not None:
            self._apply_status(status)

    @callback
    def _handle_update(self) -> None:
        if (status := self._coordinator.statuses.get(self._mesh_id)) is not None:
            self._apply_status(status)
        self.async_write_ha_state()

    @callback
    def _apply_status(self, status: LightStatus) -> None:
        self._online = status.online
        self._attr_is_on = status.state
        modes = self.supported_color_modes or set()
        if status.color_mode and ColorMode.RGB in modes:
            self._attr_color_mode = ColorMode.RGB
            self._attr_rgb_color = (status.red, status.green, status.blue)
            self._attr_brightness = cv.color_brightness_to_ha(status.color_brightness)
        else:
            self._attr_color_mode = self._default_white_mode()
            self._attr_brightness = cv.white_brightness_to_ha(status.white_brightness)
            if ColorMode.RGB in modes:
                self._attr_rgb_color = (status.red, status.green, status.blue)
        if ColorMode.COLOR_TEMP in modes:
            self._attr_color_temp_kelvin = cv.white_temperature_to_kelvin(status.white_temperature)

    # -- commands ---------------------------------------------------------------

    def _send(self, opcode: int, data: bytes) -> None:
        self._coordinator.async_send(self._mesh_id, opcode, data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        was_on = self._attr_is_on
        attribute_sent = False

        if (rgb := kwargs.get(ATTR_RGB_COLOR)) is not None:
            self._send(Opcode.COLOR, color_data(*rgb))
            self._attr_rgb_color = rgb
            self._attr_color_mode = ColorMode.RGB
            attribute_sent = True

        if (kelvin := kwargs.get(ATTR_COLOR_TEMP_KELVIN)) is not None:
            self._send(Opcode.WHITE_TEMPERATURE, byte_data(cv.kelvin_to_white_temperature(kelvin)))
            self._attr_color_temp_kelvin = kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
            attribute_sent = True

        if (brightness := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            if self._attr_color_mode == ColorMode.RGB:
                self._send(
                    Opcode.COLOR_BRIGHTNESS, byte_data(cv.ha_to_color_brightness(brightness))
                )
            else:
                self._send(
                    Opcode.WHITE_BRIGHTNESS, byte_data(cv.ha_to_white_brightness(brightness))
                )
            self._attr_brightness = brightness
            attribute_sent = True

        # Attribute commands switch the light on by themselves (legacy behaviour);
        # an explicit power-on after them is harmless and covers lights that do not.
        if not attribute_sent or not was_on:
            self._send(Opcode.POWER, power_data(True))
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._send(Opcode.POWER, power_data(False))
        self._attr_is_on = False
        self.async_write_ha_state()
