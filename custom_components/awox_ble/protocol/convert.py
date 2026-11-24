"""Value conversions between Home Assistant and device scales (pure).

Same maths as home-assistant-awox ``light.py`` (c) 2020 Frans Saris, MIT.
"""

from __future__ import annotations

from .constants import (
    COLOR_BRIGHTNESS_MAX,
    COLOR_BRIGHTNESS_MIN,
    KELVIN_MAX,
    KELVIN_MIN,
    WHITE_BRIGHTNESS_MAX,
    WHITE_BRIGHTNESS_MIN,
    WHITE_TEMPERATURE_MAX,
    WHITE_TEMPERATURE_MIN,
)


def rescale(value: float, min_from: float, max_from: float, min_to: int, max_to: int) -> int:
    normalized = (value - min_from) / (max_from - min_from)
    new_value = min(round(normalized * (max_to - min_to) + min_to), max_to)
    return max(new_value, min_to)


def ha_to_white_brightness(brightness: int) -> int:
    return rescale(brightness, 0, 255, WHITE_BRIGHTNESS_MIN, WHITE_BRIGHTNESS_MAX)


def white_brightness_to_ha(value: int) -> int:
    return rescale(value, WHITE_BRIGHTNESS_MIN, WHITE_BRIGHTNESS_MAX, 0, 255)


def ha_to_color_brightness(brightness: int) -> int:
    return rescale(brightness, 0, 255, COLOR_BRIGHTNESS_MIN, COLOR_BRIGHTNESS_MAX)


def color_brightness_to_ha(value: int) -> int:
    return rescale(value, COLOR_BRIGHTNESS_MIN, COLOR_BRIGHTNESS_MAX, 0, 255)


def kelvin_to_white_temperature(kelvin: int) -> int:
    """Device: 0 = coolest (6500 K), 0x7f = warmest (2700 K)."""
    mapped = rescale(kelvin, KELVIN_MIN, KELVIN_MAX, WHITE_TEMPERATURE_MIN, WHITE_TEMPERATURE_MAX)
    return WHITE_TEMPERATURE_MAX - mapped


def white_temperature_to_kelvin(value: int) -> int:
    inverted = WHITE_TEMPERATURE_MAX - value
    return rescale(inverted, WHITE_TEMPERATURE_MIN, WHITE_TEMPERATURE_MAX, KELVIN_MIN, KELVIN_MAX)
