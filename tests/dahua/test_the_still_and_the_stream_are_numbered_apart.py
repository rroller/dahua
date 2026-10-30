"""A device can number its stills and its streams differently.

#1151, on a Lorex LHV2008 (HCVR, firmware 2.616.GM00.0). Its owner measured
both paths from a browser:

    snapshot.cgi?channel=1   -> the picture from channel 2 (index 1)
    rtsp .../channel=1       -> channel 1 (index 0)

So `snapshot.cgi` counts from 0 on that firmware and RTSP counts from 1. The
integration asked one question, of `snapshot.cgi`, and sent the single answer
down both paths, which left every setting half right:

    auto-detect ticked     still right, stream one camera off
    auto-detect unticked   stream right, still one camera off

Their words for the second: the thumbnail shows the wrong camera, clicking it
shows that wrong camera for a split second before the stream replaces it with
the right one, and "download snapshot" saves the wrong one.

The option's own label is "disable if HTTP snapshot at channel=0 works but RTSP
only streams on channel=1", which is that device exactly. So the option governs
the stream, the path it names, and the measured answer governs the still, the
path it was measured on.

Measured against the other shape too, on a DHI-NVR5464-16P-EI, so the change is
known to leave recorders like it alone: `snapshot.cgi?channel=0` answers 400
there, channels 1, 2, 3, 12 and 15 answer 200, and 16 answers 400. A device that
refuses channel 0 never reaches the branch any of this touches.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import custom_components.dahua as dahua
from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua.const import CONF_AUTO_DETECT_CHANNEL
from custom_components.dahua.host import device_numbers_channels_from_zero

from tests.dahua.test_probe_timeouts import _Client, _coordinator

IMAGE = b"\xff\xd8\xff\xe0 a jpeg \xff\xd9"


def _clean():
    dahua._HOST_CHANNEL_BASE.clear()
    dahua._HOST_CHANNEL_BASE_LOCKS.clear()


# --- the number the still is asked for --------------------------------------


def _channel(index, device="d1", doorbell=False):
    """A coordinator for one channel of a device, numbering nothing itself."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = index
    c._channel_number = index + 1
    c.client = SimpleNamespace(device_key=device)
    c.is_doorbell = lambda: doorbell
    return c


def test_a_device_that_counts_stills_from_zero_is_asked_for_the_index():
    _clean()
    dahua._HOST_CHANNEL_BASE["d1"] = True

    assert _channel(0).get_snapshot_channel() == 0
    assert _channel(1).get_snapshot_channel() == 1
    assert _channel(7).get_snapshot_channel() == 7


def test_a_device_that_said_no_is_asked_for_the_index_plus_one():
    """The NVR5464 shape: snapshot.cgi?channel=0 answers 400."""
    _clean()
    dahua._HOST_CHANNEL_BASE["d1"] = False

    assert _channel(0).get_snapshot_channel() == 1
    assert _channel(7).get_snapshot_channel() == 8


def test_a_device_that_never_answered_is_left_at_index_plus_one():
    """Deciding nothing is #724's rule, and it holds for the still as well."""
    _clean()

    assert _channel(0).get_snapshot_channel() == 1
    assert _channel(3).get_snapshot_channel() == 4


def test_a_doorbell_keeps_its_channel_number():
    """Channel 0 does not exist on a VTO."""
    _clean()
    dahua._HOST_CHANNEL_BASE["d1"] = True

    assert _channel(0, doorbell=True).get_snapshot_channel() == 1


def test_a_late_answer_reaches_a_channel_that_already_numbered_itself():
    """The out of step channel in the #1151 dump.

    Six channels on one recorder, and channel 0 alone kept `index + 1` while
    channels 2, 3, 6 and 7 took the index. A probe that goes unanswered leaves
    that channel alone (#724) and the next channel asks again, so the host can
    settle the question after a channel has already numbered itself. Reading it
    at call time is what lets that channel catch up; a value copied at setup
    keeps the gap for ever.
    """
    _clean()
    channel = _channel(0)

    assert channel.get_snapshot_channel() == 1

    dahua._HOST_CHANNEL_BASE["d1"] = True

    assert channel.get_snapshot_channel() == 0


def test_two_devices_on_one_address_are_numbered_separately():
    _clean()
    dahua._HOST_CHANNEL_BASE["10.0.0.5:80"] = True
    dahua._HOST_CHANNEL_BASE["10.0.0.5:8000"] = False

    assert _channel(2, device="10.0.0.5:80").get_snapshot_channel() == 2
    assert _channel(2, device="10.0.0.5:8000").get_snapshot_channel() == 3


# --- reading the answer costs no request ------------------------------------


def test_reading_the_answer_asks_the_device_nothing():
    """It is read on the way to every still, so it cannot be a request."""
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


# --- what setup does with the option either way -----------------------------


def _setup_coordinator(hass, auto_detect):
    client = _Client()
    client.device_key = "d1"
    c = _coordinator(client)
    c.hass = hass
    c._channel = 1
    c._channel_number = 2
    c._channel_config = {CONF_AUTO_DETECT_CHANNEL: auto_detect}
    c.config_entry = SimpleNamespace(data={}, options={}, entry_id="e1")
    return c


async def test_the_question_is_asked_even_with_the_option_off(hass):
    """The still needs the answer whichever way the option is set.

    Unticking the box used to skip the probe altogether, so nothing knew how
    the device numbers its stills and they stayed on the stream's number.
    """
    _clean()
    c = _setup_coordinator(hass, auto_detect=False)

    await c._async_update_data()

    assert "async_probe_snapshot" in c.client.called, "the device was never asked"
    assert c.get_channel_number() == 2, "the option is about the stream"
    assert c.get_snapshot_channel() == 1, "the still ignored the answer"


async def test_the_option_on_moves_both(hass):
    """A device that numbers both paths from zero is unchanged."""
    _clean()
    c = _setup_coordinator(hass, auto_detect=True)

    await c._async_update_data()

    assert c.get_channel_number() == 1
    assert c.get_snapshot_channel() == 1
