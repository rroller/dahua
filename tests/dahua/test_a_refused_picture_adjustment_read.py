"""The picture adjustments must not be able to fail a poll, or to be read blind.

#1006, reported against 1.1.0 by two people within an hour of each other. The
number entities added in #978 read `VideoColor` on every poll, gated on nothing
but whether the number platform is switched on, and the read was not wrapped:

    403, message='Forbidden',
    url='http://<camera>/cgi-bin/configManager.cgi?action=getConfig&name=VideoColor'

Both reporters run a camera account in the device's **user** group on purpose, so
it can read events and streams and nothing else. `VideoColor` is one of the
things it cannot read. The refusal escaped the poll's `gather`, which has no
`return_exceptions`, became `UpdateFailed`, and on the first refresh that is
`ConfigEntryNotReady` -- so two working cameras sat in `setup_retry` with every
entity unavailable, over four sliders they were never going to serve. 1.0.1 was
fine because the read did not exist yet.

Two properties, and both are needed. The probe (in test_what_setup_probes.py,
which this read is now registered in) decides whether the table is asked for at
all; these pin what the *poll* does, which is the half the reporters hit:

* a channel the device never reported adjustments for is not asked again
* a refusal from a channel it did report is survivable, and holds the last value

The channel half of it is here too, as a property of `video_color_fields` rather
than of the coordinator. `VideoColor` is host-wide and indexed
`[channel][profile]`, so "the request returned 200" says nothing about whether
this channel is in the answer -- and taking another channel's row as evidence is
the fault `read_profile_mode` and `_smart_motion_row` each have a paragraph
about.

The mutations these are written against, none of which I can execute here (the
suite needs Home Assistant and this module imports it):

* dropping the `self._video_color_fields and` from the poll's condition -- the
  not-asked-again test fails
* replacing `_async_fetch_video_color` with the bare client call -- the
  survives-a-refusal test fails with the UpdateFailed the reporters saw
* returning the whole of the previous poll instead of this table's keys -- the
  carries-only-its-own-keys test fails
* reading `[0][0]` instead of `[{channel}][0]`, or any-field instead of
  per-field -- the helper tests fail
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.coordinator import (
    VIDEO_COLOR_FIELDS,
    video_color_fields,
)

ADDRESS = "10.0.0.77"

VIDEO_COLOR = "async_get_video_color"


def _refused(status=403):
    """What the reporters' account is answered with.

    403, not 400: the login was accepted and this user is not allowed the table.
    `_async_fetch_video_color` catches broadly on purpose, so the status here is
    about matching the report rather than about which branch runs.
    """
    return ClientResponseError(
        request_info=SimpleNamespace(
            real_url="http://%s/cgi-bin/configManager.cgi" % ADDRESS
        ),
        history=(),
        status=status,
    )


class _Client:
    """Answers every read emptily, records what was asked, refuses one thing."""

    use_rpc2 = False
    device_key = "%s:80" % ADDRESS

    def __init__(self, refusing=None, colors=None):
        self.asked = []
        self.refusing = refusing
        self._colors = colors or {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            self.asked.append(name)
            if name == self.refusing:
                raise _refused()
            return self._colors if name == VIDEO_COLOR else {}

        return call


def _coordinator(fields, *, refusing=None, colors=None, data=None):
    """A coordinator past its one-time init, polling with only this read on.

    Built with `object.__new__` like the rest of the poll tests here, and every
    other capability is off so the fan-out is this one read and the assertions
    are about it.
    """
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.hass = SimpleNamespace()
    c.client = _Client(refusing, colors)
    c._address = ADDRESS
    c._channel = 0
    c._channel_number = 1
    c.initialized = True
    c.model = ""
    c._profile_mode = "0"
    c._preset_position = "0"
    c._camera_reboot_generation = 0
    c._supports_profile_mode = False
    c._supports_ptz_position = False
    c._supports_coaxial_control = False
    c._supports_disarming_linkage = False
    c._supports_event_notifications = False
    c._supports_smart_motion_detection = False
    c._supports_lighting_v2 = False
    c._supports_privacy_mode = False
    c._supports_day_night_color = False
    c._supports_lighting = False
    c._supports_floodlightmode = False
    c._supports_cloud_upgrade = False
    c._nvr_active_deterrence = False
    c._alarm_output_slots = 0
    c._ivs_rules = []
    c._serial_number = "SER1"
    c.machine_name = "cam"
    # The capability questions the poll asks about the device itself. Off, so
    # nothing else joins the gather.
    for name in (
        "is_doorbell",
        "is_amcrest_doorbell",
        "is_flood_light",
        "is_indoor_monitor",
        "is_nvr_channel",
        "reads_coaxial_status",
        "supports_alarm_output",
        "supports_infrared_light",
        "supports_security_light",
        "supports_smart_motion_detection_amcrest",
        "uses_rpc2_deterrence",
    ):
        setattr(c, name, lambda *a, **k: False)
    c._wanted_by = lambda *a, **k: True
    c.read_profile_mode = lambda mode_data: "0"
    c._back_off_poll_interval = lambda n: None
    c._restore_poll_interval = lambda: None
    c.config_entry = SimpleNamespace(data={}, options={}, entry_id="e1")
    c.update_interval = timedelta(seconds=30)
    c._video_color_fields = frozenset(fields)
    if data is not None:
        c.data = data
    return c


# --- which rows count as this channel's --------------------------------------


def test_this_channels_row_is_what_counts():
    data = {
        "table.VideoColor[0][0].Brightness": "50",
        "table.VideoColor[0][0].Contrast": "50",
        "table.VideoColor[0][0].Saturation": "50",
        "table.VideoColor[0][0].Hue": "50",
    }

    assert video_color_fields(data, 0) == frozenset(VIDEO_COLOR_FIELDS)


def test_another_channels_row_is_not_this_channels():
    """The neighbouring-but-wrong case. One request answers for every channel of
    a recorder, so a channel with no row of its own must come back empty rather
    than inheriting camera 1's."""
    data = {
        "table.VideoColor[0][0].Brightness": "50",
        "table.VideoColor[0][0].Hue": "50",
    }

    assert video_color_fields(data, 3) == frozenset()


def test_the_general_profile_is_the_one_read():
    """The entities read and write profile 0, so a device that reports only its
    night profile has nothing this control can drive."""
    data = {"table.VideoColor[0][1].Brightness": "50"}

    assert video_color_fields(data, 0) == frozenset()


def test_a_device_reporting_some_fields_reports_only_those():
    data = {
        "table.VideoColor[2][0].Brightness": "40",
        "table.VideoColor[2][0].Hue": "0",
    }

    assert video_color_fields(data, 2) == frozenset({"Brightness", "Hue"})


def test_a_zero_is_a_value_like_any_other():
    """`0` is a legitimate setting on a 0-100 scale, so the presence check cannot
    be truthiness: hue sits at 0 on both devices this was measured on."""
    assert video_color_fields({"table.VideoColor[0][0].Hue": "0"}, 0) == frozenset(
        {"Hue"}
    )


@pytest.mark.parametrize("answer", [{}, None, "", "Error", []])
def test_an_answer_that_names_nothing_is_no_capability(answer):
    """An empty 200 is what some firmware sends for a table it does not have, and
    `async_get_config`'s swallowed error returns `{}` as well. Neither is a
    device saying it has picture adjustments."""
    assert video_color_fields(answer, 0) == frozenset()


# --- what the poll does ------------------------------------------------------


async def test_a_channel_with_no_adjustments_is_not_asked():
    """The fix for the reported fault. The read used to be gated on the platform
    alone, so a device that cannot serve the table was asked on every poll, for
    ever, and refused on every poll."""
    coordinator = _coordinator(fields=[])

    await coordinator._async_update_data()

    assert VIDEO_COLOR not in coordinator.client.asked


async def test_a_channel_with_adjustments_is_asked():
    """The guard against the test above passing for the wrong reason: with the
    read never made at all, every assertion here would hold."""
    coordinator = _coordinator(fields=VIDEO_COLOR_FIELDS)

    await coordinator._async_update_data()

    assert VIDEO_COLOR in coordinator.client.asked


async def test_the_poll_returns_what_it_read():
    coordinator = _coordinator(
        fields=VIDEO_COLOR_FIELDS,
        colors={"table.VideoColor[0][0].Brightness": "64"},
    )

    data = await coordinator._async_update_data()

    assert data["table.VideoColor[0][0].Brightness"] == "64"


async def test_a_refused_read_does_not_fail_the_poll():
    """#1006 itself. This read sits in the fan-out that gathers every per-poll
    request, and the gather has no `return_exceptions`, so an exception here took
    the whole refresh with it -- `setup_retry` on the first one."""
    coordinator = _coordinator(fields=VIDEO_COLOR_FIELDS, refusing=VIDEO_COLOR)

    data = await coordinator._async_update_data()

    assert isinstance(data, dict), "the poll did not complete"


async def test_a_refused_read_keeps_the_last_known_values():
    """The sliders hold their position rather than dropping to unknown, the same
    way the preset select and the coaxial status hold theirs."""
    coordinator = _coordinator(
        fields=VIDEO_COLOR_FIELDS,
        refusing=VIDEO_COLOR,
        data={"table.VideoColor[0][0].Brightness": "64"},
    )

    data = await coordinator._async_update_data()

    assert data.get("table.VideoColor[0][0].Brightness") == "64"


async def test_a_refused_read_carries_only_its_own_table():
    """Carrying the whole of the previous poll would let a stale value overwrite
    a fresh one from another coroutine in the same gather, which is the rule
    `_previous_coaxial_status` states and the reason it lists its keys."""
    coordinator = _coordinator(
        fields=VIDEO_COLOR_FIELDS,
        refusing=VIDEO_COLOR,
        data={
            "table.VideoColor[0][0].Brightness": "64",
            "status.PresetID": "7",
        },
    )

    data = await coordinator._async_update_data()

    assert data.get("table.VideoColor[0][0].Brightness") == "64"
    assert "status.PresetID" not in data


async def test_a_refused_read_with_nothing_remembered_says_nothing():
    """With no previous answer there is nothing to carry, and inventing one would
    have the sliders claim a position the camera never reported."""
    coordinator = _coordinator(fields=VIDEO_COLOR_FIELDS, refusing=VIDEO_COLOR, data={})

    data = await coordinator._async_update_data()

    assert not [key for key in data if key.startswith("table.VideoColor[")]
