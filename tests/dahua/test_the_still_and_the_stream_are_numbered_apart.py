"""A device can number its stills and its streams differently.

#1151, on a Lorex LHV2008 (HCVR, firmware 2.616.GM00.0). Its owner opened the
still path in a browser:

    snapshot.cgi?channel=1   -> the picture from channel 2 (index 1)

and reported that with the auto-detect option off, which numbers the stream
`index + 1`, every one of their six live views shows the right camera. So
`snapshot.cgi` counts from 0 on that firmware and RTSP counts from 1. The
integration sent one number down both paths, which left every setting half
right:

    auto-detect ticked     still right, stream one camera off
    auto-detect unticked   stream right, still one camera off

Their words for the second: the thumbnail shows the wrong camera, clicking it
shows that wrong camera for a split second before the stream replaces it with
the right one, and "download snapshot" saves the wrong one.

**Why the split is opt-in rather than taken from the probe.** The first version
of this change gave the still whatever the channel-0 probe concluded, on the
reasoning that the probe asks `snapshot.cgi` so its answer is about stills.
rroller's review killed that, and the replay is
`test_an_aliasing_device_stays_correctable` below: a snapshot answering on
channel 0 has two explanations, and the probe cannot tell them apart.

    genuinely zero-based   channel 0 is the first camera, 1 is the second
    channel 0 aliases 1    channel 0 is the first camera, 1 is ALSO the first

On the second kind, unticking auto-detect is the owner's only way to put the
stream back on `index + 1`, and a still that follows the probe anyway hands them
the first camera's picture under every other camera's name with nothing left to
untick. So the default is what the integration has always done -- the still
follows the stream -- and the Lorex split is selected by someone who has read
`snapshot.cgi?channel=<n>` in a browser and seen which camera came back. That is
a measurement, which is what the probe is not.

Measured against the other shape too, on a DHI-NVR5464-16P-EI, so the change is
known to leave recorders like it alone: `snapshot.cgi?channel=0` answers 400
there, channels 1, 2, 3, 12 and 15 answer 200, and 16 answers 400.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import custom_components.dahua as dahua
from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua.const import (
    CONF_AUTO_DETECT_CHANNEL,
    CONF_SNAPSHOT_CHANNEL_FROM_ZERO,
)
from custom_components.dahua.host import device_numbers_channels_from_zero

from tests.dahua.test_probe_timeouts import _Client, _coordinator

IMAGE = b"\xff\xd8\xff\xe0 a jpeg \xff\xd9"


def _clean():
    dahua._HOST_CHANNEL_BASE.clear()
    dahua._HOST_CHANNEL_BASE_LOCKS.clear()


# --- the number the still is asked for --------------------------------------


def _channel(index, device="d1", doorbell=False, from_zero=None, stream=None):
    """A coordinator for one channel of a device, numbering nothing itself.

    `stream` is the number the stream settled on, which is what the still
    follows unless `from_zero` says otherwise.
    """
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = index
    c._channel_number = index + 1 if stream is None else stream
    c.client = SimpleNamespace(device_key=device)
    c.is_doorbell = lambda: doorbell
    c._channel_config = (
        {} if from_zero is None else {CONF_SNAPSHOT_CHANNEL_FROM_ZERO: from_zero}
    )
    c.config_entry = SimpleNamespace(options={})
    return c


def test_the_still_follows_the_stream_by_default():
    """The default is the behaviour the integration has always had."""
    _clean()

    assert _channel(0).get_snapshot_channel() == 1
    assert _channel(3).get_snapshot_channel() == 4
    assert _channel(7).get_snapshot_channel() == 8


def test_the_probe_does_not_move_the_still_on_its_own():
    """The whole point of rroller's P1: a probe answer is not a measurement.

    `_HOST_CHANNEL_BASE` says this device answered a snapshot on channel 0, and
    the still still follows the stream, because that answer cannot tell
    zero-based numbering from channel 0 aliasing channel 1.
    """
    _clean()
    dahua._HOST_CHANNEL_BASE["d1"] = True

    assert _channel(1, stream=2).get_snapshot_channel() == 2


def test_the_option_splits_the_still_from_the_stream():
    """The Lorex LHV2008: stream on index + 1, still on the index."""
    _clean()

    assert _channel(0, from_zero=True, stream=1).get_snapshot_channel() == 0
    assert _channel(1, from_zero=True, stream=2).get_snapshot_channel() == 1
    assert _channel(7, from_zero=True, stream=8).get_snapshot_channel() == 7


def test_a_genuinely_zero_based_device_numbers_both_from_zero():
    """Auto-detect moved the stream to the index, and the still comes along.

    Taken from `_channel_number` rather than derived again, so the two paths
    cannot come to disagree on a device the probe got right.
    """
    _clean()
    dahua._HOST_CHANNEL_BASE["d1"] = True

    assert _channel(5, stream=5).get_snapshot_channel() == 5


def test_a_doorbell_keeps_its_channel_number():
    """Channel 0 does not exist on a VTO, whichever way the option is set."""
    _clean()

    assert _channel(0, doorbell=True).get_snapshot_channel() == 1
    assert _channel(0, doorbell=True, from_zero=True).get_snapshot_channel() == 1


def test_the_option_is_read_per_channel():
    """One channel of a recorder can be numbered apart from its neighbours.

    It is in CHANNEL_OPTION_KEYS, so a subentry's answer beats the entry's --
    the distinction #895 exists for.
    """
    _clean()
    c = _channel(1, stream=2)
    c.config_entry = SimpleNamespace(options={CONF_SNAPSHOT_CHANNEL_FROM_ZERO: True})

    assert c.get_snapshot_channel() == 1, "the entry's option was not read"

    c._channel_config = {CONF_SNAPSHOT_CHANNEL_FROM_ZERO: False}

    assert c.get_snapshot_channel() == 2, "the channel's own answer must win"


# --- reading the answer costs no request ------------------------------------


def test_reading_the_probes_answer_asks_the_device_nothing():
    _clean()

    assert device_numbers_channels_from_zero("d1") is None

    dahua._HOST_CHANNEL_BASE["d1"] = True

    assert device_numbers_channels_from_zero("d1") is True


# --- the camera fetches the still on that number ----------------------------


def _camera(snapshot, stream_number=2, snapshot_number=1, media_channel=None):
    cam = object.__new__(DahuaCamera)
    cam._channel_number = stream_number
    cam._media_channel = media_channel
    cam._name = "Heat Pump"
    cam._coordinator = SimpleNamespace(
        client=SimpleNamespace(async_get_snapshot=snapshot),
        get_snapshot_channel=lambda: snapshot_number,
    )
    return cam


async def test_the_still_is_asked_for_on_the_stills_number():
    """The #1151 bug. This used to ask for the stream's number."""
    snapshot = AsyncMock(return_value=IMAGE)
    cam = _camera(snapshot, stream_number=2, snapshot_number=1)

    assert await cam.async_camera_image() == IMAGE
    assert snapshot.await_args.args == (1,), "the still went to the stream's channel"


async def test_a_dual_sensor_camera_keeps_the_channel_it_was_built_with():
    """An SDT4E425 names the media channel for both paths, and that stands."""
    snapshot = AsyncMock(return_value=IMAGE)
    cam = _camera(snapshot, stream_number=2, snapshot_number=1, media_channel=2)

    await cam.async_camera_image()

    assert snapshot.await_args.args == (2,)


async def test_a_coordinator_that_cannot_say_still_produces_a_still():
    """A stand-in that predates the question must not stop the picture."""
    snapshot = AsyncMock(return_value=IMAGE)
    cam = object.__new__(DahuaCamera)
    cam._channel_number = 3
    cam._name = "Heat Pump"
    cam._coordinator = SimpleNamespace(
        client=SimpleNamespace(async_get_snapshot=snapshot)
    )

    assert await cam.async_camera_image() == IMAGE
    assert snapshot.await_args.args == (3,)


# --- the three device shapes, through setup ---------------------------------


def _setup_coordinator(hass, auto_detect=True, from_zero=False, channel=1):
    client = _Client()
    client.device_key = "d1"
    c = _coordinator(client)
    c.hass = hass
    c._channel = channel
    c._channel_number = channel + 1
    c._channel_config = {
        CONF_AUTO_DETECT_CHANNEL: auto_detect,
        CONF_SNAPSHOT_CHANNEL_FROM_ZERO: from_zero,
    }
    c.config_entry = SimpleNamespace(data={}, options={}, entry_id="e1")
    return c


async def test_an_aliasing_device_stays_correctable(hass):
    """rroller's P1 replay, which the first version of this change failed.

    The device answers a snapshot on channel 0 because channel 0 *aliases*
    channel 1, not because it numbers from zero. Its owner has unticked
    auto-detect, which is the one control they have, and that must put BOTH
    paths back on `index + 1`. The first version left the still on the probe's
    answer, so this channel's thumbnail showed the first camera with nothing
    left to untick.

    The cached answer is seeded deliberately rather than left absent, because
    that is the sequence an owner actually goes through: they add the device
    with auto-detect on, the probe answers True and is written to
    `_HOST_CHANNEL_BASE`, they see the wrong live view and untick the box. That
    dict is a module global and nothing clears it, not unload and not the
    reload the options flow triggers, so the stale True is still there on the
    next setup. A test that left it unset would pass against the very code
    rroller rejected.
    """
    _clean()
    dahua._HOST_CHANNEL_BASE["d1"] = True
    c = _setup_coordinator(hass, auto_detect=False, channel=1)

    await c._async_update_data()

    assert c.get_channel_number() == 2, "the stream must stay on index + 1"
    assert c.get_snapshot_channel() == 2, "the still followed the probe, not the owner"


async def test_a_zero_based_device_is_unchanged(hass):
    """Auto-detect on and the device answers: both paths take the index."""
    _clean()
    c = _setup_coordinator(hass, auto_detect=True, channel=1)

    await c._async_update_data()

    assert c.get_channel_number() == 1
    assert c.get_snapshot_channel() == 1


async def test_the_lorex_split_can_be_selected(hass):
    """#1151's device: auto-detect off for the stream, the new option for the still."""
    _clean()
    c = _setup_coordinator(hass, auto_detect=False, from_zero=True, channel=1)

    await c._async_update_data()

    assert c.get_channel_number() == 2, "RTSP counts from 1 on this firmware"
    assert c.get_snapshot_channel() == 1, "snapshot.cgi counts from 0 on this firmware"


async def test_the_probe_is_not_asked_when_nothing_reads_it(hass):
    """The option is the answer's only reader, so an opt-out costs no request.

    The first version asked unconditionally, on the reasoning that the still
    needed the answer. It does not any more, and a snapshot probe is the most
    expensive call in setup (about 1.9s on the recorder measured for #1043).
    """
    _clean()
    c = _setup_coordinator(hass, auto_detect=False)

    await c._async_update_data()

    assert "async_probe_snapshot" not in c.client.called
