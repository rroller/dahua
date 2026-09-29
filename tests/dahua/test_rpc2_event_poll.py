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

    def __init__(self, script, refuse=(), refuse_for=None):
        self._script = script
        self._refuse = set(refuse)
        # code -> how many of the first requests for it to refuse. A device that
        # refuses and then answers could not be expressed before, which is the
        # shape the poller used to lose a code to for good.
        self._refuse_for = dict(refuse_for or {})
        self._refused = {}
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

        if self._refused.get(code, 0) < self._refuse_for.get(code, 0):
            self._refused[code] = self._refused.get(code, 0) + 1
            raise Rpc2MethodRefused("busy", code=287638033,
                                    message="Request length error!")

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


async def test_a_cycle_that_answered_nothing_reports_no_activity(monkeypatch):
    """The heartbeat means the transport is healthy, so it has to be earned.

    It used not to need guarding: a refusal dropped the code there and then, so
    any cycle that got as far as the heartbeat had been answered by definition.
    Now that a refusal is tolerated for a few cycles, a cycle where the device
    refused everything reaches it -- and reporting the transport healthy on the
    strength of that is how a stream nothing is coming out of stays un-recycled.
    """
    fake = _FakeRpc2Client([{"VideoMotion": []}] * 3,
                           refuse_for={"VideoMotion": 2})
    received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await _client(monkeypatch, fake)._stream_events_rpc2(
            on_receive, ["VideoMotion"], 0)

    # Two refused cycles, then three answered ones before the script runs out.
    assert len(received) == 3, \
        "a cycle in which the device refused everything sent a heartbeat"


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
    # Start it is still holding has to be released on the way. It takes
    # RPC2_EVENT_REFUSALS_BEFORE_DROPPING cycles to get there now, so the script
    # has to keep the other code answering for at least that many.
    second = _FakeRpc2Client([{"VideoMotion": []}] * 4, refuse={"SmartMotionHuman"})
    received, on_receive = _collect()
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, second)._stream_events_rpc2(
            on_receive, ["SmartMotionHuman", "VideoMotion"], 0)

    events = [e for data, _ in received for e in parse_event(data.decode())]
    assert ("SmartMotionHuman", "Stop") in [(e["Code"], e["action"]) for e in events]


async def test_a_code_refused_once_is_asked_again(monkeypatch):
    """A refusal is not a statement that the device does not know the code.

    A recorder with sixteen channels competing for it refuses plenty of things it
    serves on the next cycle, and this used to cost that event type for the life
    of the entry -- with a debug line as the only record. #823 is where the
    refusal reasons started being kept; this is one of them mattering.
    """
    fake = _FakeRpc2Client([{"VideoMotion": []}] * 3,
                           refuse_for={"VideoMotion": 1})
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, fake)._stream_events_rpc2(
            _collect()[1], ["VideoMotion"], 0)

    assert fake.asked, "the code was dropped on its first refusal"


async def test_a_transient_refusal_does_not_cost_the_event(monkeypatch):
    """The point of it: the event still arrives once the device answers."""
    fake = _FakeRpc2Client([{"VideoMotion": [0]}] * 3,
                           refuse_for={"VideoMotion": 2})
    received, on_receive = _collect()
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, fake)._stream_events_rpc2(
            on_receive, ["VideoMotion"], 0)

    events = [(e["Code"], e["action"])
              for data, _ in received for e in parse_event(data.decode())]
    assert ("VideoMotion", "Start") in events


async def test_a_success_between_refusals_starts_the_count_again(monkeypatch):
    """Consecutive, not cumulative. A code refused once in a while is a device
    under load, and totting those up would eventually drop a code that works.

    `refuse_for` cannot express refusing every other time, hence the wrapper. The
    discriminating assertion is the `_StopPoll`: counting cumulatively drops the
    code on the third refusal, which empties the selection and raises
    EventStreamClosed instead, so this test would not reach the script's end.
    """
    monkeypatch.setattr(client_module, "RPC2_EVENT_REFUSALS_BEFORE_DROPPING", 2)
    fake = _FakeRpc2Client([{"VideoMotion": []}] * 6)

    calls = {"n": 0}
    inner = fake.request

    async def alternating(method, params=None, **kwargs):
        if method == "eventManager.getEventIndexes":
            calls["n"] += 1
            if calls["n"] % 2:
                raise Rpc2MethodRefused("busy", code=287638033,
                                        message="Request length error!")
        return await inner(method, params, **kwargs)

    fake.request = alternating
    with pytest.raises(_StopPoll):
        await _client(monkeypatch, fake)._stream_events_rpc2(
            _collect()[1], ["VideoMotion"], 0)

    assert calls["n"] > 4, \
        "the code was dropped despite answering between refusals"


async def test_no_pollable_codes_ends_the_stream(monkeypatch):
    """If the device refuses everything, say so rather than spin."""
    fake = _FakeRpc2Client([{}], refuse={"VideoMotion"})
    client = _client(monkeypatch, fake)

    with pytest.raises(EventStreamClosed):
        await client._stream_events_rpc2(_collect()[1], ["VideoMotion"], 0)


# --- easing off while nothing is happening -----------------------------------
#
# Nine default codes at a 2s cycle is about 4.5 requests a second, forever,
# against a small camera -- roughly two orders of magnitude above anything else
# this integration does, and #603 is the shape where one of these boxes runs out
# of whatever it runs out of and stops answering. Motion is bursty, so the fast
# rate only earns its keep near an event.
#
# The idle interval is bounded by evidence rather than taste: #779 measured
# VideoMotion lasting 11s and SmartMotionHuman 46s on the device this was written
# for, so an idle poll at 8s still sees both.

def _record_sleeps(monkeypatch):
    """What the poller actually waits between cycles."""
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    return waits


def _idle_settings(monkeypatch, after=0, idle=99):
    monkeypatch.setattr(client_module, "RPC2_EVENT_IDLE_AFTER_SECONDS", after)
    monkeypatch.setattr(client_module, "RPC2_EVENT_IDLE_POLL_SECONDS", idle)


async def test_a_quiet_camera_eases_off(monkeypatch):
    fake = _FakeRpc2Client([{"VideoMotion": []}, {"VideoMotion": []}])
    client = _client(monkeypatch, fake)
    _idle_settings(monkeypatch)
    waits = _record_sleeps(monkeypatch)
    _, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert waits, "the poller never waited at all"
    assert all(w > 50 for w in waits), (
        "a camera with nothing happening kept the fast rate: %s" % waits)


async def test_a_busy_camera_keeps_the_fast_cycle(monkeypatch):
    """Something active means the fast rate, and this is the half that matters:
    easing off must not make it slow to react once something happens."""
    fake = _FakeRpc2Client([{"VideoMotion": [0]}, {"VideoMotion": [0]}])
    client = _client(monkeypatch, fake)
    _idle_settings(monkeypatch)
    waits = _record_sleeps(monkeypatch)
    _, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert waits
    assert all(w < 50 for w in waits), (
        "an active camera was polled at the idle rate: %s" % waits)


async def test_activity_snaps_it_back(monkeypatch):
    """Idle, then something happens. The very next wait is the fast one."""
    fake = _FakeRpc2Client([
        {"VideoMotion": []},       # idle -> eased off
        {"VideoMotion": [0]},      # active -> back to fast
    ])
    client = _client(monkeypatch, fake)
    _idle_settings(monkeypatch)
    waits = _record_sleeps(monkeypatch)
    _, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert len(waits) == 2, waits
    assert waits[0] > 50, "the idle cycle did not ease off"
    assert waits[1] < 50, "it stayed slow while something was active"


async def test_it_does_not_ease_off_before_the_idle_period(monkeypatch):
    """One quiet cycle is not a quiet camera."""
    fake = _FakeRpc2Client([{"VideoMotion": []}, {"VideoMotion": []}])
    client = _client(monkeypatch, fake)
    _idle_settings(monkeypatch, after=3600)
    waits = _record_sleeps(monkeypatch)
    _, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert all(w < 50 for w in waits), waits


async def test_easing_off_never_polls_faster_than_the_rate_bound(monkeypatch):
    """With enough codes the request bound already gives a cycle longer than the
    idle interval, and that bound must still win."""
    fake = _FakeRpc2Client([{}, {}])
    client = _client(monkeypatch, fake)
    monkeypatch.setattr(client_module, "RPC2_EVENT_POLL_SECONDS", 200)
    _idle_settings(monkeypatch, after=0, idle=8)
    waits = _record_sleeps(monkeypatch)
    _, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion", "AlarmLocal"], 0)

    assert all(w > 100 for w in waits), (
        "easing off overrode the rate bound and polled faster: %s" % waits)


def test_the_idle_interval_can_still_see_the_shortest_measured_event():
    """#779 measured VideoMotion lasting 11s on the SL300. An idle poll longer
    than that would miss a real event to save requests, which is the wrong trade."""
    assert client_module.RPC2_EVENT_IDLE_POLL_SECONDS < 11


def test_easing_off_is_a_real_reduction():
    """If the idle interval were not meaningfully longer than the fast one this
    would be complexity for nothing."""
    assert (client_module.RPC2_EVENT_IDLE_POLL_SECONDS
            >= 2 * client_module.RPC2_EVENT_POLL_SECONDS)


# --- what the fallback tells the user (#783, @matthewdva) ---------------------

def test_the_fallback_message_explains_the_rest_of_the_damage():
    """A device with no CGI does not only lose its event stream.

    The same absent CGI makes the identity probes fall back, so it reports as
    Generic RTSP on firmware 1.0 with its CGI-only entities missing. Without this
    the user reads that as a second, unrelated fault. Lost once already, in the
    first version of the backoff, so it is pinned here.
    """
    import inspect

    source = inspect.getsource(DahuaClient.stream_events)
    assert "Generic RTSP" in source
    assert "identity_fallbacks" in source


def test_the_slow_cycle_warning_says_the_rate():
    """The warning is the line a user actually sees; a code count and an interval
    are not the units "my camera stopped answering" arrives in."""
    import inspect

    source = inspect.getsource(DahuaClient._stream_events_rpc2)
    warning = source.split("_LOGGER.warning")[1].split("_LOGGER.debug")[0]
    assert "requests a second" in warning


def test_the_idle_interval_keeps_real_margin():
    """6s against an 11s measured event leaves five seconds; 8s left three."""
    assert client_module.RPC2_EVENT_IDLE_POLL_SECONDS <= 6


# --- an expired login, which is the normal case on a quiet camera -------------
#
# The RPC2 session times out on idle. On a device with no CGI this poll is the only
# path events have, so a refusal meaning "your session is gone" has to end in a new
# login and a new attach rather than a closed stream: the subscription belongs to the
# login, so a recovered session polling the old SID would ask about something the
# device has forgotten.

SESSION_EXPIRED = client_module.RPC2_SESSION_EXPIRED_CODE


class _RefusingFirst(_FakeRpc2Client):
    """Refuses the first `times` getEventIndexes calls with `refusal`, then behaves.

    Optionally swaps the holder's login task on the way, which is how another caller
    having already re-logged-in is simulated.
    """

    def __init__(self, script, refusal, times=1, holder_to_steal=None):
        super().__init__(script)
        self._refusal = refusal
        self._times = times
        self._raised = 0
        self._holder_to_steal = holder_to_steal

    async def request(self, method, params=None, object_id=None, **kwargs):
        if method == "eventManager.getEventIndexes" and self._raised < self._times:
            self._raised += 1
            if self._holder_to_steal is not None:
                self._holder_to_steal.task = object()
            raise self._refusal()
        return await super().request(method, params, object_id, **kwargs)


def _client_and_holder(monkeypatch, fake, address="cam"):
    """Like _client, but hands back the holder so the login drop can be observed."""
    client = DahuaClient("u", "p", address, 80, 554, object())
    holder = _FakeHolder(fake)
    holder.relogins = 0

    async def _shared(self):
        # The real one logs in again when the task has been dropped, and the poller
        # recognises a new login by that object's identity. A fake that handed the
        # same None back for ever would make the re-attach look like it never fires.
        if holder.task is None:
            holder.task = object()
            holder.relogins += 1
        return holder

    monkeypatch.setattr(DahuaClient, "_shared_rpc2", _shared)
    monkeypatch.setattr(client_module, "RPC2_EVENT_POLL_SECONDS", 0)
    monkeypatch.setattr(client_module, "RPC2_EVENT_MAX_REQUESTS_PER_SECOND", 1000)
    return client, holder


def _expired():
    return Rpc2MethodRefused("expired", code=SESSION_EXPIRED,
                             message="Component error: session invalid!")


async def test_an_expired_login_does_not_end_the_poll(monkeypatch):
    """The whole point. A session expiry is routine on a camera nobody walks past, and
    ending the stream for it would stop events until something reloaded the entry."""
    fake = _RefusingFirst([{"VideoMotion": [0]}], _expired)
    client, holder = _client_and_holder(monkeypatch, fake)
    received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert fake.asked, "the poll never got past the refusal"
    assert received, "no events after recovering from the expiry"


async def test_an_expired_login_is_dropped_so_the_next_cycle_logs_in(monkeypatch):
    """`task` is what the registry uses to decide whether to log in again, and the
    session id is cleared with it."""
    fake = _RefusingFirst([{"VideoMotion": [0]}], _expired)
    client, holder = _client_and_holder(monkeypatch, fake)
    original = holder.task
    _received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert holder.task is not original, "the expired login was kept"
    assert holder.relogins == 1, (
        "logged in %d time(s); the expired session was reused"
        % holder.relogins)


async def test_a_new_login_gets_a_new_attach(monkeypatch):
    """The subscription belongs to the login. Without re-attaching, a recovered
    session polls a SID the device has already forgotten."""
    fake = _RefusingFirst([{"VideoMotion": [0]}], _expired)
    client, _ = _client_and_holder(monkeypatch, fake)
    _received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert fake.attaches >= 2, (
        "attached %d time(s); the recovered login reused the old subscription"
        % fake.attaches)


async def test_a_session_refusal_is_recognised_by_what_the_device_says(monkeypatch):
    """Some firmware says the session is out of date in words rather than with the
    documented code. Both paths used to disagree about this, so the same device
    recovered on one and had its event stream closed on the other."""
    def _in_words():
        return Rpc2MethodRefused("stale", code=268632064,
                                 message="Component error: session out of date")

    fake = _RefusingFirst([{"VideoMotion": [0]}], _in_words)
    client, holder = _client_and_holder(monkeypatch, fake)
    _received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert holder.relogins == 1, (
        "a session problem stated in words was not recognised, so no new login")


async def test_an_expired_login_is_not_counted_against_the_code(monkeypatch):
    """A code is dropped after enough consecutive refusals, on the grounds that the
    device does not know it. A session expiry is not evidence of that, so it must not
    accumulate: on a camera quiet enough to expire the session repeatedly, the code
    would otherwise be dropped for good."""
    limit = client_module.RPC2_EVENT_REFUSALS_BEFORE_DROPPING
    fake = _RefusingFirst([{"VideoMotion": [0]}], _expired, times=limit + 2)
    client, _ = _client_and_holder(monkeypatch, fake)
    _received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert fake.asked, (
        "VideoMotion was dropped after %d session expiries, so a quiet camera loses "
        "the event it was watching for" % (limit + 2))


async def test_a_refusal_that_is_not_a_session_problem_closes_the_stream(monkeypatch):
    """The gate has to still close. A device refusing the attach outright is not
    something a new login fixes, and the caller's backoff is the right place for it."""
    class _RefusingAttach(_FakeRpc2Client):
        async def request(self, method, params=None, object_id=None, **kwargs):
            if method == "eventManager.attach":
                raise Rpc2MethodRefused("no", code=268632064,
                                        message="InterfaceNotFound")
            return await super().request(method, params, object_id, **kwargs)

    client, _ = _client_and_holder(monkeypatch, _RefusingAttach([{}]))
    _received, on_receive = _collect()

    with pytest.raises(EventStreamClosed):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)


async def test_a_login_another_caller_already_replaced_is_left_alone(monkeypatch):
    """Only the login that failed is dropped. Channels of one recorder share this
    session, so nulling whatever is there now would throw away a fresh login somebody
    else just paid for, and the next cycle would pay for another."""
    fake = _RefusingFirst([{"VideoMotion": [0]}], _expired)
    client, holder = _client_and_holder(monkeypatch, fake)
    fake._holder_to_steal = holder
    _received, on_receive = _collect()

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

    assert holder.relogins == 0, (
        "dropped a login this cycle had not failed on, so the replacement was wasted")
