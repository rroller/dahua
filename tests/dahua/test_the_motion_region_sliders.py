"""Motion region 1's sensitivity and threshold, as sliders.

Asked for by @clore-hub on #1115, whose VTO4202F-P-S2 has no motion settings
in its web UI at all, so Home Assistant would be the only interface it has.

Chasing it found that there are **two different controls**, and only one of
them has a range anybody can state. Measured here on a DHI-NVR5464-16P-EI and
a VTO2000A:

    recorder   MotionDetect[n].MotionDetectWindow[0..3].Sensitive / .Threshold
               four regions per channel. Five of twelve channels tuned by hand:
               Sensitive 10, 20, 30, 50, 60 and Threshold 5, 9, 10, 11, 50.
               Level = 3 on every channel, untouched.

    doorbell   no MotionDetectWindow at all. Level = 3, and nothing else.

So the recorder's UI slider is the region's `Sensitive`, and `Level` is what a
device without regions has. These entities are the first; `Level` is not
exposed, because its range cannot be established: @clore-hub's device accepts
and stores 0, 7 and 100 without complaint, and `getConfigCaps`, `getCaps`,
`getDefault` and `getMemberNames` all decline to describe it on two device
classes.

**The 0 to 100 here is measured rather than assumed.** The recorder's own web
UI offers that range for both fields, and setting Channel 1's sensitivity to
100 there put 100 into `MotionDetect[0].MotionDetectWindow[0].Sensitive`, read
back over CGI. The displayed scale and the stored field are the same number,
and UI channel 1 is table index 0.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.number import (
    MOTION_REGION_FIELDS,
    DahuaMotionRegionNumber,
    async_setup_entry,
)

from . import adds_entities

SENSITIVE = "table.MotionDetect[{0}].MotionDetectWindow[0].Sensitive"
THRESHOLD = "table.MotionDetect[{0}].MotionDetectWindow[0].Threshold"


def _coordinator(channel=0, data=None):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c.data = {} if data is None else data
    return c


def _recorder_channel(channel=0, sensitive="60", threshold="5"):
    return _coordinator(
        channel,
        {
            SENSITIVE.format(channel): sensitive,
            THRESHOLD.format(channel): threshold,
        },
    )


# --- which devices get them ---------------------------------------------------


@pytest.mark.parametrize("field", ["Sensitive", "Threshold"])
def test_a_recorder_channel_reports_both(field):
    assert _recorder_channel().supports_motion_region(field) is True


@pytest.mark.parametrize("field", ["Sensitive", "Threshold"])
def test_a_doorbell_with_no_regions_reports_neither(field):
    """The VTO2000A measured for this: `MotionDetect[0].Level` and nothing
    else. A slider for a field the device does not carry reads unknown for
    ever, which is the rule #641 set for the profile sensor."""
    doorbell = _coordinator(0, {"table.MotionDetect[0].Level": "3"})

    assert doorbell.supports_motion_region(field) is False


def test_a_device_reporting_one_field_gets_one():
    """Per field rather than per table, like the picture sliders, because a
    device reporting one of the two should get one rather than a second stuck
    on unknown."""
    half = _coordinator(0, {SENSITIVE.format(0): "60"})

    assert half.supports_motion_region("Sensitive") is True
    assert half.supports_motion_region("Threshold") is False


def test_a_coordinator_with_no_data_yet_does_not_raise():
    """`data` does not exist until the first refresh, and reading it before
    then took 35 tests down the last time (#1094's abandoned build)."""
    bare = object.__new__(DahuaDataUpdateCoordinator)
    bare._channel = 0

    assert bare.supports_motion_region("Sensitive") is False
    assert bare.get_motion_region_value("Sensitive") is None


def test_each_channel_reads_its_own_row():
    """A recorder is one coordinator per channel against one shared table, so
    the wrong index here would show channel 0's sensitivity on all twelve."""
    channel_seven = _coordinator(
        7, {SENSITIVE.format(7): "10", SENSITIVE.format(0): "60"}
    )

    assert channel_seven.get_motion_region_value("Sensitive") == 10


# --- the values ---------------------------------------------------------------


def test_the_value_is_an_int_not_the_wire_string():
    """CGI answers `key=value` text, so every value arrives as a string and a
    slider handed "60" is not a number."""
    value = _recorder_channel(sensitive="60").get_motion_region_value("Sensitive")

    assert value == 60 and isinstance(value, int)


@pytest.mark.parametrize("junk", ["", "none", None, "60.5"])
def test_a_value_that_is_not_a_number_reads_as_nothing(junk):
    """Rather than raising out of a property, which Home Assistant calls on
    every state write."""
    c = _coordinator(0, {SENSITIVE.format(0): junk})

    assert c.get_motion_region_value("Sensitive") is None


def test_the_real_measured_values_all_read_back():
    """The five hand-tuned channels on the recorder this was measured against,
    so the parser is checked against what a real device holds rather than
    against a round number."""
    for sensitive, threshold in ((20, 50), (30, 9), (50, 10), (10, 11), (60, 5)):
        c = _recorder_channel(sensitive=str(sensitive), threshold=str(threshold))
        assert c.get_motion_region_value("Sensitive") == sensitive
        assert c.get_motion_region_value("Threshold") == threshold


# --- the entity ---------------------------------------------------------------


def _entity(coordinator, key="motion_region1_sensitivity", field="Sensitive"):
    entity = object.__new__(DahuaMotionRegionNumber)
    entity._coordinator = coordinator
    entity._key = key
    entity._field = field
    entity._attr_unique_id = "SER_" + key
    return entity


def test_the_slider_reports_the_measured_range():
    """0 to 100 on both, which is the recorder's own UI range and not a guess.
    A slider whose maximum is wrong writes a value the device stores without
    complaint and the detection then does something nobody intended."""
    entity = _entity(_recorder_channel())

    assert entity.native_min_value == 0
    assert entity.native_max_value == 100
    assert entity.native_step == 1


def test_the_slider_shows_the_device_value():
    assert _entity(_recorder_channel(sensitive="30")).native_value == 30


def test_a_write_sends_one_field_only():
    """One field per write, and not only for tidiness: this family refuses a
    whole setConfig as soon as any named key is one it does not carry, which
    is how #1115's doorbell could not turn motion detection off at all."""
    sent = []

    class _Client:
        async def async_set_motion_region(self, channel, region, field, value):
            sent.append((channel, region, field, value))

    coordinator = _recorder_channel(channel=3)
    coordinator.client = _Client()
    coordinator.get_channel = lambda: 3
    coordinator.async_refresh = _noop
    entity = _entity(coordinator)

    import asyncio

    asyncio.run(entity.async_set_native_value(45.0))

    assert sent == [(3, 0, "Sensitive", 45)]


async def _noop():
    return None


def test_the_url_names_the_region_and_the_one_field():
    """Derived from the client rather than asserted as prose. CONTRIBUTING
    insists a write test names the exact key, because a wrong one answers OK
    and changes something else for ever."""
    sent = []
    client = object.__new__(DahuaClient)

    async def get(url, verify_ok=False):
        sent.append(url)
        return "OK"

    client.get = get

    import asyncio

    asyncio.run(client.async_set_motion_region(3, 0, "Threshold", 12))

    assert sent == [
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&MotionDetect[3].MotionDetectWindow[0].Threshold=12"
    ]


# --- the platform actually gates on it ----------------------------------------
#
# Everything above drives the coordinator helper directly. The gate that
# decides whether an entity is created at all lives in `number.py`, and a test
# of the helper alone passes just as well with that gate deleted.


class _PlatformCoordinator:
    """The shape `number.py` reads while building, and nothing more."""

    subentry_id = None

    def __init__(self, regions=True):
        self._regions = regions
        self.data = {"id": 1}
        self.last_update_success = True

    def get_serial_number(self):
        return "SERIAL1"

    def get_channel(self):
        return 0

    def is_indoor_monitor_without_video(self):
        return False

    def supports_video_color(self, field):
        return False

    def supports_vth_phone_setting(self, field):
        return False

    def supports_motion_region(self, field):
        return self._regions


async def _added_for(coordinator):
    added = []
    entry = type(
        "E",
        (),
        {
            "entry_id": "e1",
            "runtime_data": {0: coordinator},
            "async_on_unload": staticmethod(lambda unsub: None),
        },
    )()
    await async_setup_entry(SimpleNamespace(data={}), entry, adds_entities(added))
    return added


async def test_a_device_with_regions_gets_both_sliders():
    added = await _added_for(_PlatformCoordinator(regions=True))

    assert sorted(entity._field for entity in added) == ["Sensitive", "Threshold"]


async def test_a_device_without_regions_gets_none():
    """The gate. Without it a doorbell with no `MotionDetectWindow` gets two
    sliders that can only ever read unknown."""
    assert await _added_for(_PlatformCoordinator(regions=False)) == []


# --- and the two halves agree -------------------------------------------------


def test_the_table_names_the_fields_the_coordinator_reads():
    """Derived rather than listed twice. The platform builds from this table
    and the coordinator keys on the same strings, so a rename in one place has
    to fail here rather than produce a slider that reads nothing."""
    c = _recorder_channel()

    for _key, field in MOTION_REGION_FIELDS:
        assert c.supports_motion_region(field), field
