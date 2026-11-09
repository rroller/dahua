"""A monitor that serves no HTTP cannot take the CGI event stream.

#949 added indoor monitors that have no web server at all and are read over
DHIP on port 5000. Their events were not moved with them: the coordinator sent
every non-doorbell to the shared CGI stream, which attaches to
`eventManager.cgi` on the HTTP port. On a device with nothing listening there,
that is not a 404, it is a refused connection, and the fallback inside the
stream is chosen on an HTTP status:

    EVENT_CGI_ABSENT = (404, 501)

so the RPC2 poll written for the SL300 (#780) is never reached. Measured on a
live VTH5221D after the update to 2.0.0-rc.4:

    transport              cgi_stream
    last_attach_failed     true
    consecutive_failures   11
    received_data          false
    rpc2_poll.used         false

    Event stream for 192.168.0.230 ended unexpectedly:
      Cannot connect to host 192.168.0.230:80

**What the device does support**, measured on the same monitor on
3.000.0012000.0.R over DHIP:

    eventManager.attach {"codes": ["All"]}    -> result true, SID 513
    eventManager.getEventIndexes {"code": X}  -> refused, 268959743,
                                                 for all nine selected codes

and then, unprompted on the same socket:

    {"method": "client.notifyEventStream",
     "params": {"SID": 513, "eventList": [{"Action": "Start",
                "Code": "DisplaySplit", ...}]}}

So the poller cannot work on it and the push can, and the push is the shape
`vto.py` already reads. This device takes that listener.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua import host as host_module
from custom_components.dahua.diagnostics import _transport

ADDRESS = "10.0.0.230"


@pytest.fixture(autouse=True)
def _clean_host_state():
    host_module._HOST_FAILURES.clear()
    yield
    host_module._HOST_FAILURES.clear()


@pytest.fixture
def registered(monkeypatch):
    """Who joined the shared CGI stream, which is what this must stop doing."""
    joined = []
    # *args, because what `_host_stream` is called with is not this test's
    # subject and has changed: #1085 adds the port, so a fixed signature here
    # would turn that merge into a TypeError in a test about something else.
    monkeypatch.setattr(
        coordinator_module,
        "_host_stream",
        lambda *args, **kwargs: type(
            "S", (), {"register": staticmethod(joined.append)}
        )(),
    )
    return joined


def _coordinator(
    over_dhip=False, doorbell=False, own_video=True, events=("VideoMotion",)
):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._address = ADDRESS
    c._rpc2_over_dhip = over_dhip
    c.events = list(events) if events is not None else None
    c._device_class = "VTH" if over_dhip else "IPC"
    c.hass = object()

    class _Client:
        def vth_own_video(self):
            return own_video

    c.client = _Client()
    c.is_doorbell = lambda: doorbell
    return c


async def _start(coordinator):
    """Run the listener start without opening a socket, and clean up after."""
    started = []

    async def _stream():
        started.append(True)

    coordinator._async_stream_vto_events = _stream
    await coordinator.async_start_event_listener()
    task = getattr(coordinator, "_vto_task", None)
    if task is not None:
        await task
    return started


# --- which transport each kind of device gets --------------------------------


async def test_a_monitor_with_no_http_takes_the_dhip_listener(registered):
    started = await _start(_coordinator(over_dhip=True))

    assert started == [True], "no listener was started"
    assert registered == [], "joined the CGI stream it cannot connect to"


async def test_an_ordinary_camera_still_joins_the_cgi_stream(registered):
    """The negative control. Almost every device takes this path, and routing
    one of them to a DHIP socket on port 5000 would be far worse than the bug
    being fixed."""
    coordinator = _coordinator(over_dhip=False)

    started = await _start(coordinator)

    assert registered == [coordinator]
    assert started == []


async def test_a_monitor_with_no_camera_is_still_left_alone(registered):
    """The earlier gate wins. A VTH that says it has no camera has no camera
    events to subscribe to, so starting a socket for it would be a connection
    held open for an empty selection."""
    started = await _start(_coordinator(over_dhip=True, own_video=False))

    assert started == []
    assert registered == []


async def test_a_camera_with_no_events_selected_starts_nothing(registered):
    started = await _start(_coordinator(over_dhip=False, events=None))

    assert started == []
    assert registered == []


# --- the login budget, which is the thing that can lock the owner out --------


def test_the_monitor_s_listener_spends_the_dhip_transport_s_budget():
    """Three refusals in total, not three each.

    `async_record_host_auth_refusal` returns the worst any one source has
    reached and MAX_AUTH_REFUSALS is 3, so two source names on one device is
    six attempts before anything stops. This monitor enforces LockLoginTimes 5
    within LoginFailLockTime 300, so six would lock Home Assistant out of it,
    and the password typed into the reauth dialog is refused along with
    everything else (#729's shape).
    """
    monitor = _coordinator(over_dhip=True)
    host_module.async_record_host_auth_refusal(ADDRESS, "dhip transport")
    host_module.async_record_host_auth_refusal(ADDRESS, "dhip transport")

    assert monitor._note_vto_login_refusal("refused") is True


def test_a_doorbell_still_keeps_its_own_budget():
    """And the other half, so the test above is about sharing rather than about
    the number. A doorbell's other transport is CGI, which is not a DHIP login,
    so its listener counting separately is right and must not change."""
    doorbell = _coordinator(over_dhip=False, doorbell=True)
    host_module.async_record_host_auth_refusal(ADDRESS, "dhip transport")
    host_module.async_record_host_auth_refusal(ADDRESS, "dhip transport")

    assert doorbell._note_vto_login_refusal("refused") is False


def test_the_shared_budget_still_stops_on_its_own_three():
    """A monitor with nothing else spending reaches the limit by itself."""
    monitor = _coordinator(over_dhip=True)

    assert monitor._note_vto_login_refusal("one") is False
    assert monitor._note_vto_login_refusal("two") is False
    assert monitor._note_vto_login_refusal("three") is True


def test_the_source_name_is_the_transport_s_own():
    """Derived rather than asserted as a literal twice: the name only works if
    it is the one `_new_dhip_transport` records under."""
    import inspect

    from custom_components.dahua.client import DahuaClient

    source = inspect.getsource(DahuaClient._new_dhip_transport)

    assert (
        '"{0}"'.format(_coordinator(over_dhip=True)._dhip_listener_auth_source()).strip(
            '"'
        )
        in source
    )


# --- and the dump says which socket it is ------------------------------------


def test_the_dump_calls_it_a_dhip_listener_not_a_doorbell():
    monitor = _coordinator(over_dhip=True)
    monitor._vto_task = object()

    assert _transport(monitor, {}) == "dhip_listener"


def test_a_doorbell_is_still_a_vto_listener():
    doorbell = _coordinator(over_dhip=False, doorbell=True)
    doorbell._vto_task = object()

    assert _transport(doorbell, {}) == "vto_listener"


class _WhateverKey(dict):
    """A `_HOST_STREAMS` that answers with one stream for any key.

    What the streams are keyed by is not this test's subject and is being
    changed underneath it: #1085 moves it from the address to `address:port`.
    Keying a fixture dict by hand would make these two cases fail on that
    merge while saying nothing about the field they are about.
    """

    def __init__(self, stream):
        super().__init__()
        self._stream = stream

    def get(self, key, default=None):
        return self._stream


def _with_stream(monkeypatch, stream):
    import custom_components.dahua as dahua

    monkeypatch.setattr(dahua, "_HOST_STREAMS", _WhateverKey(stream))


def test_a_stream_object_nobody_joined_is_not_a_transport(monkeypatch):
    """The poll builds the host stream object every time it asks it whether
    motion is pushed, so the entry in `_HOST_STREAMS` exists on a device that
    never registered and never attached. Reporting that as `cgi_stream` names a
    transport that has done nothing, which is what #813 added this field to
    stop doing."""
    _with_stream(monkeypatch, type("S", (), {"_by_channel": {}, "_task": None})())

    assert _transport(_coordinator(), {}) is None


def test_a_stream_a_channel_did_join_still_reports_itself(monkeypatch):
    """The negative control: almost every device is here, and losing the name
    would be worse than the wrong name."""
    coordinator = _coordinator()
    _with_stream(
        monkeypatch,
        type("S", (), {"_by_channel": {0: [coordinator]}, "_task": None})(),
    )

    assert _transport(coordinator, {}) == "cgi_stream"


@pytest.mark.parametrize("events", [None, []])
async def test_a_dhip_monitor_with_no_selected_events_starts_no_listener(
    registered, events
):
    assert await _start(_coordinator(over_dhip=True, events=events)) == []
    assert registered == []


async def test_the_listener_checks_a_budget_already_spent_by_the_transport(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    for _ in range(3):
        host_module.async_record_host_auth_refusal(ADDRESS, "dhip transport")
    connect = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(asyncio.get_running_loop(), "create_connection", connect)
    monitor = _coordinator(over_dhip=True)
    await monitor._async_stream_vto_events()
    connect.assert_not_called()


async def test_the_transport_checks_a_budget_already_spent_by_the_listener(monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.dahua import dhip
    from custom_components.dahua.client import DahuaClient
    from custom_components.dahua.dhip import DhipLoginRefused

    client = DahuaClient.__new__(DahuaClient)
    client._rpc2_over_dhip = True
    client._address, client._username, client._password = ADDRESS, "u", "p"
    transport = client._new_dhip_transport()
    for _ in range(3):
        _coordinator(over_dhip=True)._note_vto_login_refusal("refused")
    connect = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(dhip.asyncio, "open_connection", connect)
    with pytest.raises(DhipLoginRefused):
        await transport.exchange({"method": "magicBox.getSerialNo"})
    connect.assert_not_called()
