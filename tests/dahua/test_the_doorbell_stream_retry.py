"""What the doorbell's event connection remembers between attempts.

`vto_retry_state` decides how long to wait before re-attaching, and
`test_vto_reconnect.py` covers it thoroughly as a function. The loop that calls it,
`_async_stream_vto_events`, had none of its thirty-seven statements executed, which is
the same split `host.py` had: the arithmetic was pinned and the bookkeeping feeding it
was not.

What the loop owes that function is one fact, and it is the fact the whole backoff turns
on: **did the device say anything**. A front door is quiet for hours, so silence cannot
mean refusal; the signal is anything at all, the keepAlive answer included. Get it
backwards and a working doorbell is backed off to one attempt every ten minutes, or an
unplugged one is contacted 2,880 times a day, which is the bug the function was written
to fix.

The two endings reach that fact by different roads, which is why both are tested:

* the connection never establishes, so there is no protocol to ask and the answer has to
  come from a `getattr` default rather than from an attribute on `None`;
* the connection establishes and later drops, so the protocol is there and is asked
  directly.

The connection is scripted by replacing `create_connection` on the running loop, which is
an attribute on that one object. Patching `asyncio.get_event_loop` instead would be
global for the duration, and that mistake once made a test count Home Assistant's own
sleeps as the integration's (#857).
"""

import asyncio

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module

ADDRESS = "10.0.0.232"

# What one attempt does. "refused" never connects; the other two connect and then
# drop, differing only in whether the device said anything first.
REFUSED = ("refused", None)
TALKED = ("connected", True)
SILENT = ("connected", False)


class _Protocol:
    """Stands in for DahuaVTOClient: what the loop reads off it and nothing else."""

    def __init__(self, received_data):
        self.received_data = received_data
        self.disconnected = asyncio.get_running_loop().create_future()
        # Already over: the loop awaits this to learn the socket has closed, and
        # every test here is about what happens next.
        self.disconnected.set_result(None)


class _Attempts:
    """Scripts each connection attempt, then ends the loop.

    `_async_stream_vto_events` is a `while True`, so something has to stop it. It
    re-raises CancelledError untouched, which is how Home Assistant stops it in
    production too, so running out of script raises that.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.attempts = 0

    async def create_connection(self, factory, host=None, port=None):
        self.attempts += 1
        if not self.script:
            raise asyncio.CancelledError
        kind, talked = self.script.pop(0)
        if kind == "refused":
            raise OSError("connection refused")
        return None, _Protocol(talked)


def _coordinator():
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = ADDRESS
    coordinator._username = "u"
    coordinator._password = "p"
    coordinator._vto_client = None
    coordinator.on_receive_vto_event = lambda event: None
    return coordinator


@pytest.fixture
def retries(monkeypatch):
    """Record what the delay function is told, and never actually wait.

    A real delay here is a minute at the short end, so the suite's nine second
    timeout would fire long before the second attempt.
    """

    class _Asked(list):
        """A list of what the delay function was told, plus the delay it
        should hand back, so a test can choose one."""

        delay = 0

    asked = _Asked()

    def fake(lived_seconds, consecutive_failures, received_data):
        asked.append(
            {
                "lived": lived_seconds,
                "failures": consecutive_failures,
                "received_data": received_data,
            }
        )
        # Reset on a device that spoke, climb otherwise, which is what the real
        # one does. A fake that only ever climbed would make the recovery test
        # assert the fake's behaviour rather than the loop's.
        return asked.delay, (0 if received_data else consecutive_failures + 1)

    monkeypatch.setattr(coordinator_module, "vto_retry_state", fake)
    return asked


@pytest.fixture
def connections(monkeypatch):
    """Replace create_connection on the running loop, scoped to that object."""

    def install(*script):
        attempts = _Attempts(*script)
        monkeypatch.setattr(
            asyncio.get_running_loop(), "create_connection", attempts.create_connection
        )
        return attempts

    return install


def _warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("custom_components.dahua")
        and r.levelname in ("WARNING", "ERROR")
    ]


async def _run(coordinator):
    with pytest.raises(asyncio.CancelledError):
        await coordinator._async_stream_vto_events()


# --- a doorbell that will not answer ----------------------------------------


async def test_a_refused_connection_reports_that_nothing_was_said(retries, connections):
    """There is no protocol to ask, so the answer comes from a `getattr` default.
    Reading the attribute off `None` would raise, and the raise would happen inside
    the handler that exists to keep this loop alive."""
    connections(REFUSED)

    await _run(_coordinator())

    assert [r["received_data"] for r in retries] == [False]


async def test_refusals_accumulate(retries, connections):
    """The count is what the backoff grows on, so it has to survive the iteration
    that produced it rather than resetting each time round."""
    connections(REFUSED, REFUSED, REFUSED)

    await _run(_coordinator())

    assert [r["failures"] for r in retries] == [0, 1, 2]


async def test_a_refusal_is_reported_with_its_cause(retries, connections, caplog):
    """ "The doorbell is unreachable" is not actionable. Which error it was
    separates a wrong password from a device that is switched off."""
    connections(REFUSED)

    await _run(_coordinator())

    said = _warnings(caplog)
    assert len(said) == 1
    assert ADDRESS in said[0]
    assert "connection refused" in said[0], said[0]


async def test_it_keeps_trying_after_a_refusal(retries, connections):
    """The backoff exists so that it can keep trying cheaply. Giving up would
    leave a doorbell that was briefly unplugged permanently dead."""
    attempts = connections(REFUSED, REFUSED)

    await _run(_coordinator())

    assert attempts.attempts == 3, "it stopped instead of re-attaching"


# --- a doorbell that connected and then dropped -----------------------------


async def test_a_connection_that_talked_says_so(retries, connections):
    """The distinction the whole backoff rests on. This device answered, so the
    socket ending is not a refusal and it must not be treated as one."""
    connections(TALKED)

    await _run(_coordinator())

    assert [r["received_data"] for r in retries] == [True]


async def test_a_connection_that_stayed_silent_says_that_too(retries, connections):
    """Connected but said nothing at all, not even the keepAlive answer. That is
    the shape of a device refusing at a level the socket does not show."""
    connections(SILENT)

    await _run(_coordinator())

    assert [r["received_data"] for r in retries] == [False]


async def test_a_talking_connection_clears_the_failure_count(retries, connections):
    """Recovery. Two refusals then a connection that talks, and the count handed
    to the next attempt is back to zero."""
    connections(REFUSED, REFUSED, TALKED, REFUSED)

    await _run(_coordinator())

    assert [r["failures"] for r in retries] == [0, 1, 2, 0]


async def test_the_client_is_published_once_it_connects(retries, connections):
    """`_vto_client` is what the cancel-call and open-door services reach for, and
    #859's `no_vto_connection_for_service` error is what a user sees when it is
    None. It has to be set on connect rather than on first event."""
    connections(TALKED)
    coordinator = _coordinator()

    await _run(coordinator)

    assert coordinator._vto_client is not None
    assert coordinator._vto_client.received_data is True


async def test_a_disconnect_is_reported(retries, connections, caplog):
    connections(TALKED)

    await _run(_coordinator())

    said = _warnings(caplog)
    assert len(said) == 1
    assert ADDRESS in said[0]
    assert "econnect" in said[0], said[0]


async def test_a_disconnect_with_no_wait_does_not_promise_one(
    retries, connections, caplog
):
    """Two spellings of the same line, one naming a delay and one not. Saying
    "reconnecting in 0s" is the kind of detail that sends somebody looking for a
    setting that does not exist."""
    connections(TALKED)

    await _run(_coordinator())

    assert "in 0s" not in _warnings(caplog)[0]


# --- and being shut down is not a failure -----------------------------------


async def test_cancellation_is_not_retried(retries, connections):
    """Cancellation is Home Assistant stopping the task. The broad handler beside
    it exists so a refused doorbell keeps trying, and it must not swallow this: a
    background task that ignores cancellation stops Home Assistant from stopping.
    """
    connections()

    await _run(_coordinator())

    assert retries == [], "a cancellation was counted as a failed attempt"


async def test_a_wait_before_reconnecting_is_named(retries, connections, caplog):
    """The other spelling of the disconnect line. A user reading "reconnecting"
    and waiting two minutes has no way to tell that from a hang, so when there is
    a delay it is said.

    One second rather than a realistic sixty, because the loop really sleeps it.
    The line is logged before the sleep, so the branch is exercised either way and
    a realistic delay would only spend the suite's timeout budget.
    """
    retries.delay = 1
    connections(SILENT)

    await _run(_coordinator())

    assert "in 1s" in _warnings(caplog)[0], _warnings(caplog)
