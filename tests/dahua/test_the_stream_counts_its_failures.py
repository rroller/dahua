"""What the shared event stream remembers between attaches.

`_async_run` holds the stream open and re-attaches when it ends. Around that sits a
small amount of bookkeeping that decides three separate things, and none of it was
executed by the suite outside the credential-refusal path:

* how long to wait before re-attaching, via `event_stream_retry_delay`;
* whether to say anything, which is once per outage rather than once per retry;
* `_consecutive_failures`, which is shared with the credential budget.

The distinction it all turns on is **whether anything arrived**. A stream that attached,
delivered events for an hour and then hit `EVENT_STREAM_MAX_LIFETIME_SECONDS` is a
working stream being deliberately recycled. A stream that attached and delivered nothing,
not even the heartbeat it asked for, is a device refusing to talk. Both arrive here as
`asyncio.TimeoutError`, because aiohttp's `ServerTimeoutError` is one, so the only thing
separating a healthy recycle from a dead subscription is `_received_data`.

Getting that backwards would not fail loudly. A working stream would climb the failure
counter on every recycle, back off further each time, and warn about an outage that is
not happening. Nothing here reads as an error, which is why it is worth pinning.

The retry delay itself is a pure function tested in `test_failure_backoff.py`,
`test_stream_jitter.py` and `test_stream_closed_but_alive.py`. These tests replace it and
assert **what it is told**, because that is the contract the bookkeeping owes it.
"""

import asyncio

import pytest

from custom_components.dahua import host as host_module
from custom_components.dahua.host import DahuaHostEventStream

ADDRESS = "10.0.0.5"

# Carried by whatever ends an attach, so the log assertions can look for it.
REASON = "the peer hung up"

# What one attach does before it ends. "quiet" delivers nothing, which is the device
# refusing to talk; "talks" delivers a heartbeat first, which is a stream that worked.
QUIET_TIMEOUT = ("quiet", asyncio.TimeoutError)
TALKS_THEN_TIMEOUT = ("talks", asyncio.TimeoutError)
QUIET_ERROR = ("quiet", ConnectionResetError)
TALKS_THEN_ERROR = ("talks", ConnectionResetError)


class _Attaches:
    """A client that scripts what each attach does, then ends the loop.

    `_async_run` is a `while True`, so something has to stop it. It re-raises
    `CancelledError` untouched, which is how Home Assistant stops it in production
    too, so running out of script raises that and the test catches it. A test that
    forgot to would report a timeout rather than a failure.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.attaches = []

    async def stream_events(self, on_receive, events, channel):
        self.attaches.append(list(events))
        if not self.script:
            raise asyncio.CancelledError
        talks, ending = self.script.pop(0)
        if talks == "talks":
            on_receive(b"Heartbeat", 0)
        raise ending(REASON)


def _stream(client, **kwargs):
    """A stream built the way `test_refused_credentials.py` builds one: only the
    attributes `_async_run` reads, so a test cannot pass by accident on state it
    never set."""
    stream = object.__new__(DahuaHostEventStream)
    stream._address = ADDRESS
    stream._events = frozenset({"VideoMotion"})
    stream._received_data = False
    stream._failing = False
    stream._consecutive_failures = 0
    stream._by_channel = {}
    stream._owner = type("_Owner", (), {"client": client})()
    for name, value in kwargs.items():
        setattr(stream, name, value)
    return stream


@pytest.fixture
def retries(monkeypatch):
    """Record what the retry delay is told, and never actually wait.

    Patched on `host_module`, where `_async_run` looks the name up. A real delay here
    is sixty seconds at the short end, so the suite's nine second timeout would fire
    long before the second attach.
    """
    asked = []

    def fake(lived_seconds, consecutive_failures=0, received_data=False):
        asked.append({
            "lived": lived_seconds,
            "failures": consecutive_failures,
            "received_data": received_data,
        })
        return 0

    monkeypatch.setattr(host_module, "event_stream_retry_delay", fake)
    return asked


def _warnings(caplog):
    """This integration's warnings only. Asserting on every WARNING in the process
    would make these tests depend on what the harness logs."""
    return [r.getMessage() for r in caplog.records
            if r.levelname == "WARNING"
            and r.name.startswith("custom_components.dahua")]


async def _run(stream):
    with pytest.raises(asyncio.CancelledError):
        await stream._async_run()


# --- a working stream being recycled is not a failure -----------------------

async def test_a_recycled_stream_is_not_counted_as_a_failure(retries):
    """The deliberate recycle. `wait_for` fires at the maximum lifetime on a stream
    that has been delivering events all along, and that must leave the counter where
    it was: it is shared with the credential budget, so a long-lived host would
    otherwise spend its budget on its own successful recycles."""
    stream = _stream(_Attaches(TALKS_THEN_TIMEOUT))

    await _run(stream)

    assert [r["failures"] for r in retries] == [0]
    assert retries[0]["received_data"] is True
    assert stream._consecutive_failures == 0
    assert stream._failing is False


async def test_a_recycled_stream_says_nothing(retries, caplog):
    """Nothing is wrong, so nothing is logged. A warning per recycle would be one
    per hour, for ever, on every healthy device."""
    await _run(_stream(_Attaches(TALKS_THEN_TIMEOUT)))

    assert _warnings(caplog) == []


async def test_recycling_clears_a_failure_that_came_before_it(retries):
    """Recovery. The counter is cleared by the attach that works, not by a timer,
    so a device that comes back stops being backed off immediately."""
    stream = _stream(_Attaches(TALKS_THEN_TIMEOUT),
                     _consecutive_failures=4, _failing=True)

    await _run(stream)

    assert stream._consecutive_failures == 0
    assert stream._failing is False


# --- a stream that attached and delivered nothing ---------------------------

async def test_a_silent_attach_is_counted(retries):
    """The opposite case arriving as the same exception. Nothing was delivered, so
    this is a device that is not talking and the count has to climb, otherwise the
    retry never backs off."""
    stream = _stream(_Attaches(QUIET_TIMEOUT, QUIET_TIMEOUT, QUIET_TIMEOUT))

    await _run(stream)

    assert [r["failures"] for r in retries] == [1, 2, 3]
    assert all(r["received_data"] is False for r in retries)
    assert stream._failing is True


async def test_a_silent_attach_says_what_to_try(retries, caplog):
    """The one thing a user can do about it. A subscription the firmware will not
    serve looks exactly like a dead camera from Home Assistant, and the advice is
    not guessable from anything else in the log."""
    await _run(_stream(_Attaches(QUIET_TIMEOUT)))

    said = _warnings(caplog)
    assert len(said) == 1
    assert ADDRESS in said[0]
    assert "fewer" in said[0], said[0]


async def test_it_is_said_once_per_outage_not_once_per_retry(retries, caplog):
    """Three silent attaches, one warning. Silence was the old behaviour and it is
    why these failures went unreported; a warning every minute for ever is the other
    extreme, and the middle is a flag that stays set."""
    await _run(_stream(_Attaches(QUIET_TIMEOUT, QUIET_TIMEOUT, QUIET_TIMEOUT)))

    assert len(_warnings(caplog)) == 1


async def test_a_second_outage_is_reported_again(retries, caplog):
    """Once per outage, so a device that broke, recovered and broke again is two
    reports rather than one. A flag that was never cleared would hide the second."""
    await _run(_stream(_Attaches(
        QUIET_TIMEOUT, TALKS_THEN_TIMEOUT, QUIET_TIMEOUT)))

    assert len(_warnings(caplog)) == 2


# --- a stream that ended some other way -------------------------------------

async def test_a_stream_that_talked_and_then_died_does_not_climb(retries):
    """The socket ending is not the device refusing contact. It attached and it
    delivered, so whatever closed it is not a reason to back off further or to
    spend the credential budget."""
    stream = _stream(_Attaches(TALKS_THEN_ERROR))

    await _run(stream)

    assert [r["failures"] for r in retries] == [0]
    assert retries[0]["received_data"] is True


async def test_a_stream_that_died_without_talking_climbs(retries):
    stream = _stream(_Attaches(QUIET_ERROR, QUIET_ERROR))

    await _run(stream)

    assert [r["failures"] for r in retries] == [1, 2]
    assert stream._failing is True


async def test_the_failure_is_reported_with_its_cause(retries, caplog):
    """The exception text, because "the stream ended" alone has never been enough to
    act on: a refused subscription, a reset socket and a closed session all end it."""
    await _run(_stream(_Attaches(QUIET_ERROR)))

    said = _warnings(caplog)
    assert len(said) == 1
    assert REASON in said[0], said[0]


# --- and what it attached with ----------------------------------------------

async def test_the_reattach_asks_for_the_same_codes(retries):
    """The retry is a retry, not a renegotiation. A subscription that changed shape
    between attempts would make an intermittent failure impossible to reason about."""
    client = _Attaches(QUIET_TIMEOUT, QUIET_TIMEOUT)
    stream = _stream(client, _events=frozenset({"VideoMotion", "AlarmLocal"}))

    await _run(stream)

    assert client.attaches == [
        ["AlarmLocal", "VideoMotion"],
        ["AlarmLocal", "VideoMotion"],
        ["AlarmLocal", "VideoMotion"],
    ]


async def test_a_broadened_subscription_reattaches_as_All(retries):
    """And keeps being All. `_using_all_events` is decided when the channels change,
    not per attach, so a retry must not quietly fall back to the explicit list the
    firmware went silent on."""
    client = _Attaches(QUIET_TIMEOUT)
    stream = _stream(client, _using_all_events=True)

    await _run(stream)

    assert client.attaches == [["All"], ["All"]]
