"""A doorbell that refuses port 5000 still has a CGI event stream.

A doorbell is the one device that never joins the shared CGI event stream: it
takes the DHIP listener on port 5000 and nothing else. On firmware that does
not open that port, it therefore has no event transport at all, and selecting
DoorbellPressed does nothing.

Measured on one model, a DH-DB6I, on two firmware versions, which is what makes
this a fallback rather than a second stream:

* `1.000.020000000.9.R`: port 5000 open, presses arrive over DHIP
  (@phabibjr, #1131).
* `2.820.010000000.33.R`: port 5000 refused, and the CGI attach carries the
  press as `_DoTalkAction_` Invite and `PhoneCallDetect`
  (@growlocally, #1187).

So a doorbell that runs both would report one press twice on the firmware where
both work. The loop returns instead, and that is what most of this file pins:
not only that the CGI stream is joined, but that the DHIP retry stops, and that
only a refused port causes either.

The connection is scripted by replacing `create_connection` on the running
loop, the same way `test_the_doorbell_stream_retry.py` does, because patching
`asyncio.get_event_loop` instead is global for the duration (#857).
"""

import asyncio

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua import diagnostics as diagnostics_module

ADDRESS = "10.0.0.241"


class _Protocol:
    """What the loop reads off DahuaVTOClient, and nothing else."""

    def __init__(self):
        self.received_data = True
        self.login_refused = None
        self.disconnected = asyncio.get_running_loop().create_future()
        self.disconnected.set_result(None)


class _Attempts:
    """Scripts each connection attempt, then ends the loop.

    The loop is a `while True` and re-raises CancelledError untouched, which is
    how Home Assistant stops it in production, so running out of script raises
    that. A test whose subject is the loop *returning* must therefore not see
    CancelledError, and that is an assertion in itself.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.attempts = 0

    async def create_connection(self, factory, host=None, port=None):
        self.attempts += 1
        if not self.script:
            raise asyncio.CancelledError
        failure = self.script.pop(0)
        if failure is not None:
            raise failure
        return None, _Protocol()


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


@pytest.fixture
def registered(monkeypatch):
    """Who joined the shared CGI stream.

    `async_start_event_listener` is left real, so what is asserted is that the
    switch reaches the registration rather than that a stand-in was called.
    """
    joined = []
    monkeypatch.setattr(
        coordinator_module,
        "_host_stream",
        lambda *args, **kwargs: type(
            "S", (), {"register": staticmethod(joined.append)}
        )(),
    )
    return joined


@pytest.fixture
def retries(monkeypatch):
    """Record what the delay function is told, and never actually wait.

    A real delay here is a minute at the short end, past the suite's nine
    second timeout. An empty list means the loop never reached the retry
    arithmetic, which is how "it stopped" is told from "it went round again".
    """
    asked = []

    def fake(lived_seconds, consecutive_failures, received_data):
        asked.append(received_data)
        return 0, consecutive_failures + 1

    monkeypatch.setattr(coordinator_module, "vto_retry_state", fake)
    return asked


def _coordinator(over_dhip=False, events=("DoorbellPressed",)):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._address = ADDRESS
    c._username = "u"
    c._password = "p"
    c._vto_client = None
    c.on_receive_vto_event = lambda event: None
    c._rpc2_over_dhip = over_dhip
    c.events = list(events) if events is not None else None
    c._device_class = "VTH" if over_dhip else "DB"
    c.hass = object()

    class _Client:
        def vth_own_video(self):
            return True

    c.client = _Client()
    return c


def _warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("custom_components.dahua")
        and r.levelname in ("WARNING", "ERROR")
    ]


# --- the switch --------------------------------------------------------------


async def test_a_refused_port_5000_moves_the_doorbell_to_the_cgi_stream(
    connections, registered, retries
):
    """The bug in #1187: this doorbell had no event transport at all."""
    coordinator = _coordinator()
    connections(ConnectionRefusedError(111, "Connect call failed"))

    await coordinator._async_stream_vto_events()

    assert registered == [coordinator], "the CGI stream was not joined"


async def test_the_dhip_retry_stops_rather_than_running_beside_the_cgi_stream(
    connections, registered, retries
):
    """One transport at a time, by construction.

    Both codes this doorbell sends for a press are translated to
    DoorbellPressed, so a doorbell holding both transports on the firmware
    where 5000 does open would put one press on the bus twice. Nothing
    de-duplicates them, so the loop has to end.
    """
    attempts = connections(
        ConnectionRefusedError(111, "Connect call failed"),
        None,  # would connect, if the loop ever came back round
    )

    await _coordinator()._async_stream_vto_events()

    assert attempts.attempts == 1, "the doorbell kept retrying DHIP as well"
    assert retries == [], "the loop went round instead of handing over"


async def test_the_warning_says_which_transport_took_over(
    connections, registered, retries, caplog
):
    """The one line an owner has to find when their events move."""
    connections(ConnectionRefusedError(111, "Connect call failed"))

    with caplog.at_level("WARNING"):
        await _coordinator()._async_stream_vto_events()

    assert any(
        ADDRESS in message and "port 5000" in message and "CGI event stream" in message
        for message in _warnings(caplog)
    ), _warnings(caplog)


# --- what must not switch ----------------------------------------------------


async def test_a_doorbell_that_is_merely_unreachable_keeps_the_dhip_listener(
    connections, registered, retries
):
    """A reboot or a dropped network is not a closed port.

    This is the negative control that matters most: a working VTO that happens
    to be restarting when Home Assistant does must not be moved onto the CGI
    stream for the rest of the session.
    """
    connections(TimeoutError("timed out"))

    with pytest.raises(asyncio.CancelledError):
        await _coordinator()._async_stream_vto_events()

    assert registered == [], "an unreachable doorbell switched transports"
    assert retries == [False], "it did not back off and retry"


async def test_a_generic_os_error_keeps_the_dhip_listener(
    connections, registered, retries
):
    """`ConnectionRefusedError` derives from `OSError`, and the wider class is
    everything from no route to host to a bad address. Only the refusal means
    the port is shut."""
    connections(OSError("no route to host"))

    with pytest.raises(asyncio.CancelledError):
        await _coordinator()._async_stream_vto_events()

    assert registered == []
    assert retries == [False]


async def test_a_monitor_with_no_web_server_has_nothing_to_fall_back_to(
    connections, registered, retries
):
    """Its reads are all DHIP logins, so the CGI stream would fail for ever
    against a web server that does not exist (#949)."""
    connections(ConnectionRefusedError(111, "Connect call failed"))

    with pytest.raises(asyncio.CancelledError):
        await _coordinator(over_dhip=True)._async_stream_vto_events()

    assert registered == []
    assert retries == [False]


async def test_a_doorbell_with_no_codes_selected_keeps_retrying(
    connections, registered, retries
):
    """`async_start_event_listener` registers nothing without a selection, so
    switching would abandon one transport without gaining another."""
    connections(ConnectionRefusedError(111, "Connect call failed"))

    with pytest.raises(asyncio.CancelledError):
        await _coordinator(events=None)._async_stream_vto_events()

    assert registered == []
    assert retries == [False]


# --- the dump has to name the transport that is carrying events --------------


class _Stream:
    def __init__(self):
        self._by_channel = {0: ["a coordinator"]}
        self._task = None


@pytest.fixture
def host_streams(monkeypatch):
    """A carrying CGI stream for this address, cleared afterwards.

    `_HOST_STREAMS` is a module global, and the suite runs with `-n auto`, so a
    test that leaves an entry in it changes another file's answer.
    """
    from custom_components import dahua as dahua_module

    streams = {ADDRESS: _Stream()}
    monkeypatch.setattr(dahua_module, "_HOST_STREAMS", streams)
    return streams


def _dump_transport(coordinator):
    return diagnostics_module._transport(coordinator, {})


async def test_the_dump_names_the_cgi_stream_once_the_listener_has_ended(
    host_streams,
):
    """Otherwise the field describes the transport the device used to have,
    which is what it was added to stop doing (#813)."""
    coordinator = _coordinator()
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    coordinator._vto_task = finished

    assert _dump_transport(coordinator) == "cgi_stream"


async def test_a_listener_between_two_attempts_is_still_the_transport(host_streams):
    """It is sleeping before reconnecting, not finished."""
    coordinator = _coordinator()
    coordinator._vto_task = asyncio.get_running_loop().create_future()

    try:
        assert _dump_transport(coordinator) == "vto_listener"
    finally:
        coordinator._vto_task.cancel()


async def test_a_stand_in_task_is_not_read_as_finished(host_streams):
    """Much of the suite hands diagnostics a double whose `done()` answers
    something truthy that is not a bool. Reading that as finished would move
    every such device's reported transport, so only an explicit True counts."""
    coordinator = _coordinator()
    coordinator._vto_task = type("T", (), {"done": staticmethod(lambda: object())})()

    assert _dump_transport(coordinator) == "vto_listener"


async def test_a_task_that_cannot_be_asked_is_not_read_as_finished(host_streams):
    """A double with no `done` at all keeps the answer it had before."""
    coordinator = _coordinator()
    coordinator._vto_task = object()

    assert _dump_transport(coordinator) == "vto_listener"
