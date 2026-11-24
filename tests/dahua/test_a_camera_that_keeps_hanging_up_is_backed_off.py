"""A camera that keeps hanging up talking streams is reconnected less often (#1235).

A stream that delivered anything and then ended inside a minute used to be
retried in ten seconds, every time, for ever. That is right for firmware that
drops a working stream now and then. It was wrong for #1235's camera, which
closed every new stream after about thirty seconds for four hours: 362 fresh
authenticated connections, while the camera's user table filled.

Now the first three short streams in a row keep the ten seconds, then the wait
doubles up to EVENT_STREAM_MAX_SHORT_RETRY_SECONDS, and one stream that lasts
EVENT_STREAM_HEALTHY_SECONDS starts the count again.

Only on a host that has held a stream that long at least once. #1235's camera
ran normally for hours first; firmware that drops every stream after a few
seconds while healthy, the case #645 kept the flat retry for, never does, and
keeps the ten seconds.

The loop tests wrap the real delay function, so they check both what it is told
and what it answers.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

import custom_components.dahua as dahua
from custom_components.dahua import host as host_module
from custom_components.dahua.diagnostics import _stream_block
from custom_components.dahua.host import (
    EVENT_STREAM_HEALTHY_SECONDS,
    EVENT_STREAM_JITTER,
    EVENT_STREAM_MAX_SHORT_RETRY_SECONDS,
    EVENT_STREAM_SHORT_RETRY_SECONDS,
    EVENT_STREAM_SHORT_STREAMS_BEFORE_BACKOFF,
    DahuaHostEventStream,
    event_stream_retry_delay,
)

ADDRESS = "10.0.0.5"


def _about(seconds):
    return pytest.approx(seconds, rel=EVENT_STREAM_JITTER + 0.001)


# --- the delay itself ----------------------------------------------------------


def test_the_numbers_this_was_built_with():
    """Pinned so a change to any of them is a deliberate one."""
    assert EVENT_STREAM_SHORT_RETRY_SECONDS == 10
    assert EVENT_STREAM_SHORT_STREAMS_BEFORE_BACKOFF == 3
    assert EVENT_STREAM_MAX_SHORT_RETRY_SECONDS == 120
    assert EVENT_STREAM_HEALTHY_SECONDS == 60


@pytest.mark.parametrize(
    "short_streams, seconds",
    [(1, 10), (2, 10), (3, 10), (4, 20), (5, 40), (6, 80), (7, 120), (8, 120)],
)
def test_short_streams_in_a_row_double_the_wait_after_three(short_streams, seconds):
    delay = event_stream_retry_delay(30.0, 0, True, short_streams)

    assert delay == _about(seconds)


def test_the_wait_never_passes_its_ceiling():
    for short_streams in (9, 50, 10_000):
        delay = event_stream_retry_delay(30.0, 0, True, short_streams)
        assert delay <= EVENT_STREAM_MAX_SHORT_RETRY_SECONDS * (1 + EVENT_STREAM_JITTER)


def test_a_caller_that_does_not_count_keeps_ten_seconds():
    """The doorbell's DHIP listener shares the function and does not count."""
    assert event_stream_retry_delay(30.0, 0, True) == _about(10)


def test_a_stream_that_lasted_still_reconnects_at_once():
    assert event_stream_retry_delay(EVENT_STREAM_HEALTHY_SECONDS, 0, True, 50) == 0.0


def test_a_silent_attach_is_judged_as_before():
    """The instant-death backoff is untouched by the count."""
    assert event_stream_retry_delay(0.0, 1, False, 50) == _about(60)


# --- the loop counts --------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class _Attaches:
    """Each attach talks (or not) for a scripted time, then the device hangs up.

    Running out of script raises CancelledError, which `_async_run` re-raises.
    """

    def __init__(self, clock, *script):
        self.clock = clock
        self.script = list(script)

    async def stream_events(self, on_receive, events, channel):
        if not self.script:
            raise asyncio.CancelledError
        talks, seconds = self.script.pop(0)
        if talks:
            on_receive(b"Heartbeat", 0)
        self.clock.now += seconds
        raise ConnectionResetError("the peer hung up")


def _stream(client):
    stream = object.__new__(DahuaHostEventStream)
    stream._address = ADDRESS
    stream._events = frozenset({"VideoMotion"})
    stream._received_data = False
    stream._failing = False
    stream._consecutive_failures = 0
    stream._by_channel = {}
    stream._owner = SimpleNamespace(client=client)
    return stream


@pytest.fixture
def run(monkeypatch):
    """Run the loop over a script; return what the real delay was told and said."""

    async def _run(*script):
        clock = _Clock()
        monkeypatch.setattr(host_module, "time", clock)
        told = []
        real = event_stream_retry_delay

        def delay(*args):
            answer = real(*args)
            told.append((args[3], answer))
            return 0

        monkeypatch.setattr(host_module, "event_stream_retry_delay", delay)
        stream = _stream(_Attaches(clock, *script))

        async def nothing():
            return None

        stream._async_reconcile_latched_events = nothing
        stream._async_reconcile_alarm_inputs = nothing
        with pytest.raises(asyncio.CancelledError):
            await stream._async_run()
        return stream, told

    return _run


TALKS_8S = (True, 8)
TALKS_30S = (True, 30)
TALKS_2H = (True, 7200)
SILENT = (False, 5)


async def test_a_camera_hanging_up_every_thirty_seconds_is_backed_off(run):
    """#1235's shape: hours of a normal stream, then six that each talked
    for thirty seconds."""
    stream, told = await run(TALKS_2H, *[TALKS_30S] * 6)

    assert [count for count, _ in told] == [0, 1, 2, 3, 4, 5, 6]
    assert [answer for _, answer in told] == [
        0.0,
        *[_about(s) for s in (10, 10, 10, 20, 40, 80)],
    ]
    assert stream._consecutive_short_streams == 6


async def test_one_stream_that_lasts_starts_the_count_again(run):
    stream, told = await run(TALKS_2H, *[TALKS_30S] * 5, TALKS_2H, TALKS_30S)

    assert [count for count, _ in told] == [0, 1, 2, 3, 4, 5, 0, 1]
    assert told[-1][1] == _about(10)


async def test_a_silent_attach_does_not_move_the_count(run):
    """Neither up nor back to zero: it says nothing about how long a talking
    stream would last."""
    stream, told = await run(TALKS_2H, *[TALKS_30S] * 4, SILENT, SILENT, TALKS_30S)

    assert [count for count, _ in told] == [0, 1, 2, 3, 4, 4, 4, 5]


async def test_the_backoff_is_said_once(run, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.dahua")

    await run(TALKS_2H, *[TALKS_30S] * 7)

    said = [
        r.getMessage()
        for r in caplog.records
        if "waiting longer between reconnects" in r.getMessage()
    ]
    assert len(said) == 1
    assert ADDRESS in said[0]


async def test_firmware_that_never_holds_a_stream_keeps_ten_seconds(run):
    """#645's case: a camera that hangs up every stream after eight seconds
    while it is working. Backing it off would leave it unwatched most of the
    time, so it is never counted."""
    stream, told = await run(*[TALKS_8S] * 12)

    assert [count for count, _ in told] == [0] * 12
    assert [answer for _, answer in told] == [_about(10)] * 12
    assert stream._held_a_stream is False


async def test_counting_starts_only_once_a_stream_has_lasted(run):
    stream, told = await run(*[TALKS_30S] * 5, TALKS_2H, *[TALKS_30S] * 4)

    assert [count for count, _ in told] == [0, 0, 0, 0, 0, 0, 1, 2, 3, 4]
    assert stream._held_a_stream is True
    assert told[-1][1] == _about(20)


# --- the dump shows it ------------------------------------------------------------


@pytest.fixture
def _clean_streams():
    dahua._HOST_STREAMS.clear()
    yield
    dahua._HOST_STREAMS.clear()


def test_the_diagnostics_carry_the_count(_clean_streams):
    stream = SimpleNamespace(
        _task=None,
        _events=frozenset({"VideoMotion"}),
        _received_data=True,
        _failing=False,
        _consecutive_failures=0,
        _consecutive_short_streams=5,
        _held_a_stream=True,
        _owner=None,
        _by_channel={},
    )
    dahua._HOST_STREAMS[ADDRESS] = stream
    coordinator = SimpleNamespace(_address=ADDRESS)

    block = _stream_block(coordinator)

    assert block["short_streams_in_a_row"] == 5
    assert block["has_held_a_stream"] is True
