"""An IVS rule's index from getEventIndexes names its channel in the low bits.

defre92's DH-IPC-HDW2849TM-S-IL on 3.120 (#1176), polled while walking
through three intrusion zones on its one channel, answered `channels[0]=131072`
(0x20000) and `channels[0]=196608` (0x30000), and both at once when two rules
were active, in either order. Its own event stream reported `index=0` for the
same events. Read whole, the index matched no channel and the event was lost.
"""

import pytest

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.dahua_utils import parse_event

from .test_rpc2_event_poll import _client, _collect, _FakeRpc2Client, _StopPoll

RULE_2 = 0x20000
RULE_3 = 0x30000


def _edges(received):
    return [
        (e["Code"], e["action"], e["index"])
        for data, _ in received
        for e in parse_event(data.decode())
    ]


async def _poll(monkeypatch, script):
    fake = _FakeRpc2Client([{"CrossRegionDetection": s} for s in script])
    received, on_receive = _collect()
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, fake)._stream_events_rpc2(
            on_receive, ["CrossRegionDetection"], 0
        )
    return _edges(received)


async def test_the_capture_reaches_channel_0_as_one_start_and_one_stop(monkeypatch):
    """The reporter's sequence: one rule, then two in both orders, then one
    left, then none. Channel 0 is active throughout, so it is one event."""
    edges = await _poll(
        monkeypatch,
        [[], [RULE_3], [RULE_2], [RULE_2, RULE_3], [RULE_3, RULE_2], [RULE_2], []],
    )

    assert edges == [
        ("CrossRegionDetection", "Start", "0"),
        ("CrossRegionDetection", "Stop", "0"),
    ]


async def test_one_rule_ending_does_not_stop_another_on_the_channel(monkeypatch):
    edges = await _poll(monkeypatch, [[RULE_2, RULE_3], [RULE_3]])

    assert edges == [("CrossRegionDetection", "Start", "0")]


async def test_a_rule_on_another_channel_keeps_its_channel(monkeypatch):
    """The low bits are the channel, so a rule on channel 5 is channel 5."""
    edges = await _poll(monkeypatch, [[RULE_2 | 5], []])

    assert edges == [
        ("CrossRegionDetection", "Start", "5"),
        ("CrossRegionDetection", "Stop", "5"),
    ]


async def test_a_plain_channel_number_is_unchanged(monkeypatch):
    edges = await _poll(monkeypatch, [[14], []])

    assert edges == [
        ("CrossRegionDetection", "Start", "14"),
        ("CrossRegionDetection", "Stop", "14"),
    ]


async def test_the_cgi_reader_gives_the_channel(monkeypatch):
    """The same answer over CGI, as the reporter's curl loop read it. The
    lost-Stop reconcile compares this against latched channels, so read whole
    it would close a channel 0 that was still active."""
    client = DahuaClient("u", "p", "cam", 80, 554, object())

    async def get_bytes(_url):
        return b"channels[0]=131072\r\nchannels[1]=196608\r\n"

    monkeypatch.setattr(client, "get_bytes", get_bytes)

    assert await client.async_get_event_indexes_cgi("CrossRegionDetection") == {0}


@pytest.mark.parametrize(
    "raw, channel",
    [(0, 0), (63, 63), (0xFFFF, 0xFFFF), (0x10000, 0), (RULE_3 | 0x1234, 0x1234)],
)
def test_the_channel_is_the_low_16_bits(raw, channel):
    """Exactly 16: a narrower mask passes every realistic channel above and
    still changes the meaning of a larger one."""
    from custom_components.dahua.client import event_index_channel

    assert event_index_channel(raw) == channel
