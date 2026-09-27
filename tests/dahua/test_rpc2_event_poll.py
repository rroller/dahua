"""Tests for the RPC2 event poll used by devices with no CGI at all.

The two lifecycle behaviours here were both reported against the first version of
the poller, and both are invisible in normal use: one only shows on a camera that
has been quiet for a whole stream lifetime, the other only after a transport
failure that happens to span the end of a motion event.
"""
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient, EventStreamClosed
from custom_components.dahua.dahua_utils import parse_event
from custom_components.dahua.rpc2 import Rpc2MethodRefused


class _StopPoll(Exception):
    """Escape the poller's endless loop once the script is exhausted.

    Deliberately not an aiohttp/OS error: those are caught and turned into
    EventStreamClosed, which would hide whether the script ran to the end.
    """


class _FakeRpc2Client:
    """Serves a scripted answer per cycle for eventManager.getEventIndexes."""

    def __init__(self, script, refuse=()):
        self._script = script
        self._refuse = set(refuse)
        self._served = {}
        self.attaches = 0
        self.asked = []

    async def request(self, method, params=None, object_id=None, **kwargs):
        if method == "eventManager.attach":
            self.attaches += 1
            return {"result": True, "params": {"SID": 1234}}
        if method != "eventManager.getEventIndexes":
            return {"result": True, "params": {}}

        code = params["code"]
        if code in self._refuse:
            raise Rpc2MethodRefused("refused", code=268632064, message="InterfaceNotFound")

        cycle = self._served.get(code, 0)
        self._served[code] = cycle + 1
        if cycle >= len(self._script):
            raise _StopPoll()
        self.asked.append((cycle, code))
        return {"result": True, "params": {"indexes": self._script[cycle].get(code, [])}}


class _FakeHolder:
    def __init__(self, client):
        self.client = client
        self.task = object()   # identity is all the poller uses
        self.keepalive = None


def _client(monkeypatch, fake, address="cam"):
    """A DahuaClient whose shared RPC2 session is the fake."""
    client = DahuaClient("u", "p", address, 80, 554, object())
    holder = _FakeHolder(fake)

    async def _shared(self):
        return holder

    monkeypatch.setattr(DahuaClient, "_shared_rpc2", _shared)
    # Keep the cycle from sleeping; the rate bound is not what is under test.
    monkeypatch.setattr(client_module, "RPC2_EVENT_POLL_SECONDS", 0)
    monkeypatch.setattr(client_module, "RPC2_EVENT_MAX_REQUESTS_PER_SECOND", 1000)
    return client


def _collect():
    received = []
    return received, lambda data, channel: received.append((data, channel))


async def test_heartbeat_parses_to_no_event():
    """It must prove the transport is alive without inventing an event."""
    assert list(parse_event(client_module.RPC2_EVENT_HEARTBEAT.decode())) == []


async def test_idle_cycle_still_reports_transport_activity(monkeypatch):
    """A quiet camera must not look like a dead stream.

    on_receive is what sets DahuaHostEventStream._received_data, and it does so
    before parsing precisely so a heartbeat counts. Without a call here, a camera
    with nothing happening delivers nothing for a whole stream lifetime, and the
    recycle is then read as a silent stream: warned about, and backed off from for
    up to ten minutes at a time.
    """
    fake = _FakeRpc2Client([{"VideoMotion": []}, {"VideoMotion": []}])
    client = _client(monkeypatch, fake)
    received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert received, "an idle cycle delivered nothing at all"
    assert all(data == client_module.RPC2_EVENT_HEARTBEAT for data, _ in received)
    # One per successful cycle, not one per code.
    assert len(received) == 2


async def test_active_event_emits_start_then_stop(monkeypatch):
    """The ordinary edges, within one poller."""
    fake = _FakeRpc2Client([{"VideoMotion": [0]}, {"VideoMotion": []}])
    client = _client(monkeypatch, fake)
    received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    events = [e for data, _ in received for e in parse_event(data.decode())]
    assert [(e["Code"], e["action"], e["index"]) for e in events] == [
        ("VideoMotion", "Start", "0"),
        ("VideoMotion", "Stop", "0"),
    ]


async def test_stop_is_still_emitted_after_the_poller_restarts(monkeypatch):
    """A Start owed a Stop must get one from the poller's replacement.

    The set of active events used to be local, so a restart began empty: if an
    event went inactive while the transport was down, the new poller compared an
    empty device against an empty memory and emitted nothing. The ordinary motion
    sensor has no auto-off, so it stayed on until the next full motion event.
    """
    first = _FakeRpc2Client([{"VideoMotion": [0]}])
    client = _client(monkeypatch, first)
    received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    started = [e for data, _ in received for e in parse_event(data.decode())]
    assert [(e["Code"], e["action"]) for e in started] == [("VideoMotion", "Start")]

    # The device goes quiet while the poller is away, and a new one takes over.
    second = _FakeRpc2Client([{"VideoMotion": []}])
    client2 = _client(monkeypatch, second)
    received2, on_receive2 = _collect()

    with pytest.raises(_StopPoll):
        await client2._stream_events_rpc2(on_receive2, ["VideoMotion"], 0)

    stopped = [e for data, _ in received2 for e in parse_event(data.decode())]
    assert [(e["Code"], e["action"], e["index"]) for e in stopped] == [
        ("VideoMotion", "Stop", "0")
    ], "the replacement poller never cleared the earlier Start"


async def test_state_is_per_host(monkeypatch):
    """One camera's active event is not another camera's stale Start."""
    first = _FakeRpc2Client([{"VideoMotion": [0]}])
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, first, "cam-a")._stream_events_rpc2(
            _collect()[1], ["VideoMotion"], 0)

    other = _FakeRpc2Client([{"VideoMotion": []}])
    received, on_receive = _collect()
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, other, "cam-b")._stream_events_rpc2(
            on_receive, ["VideoMotion"], 0)

    events = [e for data, _ in received for e in parse_event(data.decode())]
    assert events == [], "cam-b was given cam-a's Start to clear"


async def test_a_code_the_device_refuses_is_dropped_and_cleared(monkeypatch):
    """A code that can no longer be observed must not stay on forever."""
    first = _FakeRpc2Client([{"SmartMotionHuman": [0]}])
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, first)._stream_events_rpc2(
            _collect()[1], ["SmartMotionHuman"], 0)

    # Now the device refuses that code, so it is dropped from the poll -- and the
    # Start it is still holding has to be released on the way.
    second = _FakeRpc2Client([{"VideoMotion": []}], refuse={"SmartMotionHuman"})
    received, on_receive = _collect()
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, second)._stream_events_rpc2(
            on_receive, ["SmartMotionHuman", "VideoMotion"], 0)

    events = [e for data, _ in received for e in parse_event(data.decode())]
    assert ("SmartMotionHuman", "Stop") in [(e["Code"], e["action"]) for e in events]


async def test_no_pollable_codes_ends_the_stream(monkeypatch):
    """If the device refuses everything, say so rather than spin."""
    fake = _FakeRpc2Client([{}], refuse={"VideoMotion"})
    client = _client(monkeypatch, fake)

    with pytest.raises(EventStreamClosed):
        await client._stream_events_rpc2(_collect()[1], ["VideoMotion"], 0)
