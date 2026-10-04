"""Picture-adjustment number entities: brightness, contrast, saturation, hue.

The controls Reolink and Tapo expose as sliders. Backed by the VideoColor config
table (0-100, measured on a DHI-NVR5464 and a VTO); the entity reads the poll's
value and writes the channel's general profile on change.
"""

import pytest

from custom_components.dahua import number as number_module
from custom_components.dahua.number import (
    DahuaImageAdjustmentNumber,
    IMAGE_ADJUSTMENTS,
    async_setup_entry,
)

from . import adds_entities


class _Client:
    def __init__(self):
        self.calls = []

    async def async_set_video_color(self, channel, field, value):
        self.calls.append((channel, field, value))


class _Coordinator:
    subentry_id = None

    def __init__(self, *, channel=0, values=None, no_video=False):
        self.client = _Client()
        self._channel = channel
        self._values = values or {}
        self._no_video = no_video
        self.data = {"id": 1}
        self.last_update_success = True
        self.refreshed = 0

    def get_serial_number(self):
        return "SERIAL1"

    def get_channel(self):
        return self._channel

    def get_video_color(self, field):
        return self._values.get(field)

    def is_indoor_monitor_without_video(self):
        return self._no_video

    async def async_refresh(self):
        self.refreshed += 1


def _number(coordinator, key="image_brightness", name="Brightness", field="Brightness"):
    return DahuaImageAdjustmentNumber(coordinator, object(), key, name, field)


# --- the entity -------------------------------------------------------------


def test_native_value_comes_from_the_poll():
    c = _Coordinator(values={"Brightness": 50})
    assert _number(c).native_value == 50


def test_range_is_zero_to_one_hundred():
    n = _number(_Coordinator(values={"Brightness": 50}))
    assert n.native_min_value == 0
    assert n.native_max_value == 100
    assert n.native_step == 1


def test_unique_id_is_the_serial_and_key():
    n = _number(_Coordinator(values={"Hue": 50}), key="image_hue", field="Hue")
    assert n.unique_id == "SERIAL1_image_hue"


def test_it_is_unavailable_until_the_poll_has_a_value():
    c = _Coordinator(values={})  # VideoColor not read / absent
    assert _number(c).available is False
    c2 = _Coordinator(values={"Brightness": 60})
    assert _number(c2).available is True


async def test_setting_a_value_writes_the_field_and_refreshes():
    c = _Coordinator(channel=3, values={"Saturation": 50})
    n = _number(c, key="image_saturation", name="Saturation", field="Saturation")

    await n.async_set_native_value(72.0)

    assert c.client.calls == [(3, "Saturation", 72)]
    assert c.refreshed == 1


# --- the platform -----------------------------------------------------------


async def test_a_camera_gets_the_four_adjustments():
    added = []
    coordinator = _Coordinator()
    entry = type("E", (), {"entry_id": "e1", "runtime_data": {0: coordinator}})()

    await async_setup_entry(None, entry, adds_entities(added))

    keys = sorted(n.unique_id for n in added)
    assert keys == [
        "SERIAL1_image_brightness",
        "SERIAL1_image_contrast",
        "SERIAL1_image_hue",
        "SERIAL1_image_saturation",
    ]
    assert len(added) == len(IMAGE_ADJUSTMENTS)


async def test_an_indoor_monitor_without_a_camera_gets_none():
    added = []
    coordinator = _Coordinator(no_video=True)
    entry = type("E", (), {"entry_id": "e1", "runtime_data": {0: coordinator}})()

    await async_setup_entry(None, entry, adds_entities(added))

    assert added == []
