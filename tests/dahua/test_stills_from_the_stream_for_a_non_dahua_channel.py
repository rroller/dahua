"""A channel the recorder reaches over another protocol serves no snapshot.

`is_onvif_channel` already records that for ONVIF -- snapshot.cgi answers 400 for
the ONVIF channel of a DHI-NVR5464-16P-EI while every Dahua-protocol channel on
the same recorder returns an image. A channel added as generic RTSP behaves the
same way. Measured on a DHI-NVR5232-16P-EI, where channel 2 is a third-party
camera with `ProtocolType=General`:

    index 1  ch=2  General  snapshot.cgi -> 500 after 10-15s, no image
    index 0  ch=1  Private  snapshot.cgi -> 200, 945,557 bytes
    index 2  ch=3  Private  snapshot.cgi -> 200, 734,898 bytes

Being slow is not the whole cost. MAX_CONCURRENT_REQUESTS_PER_HOST is 2, so one
channel that cannot produce an image holds half the host's budget for those
seconds: all 14 stills of that recorder requested together returned 10/14, and
14/14 once those two entities stopped asking.

What the channel does serve is RTSP -- `hevc 2592x1944` on main and
`hevc 800x600` on sub, straight off the recorder -- so the still is rendered from
the stream and snapshot.cgi is never called for it. For those cameras that is the
difference between no image and an image.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import camera as camera_module
from custom_components.dahua.camera import (
    DahuaCamera,
    _async_stream_still_coordinators,
)

UUID = "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{0}.ProtocolType"
BRACKET = "table.RemoteDevice[{0}].ProtocolType"

# The measured recorder: seven populated channels, one of them generic RTSP.
MEASURED = {
    UUID.format(0): "Private",
    UUID.format(1): "General",
    UUID.format(2): "Private",
    UUID.format(3): "Private",
}


class _Client:
    """A client whose remote-device table is whatever the test hands it."""

    def __init__(self, table=None, error=None):
        self._table = table
        self._error = error
        self.calls = 0

    async def async_get_remote_devices(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._table

    @staticmethod
    def to_stream_name(subtype):
        return "Main" if subtype == 0 else "Sub"

    @staticmethod
    def get_rtsp_stream_url(channel, subtype):
        return "rtsp://host/cam?channel=%s&subtype=%s" % (channel, subtype)


class _Coordinator:
    def __init__(self, channel=0, client=None, address="10.0.0.1"):
        self.client = client if client is not None else _Client(MEASURED)
        self._channel = channel
        self._address = address

    def get_address(self):
        return self._address

    def get_channel(self):
        return self._channel

    def get_channel_number(self):
        return self._channel + 1

    def get_serial_number(self):
        return "SERIAL%d" % self._channel

    def channel_option(self, name, default=None):
        return default


# --- which channels are chosen ------------------------------------------------


async def test_only_the_generic_rtsp_channel_is_chosen():
    """The one channel the recorder cannot photograph, and only that one."""
    client = _Client(MEASURED)
    coordinators = [_Coordinator(channel=c, client=client) for c in range(4)]

    chosen = await _async_stream_still_coordinators(coordinators)

    assert [c.get_channel() for c in chosen] == [1]


async def test_an_onvif_channel_is_chosen_too():
    """ONVIF is the case upstream already measured; this is the general rule."""
    client = _Client({UUID.format(0): "Private", UUID.format(1): "Onvif"})
    coordinators = [_Coordinator(channel=c, client=client) for c in range(2)]

    chosen = await _async_stream_still_coordinators(coordinators)

    assert [c.get_channel() for c in chosen] == [1]


async def test_the_bracket_spelling_is_accepted_too():
    """Both key shapes, exactly as parse_remote_devices accepts both."""
    client = _Client({BRACKET.format(0): "General"})

    chosen = await _async_stream_still_coordinators([_Coordinator(0, client)])

    assert len(chosen) == 1


@pytest.mark.parametrize("protocol", ["Private", "private", "PRIVATE"])
async def test_the_recorders_own_protocol_is_never_chosen(protocol):
    """However the recorder spells it."""
    client = _Client({UUID.format(0): protocol})

    assert await _async_stream_still_coordinators([_Coordinator(0, client)]) == set()


# --- and the cases that must change nothing -----------------------------------


async def test_a_channel_that_stated_no_protocol_is_left_alone():
    """Absence of an answer is not evidence that a snapshot will fail."""
    client = _Client({UUID.format(0): "   ", UUID.format(1): "Private"})
    coordinators = [_Coordinator(channel=c, client=client) for c in range(2)]

    assert await _async_stream_still_coordinators(coordinators) == set()


@pytest.mark.parametrize("table", [None, "", [], {}, 0])
async def test_a_reply_that_is_not_a_table_changes_nothing(table):
    client = _Client(table)

    assert await _async_stream_still_coordinators([_Coordinator(0, client)]) == set()


async def test_a_table_that_could_not_be_read_changes_nothing():
    """A refusal leaves every channel asking snapshot.cgi, as it does today."""
    client = _Client(error=OSError("no answer"))

    assert await _async_stream_still_coordinators([_Coordinator(0, client)]) == set()


async def test_a_client_that_cannot_be_asked_changes_nothing():
    """A client with no such method, as an older stand-in would be."""
    bare_client = SimpleNamespace()

    assert (
        await _async_stream_still_coordinators([_Coordinator(0, bare_client)]) == set()
    )


async def test_a_coordinator_that_cannot_be_asked_is_skipped():
    """The stand-in coordinators in the tests predate this question."""
    bare = SimpleNamespace(client=_Client(MEASURED))

    assert await _async_stream_still_coordinators([bare]) == set()


# --- the cost of asking -------------------------------------------------------


async def test_the_table_is_read_once_per_host_not_once_per_channel():
    """A getConfig is a login the recorder writes to its own log."""
    client = _Client(MEASURED)
    coordinators = [_Coordinator(channel=c, client=client) for c in range(4)]

    await _async_stream_still_coordinators(coordinators)

    assert client.calls == 1


async def test_two_hosts_are_each_read_once():
    first, second = _Client(MEASURED), _Client(MEASURED)
    coordinators = [
        _Coordinator(channel=1, client=first, address="10.0.0.1"),
        _Coordinator(channel=1, client=second, address="10.0.0.2"),
    ]

    chosen = await _async_stream_still_coordinators(coordinators)

    assert first.calls == 1 and second.calls == 1
    assert len(chosen) == 2


# --- what the entity does with it ---------------------------------------------


def _entry():
    return SimpleNamespace(title="Recorder", data={}, options={})


@pytest.mark.parametrize("stream_stills", [True, False])
def test_the_entity_reports_what_setup_decided(stream_stills):
    camera = DahuaCamera(
        _Coordinator(channel=1), 0, _entry(), stream_stills=stream_stills
    )

    assert camera.use_stream_for_stills is stream_stills


def test_a_camera_asks_for_a_snapshot_unless_told_otherwise():
    """The default, so nothing that constructs one positionally changes."""
    assert DahuaCamera(_Coordinator(), 0, _entry()).use_stream_for_stills is False


# --- and that setup hands it over ---------------------------------------------


async def test_setup_passes_the_flag_to_the_channel_that_needs_it(monkeypatch):
    """End to end through the real async_setup_entry."""
    built = []

    class _Recorder:
        def __init__(self, coordinator, stream_index, config_entry, **kwargs):
            built.append((coordinator.get_channel(), kwargs.get("stream_stills")))

    monkeypatch.setattr(camera_module, "DahuaCamera", _Recorder)
    monkeypatch.setattr(
        camera_module.entity_platform,
        "async_get_current_platform",
        lambda: SimpleNamespace(async_register_entity_service=lambda *a, **k: None),
    )

    client = _Client(MEASURED)
    coordinators = {channel: _RecorderChannel(channel, client) for channel in range(3)}
    entry = SimpleNamespace(runtime_data=coordinators, data={}, options={})

    await camera_module.async_setup_entry(None, entry, lambda *a, **k: None)

    assert sorted(set(built)) == [(0, False), (1, True), (2, False)]


class _RecorderChannel(_Coordinator):
    """A channel of a recorder, as the platform reads one."""

    subentry_id = None

    def __init__(self, channel, client):
        super().__init__(channel=channel, client=client)

    def get_model(self):
        return "DHI-NVR5232-16P-EI"

    def get_max_streams(self):
        return 1

    def supports_infrared_light(self):
        return False

    def supports_illuminator(self):
        return False
