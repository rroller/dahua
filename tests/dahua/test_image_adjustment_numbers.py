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

    def __init__(self, *, channel=0, values=None, no_video=False, fields=None):
        self.client = _Client()
        self._channel = channel
        self._values = values or {}
        self._no_video = no_video
        # Which adjustments the device reported for this channel at setup. The
        # real coordinator probes VideoColor for them and the platform creates
        # one entity per field, so the default here is an ordinary camera that
        # serves all four. Modelled rather than assumed, because a fake that
        # answers yes to everything is how the #1006 gate would go untested.
        self._fields = (
            frozenset(field for _key, _name, field in IMAGE_ADJUSTMENTS)
            if fields is None
            else frozenset(fields)
        )
        self.data = {"id": 1}
        self.last_update_success = True
        self.refreshed = 0

    def get_serial_number(self):
        return "SERIAL1"

    def get_address(self):
        return "10.0.0.5"

    def get_channel(self):
        return self._channel

    def get_video_color(self, field):
        return self._values.get(field)

    def supports_video_color(self, field):
        return field in self._fields

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


async def test_a_device_that_reports_no_adjustments_gets_no_sliders():
    """#1006. These were created for every device, and VideoColor is not a table
    every account can read: a camera account in the device's `user` group answers
    403 to it, deliberately, and two reporters had every entity of a working
    camera go unavailable because of the four sliders it was never going to feed.

    The read is gated now, and so is the entity. An entity that can only ever
    read unknown is worse than no entity, which is the rule the profile sensor
    established in #641.
    """
    added = []
    coordinator = _Coordinator(fields=[])
    entry = type("E", (), {"entry_id": "e1", "runtime_data": {0: coordinator}})()

    await async_setup_entry(None, entry, adds_entities(added))

    assert added == []


async def test_only_the_adjustments_the_device_reported_are_created():
    """Per field rather than one yes for the table. A device serving three of the
    four would otherwise get a fourth slider stuck at unknown for ever, which is
    the same fault as the whole set, one control over.
    """
    added = []
    coordinator = _Coordinator(fields=["Brightness", "Hue"])
    entry = type("E", (), {"entry_id": "e1", "runtime_data": {0: coordinator}})()

    await async_setup_entry(None, entry, adds_entities(added))

    assert sorted(n.unique_id for n in added) == [
        "SERIAL1_image_brightness",
        "SERIAL1_image_hue",
    ]
