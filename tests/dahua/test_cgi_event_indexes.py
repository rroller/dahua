"""The CGI event-state response is converted to recorder channel indexes."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.dahua import host as host_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.host import DahuaHostEventStream


async def test_get_event_indexes_parses_all_active_channels(monkeypatch):
    client = DahuaClient("u", "p", "recorder", 80, 554, object())
    requested = []

    async def get_bytes(url):
        requested.append(url)
        return b"channels[0]=0\r\nchannels[1]=5\r\nchannels[2]=7\r\n"

    monkeypatch.setattr(client, "get_bytes", get_bytes)

    assert await client.async_get_event_indexes_cgi("VideoMotion") == {0, 5, 7}
    assert requested == [
        "/cgi-bin/eventManager.cgi?action=getEventIndexes&code=VideoMotion"
    ]


async def test_get_event_indexes_accepts_an_empty_response(monkeypatch):
    client = DahuaClient("u", "p", "recorder", 80, 554, object())

    async def get_bytes(_url):
        return b""

    monkeypatch.setattr(client, "get_bytes", get_bytes)

    assert await client.async_get_event_indexes_cgi("VideoMotion") == set()


# --- one state per channel (#1233) ---------------------------------------------
#
# Verbatim from @hakkitorlakcik's DHI-NVR2108HS-8P-S2 on 3.205, curl the only
# client connected. Eight rows every time, one per channel.


def _states(*active):
    return "".join(
        "channels[%d]=%d\r\n" % (channel, 1 if channel in active else 0)
        for channel in range(8)
    ).encode()


async def _asked(monkeypatch, body, code="VideoMotion"):
    client = DahuaClient("u", "p", "recorder", 80, 554, object())

    async def get_bytes(_url):
        return body

    monkeypatch.setattr(client, "get_bytes", get_bytes)
    return await client.async_get_event_indexes_cgi(code)


async def test_an_idle_recorder_that_answers_in_states_has_nothing_active(monkeypatch):
    """Read as a list, eight rows of 0 were channel 0 in motion, for ever."""
    assert _states() == (
        b"channels[0]=0\r\nchannels[1]=0\r\nchannels[2]=0\r\nchannels[3]=0\r\n"
        b"channels[4]=0\r\nchannels[5]=0\r\nchannels[6]=0\r\nchannels[7]=0\r\n"
    )

    assert await _asked(monkeypatch, _states()) == set()


async def test_the_channel_someone_walked_past_is_the_active_one(monkeypatch):
    """15:04:57 and 15:05:04 in the report: channel 2, then channel 7. Read
    as a list, both were channels 0 and 1."""
    assert await _asked(monkeypatch, _states(2)) == {2}
    assert await _asked(monkeypatch, _states(7)) == {7}


async def test_several_channels_active_at_once(monkeypatch):
    assert await _asked(monkeypatch, _states(0, 3, 7)) == {0, 3, 7}
    assert await _asked(monkeypatch, _states(*range(8))) == set(range(8))


async def test_three_rows_are_enough_to_tell(monkeypatch):
    """A list names each active index once, so it cannot have three rows that
    are all 0 or 1."""
    body = b"channels[0]=0\nchannels[1]=1\nchannels[2]=0\n"

    assert await _asked(monkeypatch, body) == {1}


# --- and a list of what is active is still a list ---------------------------------


async def test_the_list_measured_on_the_other_recorder_is_unchanged(monkeypatch):
    """DHI-NVR5464-16P-EI: one row, naming channel 14."""
    assert await _asked(monkeypatch, b"channels[0]=14\r\n") == {14}


async def test_no_events_is_nothing_active(monkeypatch):
    assert await _asked(monkeypatch, b"Error: No Events\r\n") == set()


async def test_one_or_two_rows_are_read_as_a_list(monkeypatch):
    """Undecidable from the answer: `channels[0]=0` is channel 0 active in a
    list and idle in states. Kept as a list, which is what it has always
    been read as, until a device is seen that says otherwise."""
    assert await _asked(monkeypatch, b"channels[0]=0\n") == {0}
    assert await _asked(monkeypatch, b"channels[0]=1\n") == {1}
    assert await _asked(monkeypatch, b"channels[0]=0\nchannels[1]=1\n") == {0, 1}


async def test_three_rows_with_a_larger_value_are_a_list(monkeypatch):
    body = b"channels[0]=0\nchannels[1]=1\nchannels[2]=2\n"

    assert await _asked(monkeypatch, body) == {0, 1, 2}


async def test_rows_that_do_not_count_up_from_zero_are_a_list(monkeypatch):
    """The state shape has a row for every channel, in order. Anything else
    is not it."""
    assert await _asked(
        monkeypatch, b"channels[1]=0\nchannels[2]=1\nchannels[3]=0\n"
    ) == {0, 1}
    assert await _asked(
        monkeypatch, b"channels[0]=0\nchannels[2]=1\nchannels[1]=0\n"
    ) == {0, 1}


async def test_packed_rule_indexes_are_still_decoded(monkeypatch):
    """#1176's camera, two rules active on its one channel."""
    body = b"channels[0]=65536\r\nchannels[1]=131072\r\n"

    assert await _asked(monkeypatch, body, code="CrossRegionDetection") == {0}


# --- what the motion poller does with it ----------------------------------------


async def test_the_poller_reports_the_walk_past_on_the_channels_it_happened_on(
    monkeypatch,
):
    """The report's sequence through the real client and the real poller:
    idle, someone passing channel 2, then channel 7, then idle again.

    Read as a list, the same four answers gave a Start on channel 0 that
    never ended, a Start and a Stop on channel 1, and nothing at all on the
    two channels the person walked past.
    """
    answers = [_states(), _states(2), _states(7), _states()]
    client = DahuaClient("u", "p", "recorder", 80, 554, object())

    async def get_bytes(_url):
        if not answers:
            raise asyncio.CancelledError()
        return answers.pop(0)

    monkeypatch.setattr(client, "get_bytes", get_bytes)
    monkeypatch.setattr(host_module, "VIDEO_MOTION_POLL_SECONDS", 0)
    stream = object.__new__(DahuaHostEventStream)
    stream._address = "recorder"
    stream._owner = SimpleNamespace(client=client)
    dispatched = []
    stream._dispatch_events = lambda data, video_motion_source=None: (
        dispatched.append((data.decode().strip(), video_motion_source))
    )

    with pytest.raises(asyncio.CancelledError):
        await stream._async_poll_video_motion()

    assert dispatched == [
        ("Code=VideoMotion;action=Start;index=2", "poll"),
        ("Code=VideoMotion;action=Start;index=7", "poll"),
        ("Code=VideoMotion;action=Stop;index=2", "poll"),
        ("Code=VideoMotion;action=Stop;index=7", "poll"),
    ]
