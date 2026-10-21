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
    monkeypatch.setattr(
        coordinator_module,
        "_host_stream",
        lambda hass, address: type(
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


def test_a_stream_object_nobody_joined_is_not_a_transport():
    """The poll builds the host stream object every time it asks it whether
    motion is pushed, so the entry in `_HOST_STREAMS` exists on a device that
    never registered and never attached. Reporting that as `cgi_stream` names a
    transport that has done nothing, which is what #813 added this field to
    stop doing."""
    from custom_components.dahua import _HOST_STREAMS

    coordinator = _coordinator()
    _HOST_STREAMS[ADDRESS] = type("S", (), {"_by_channel": {}, "_task": None})()
    try:
        assert _transport(coordinator, {}) is None
    finally:
        _HOST_STREAMS.pop(ADDRESS, None)


def test_a_stream_a_channel_did_join_still_reports_itself():
    """The negative control: almost every device is here, and losing the name
    would be worse than the wrong name."""
    from custom_components.dahua import _HOST_STREAMS

    coordinator = _coordinator()
    _HOST_STREAMS[ADDRESS] = type(
        "S", (), {"_by_channel": {0: [coordinator]}, "_task": None}
    )()
    try:
        assert _transport(coordinator, {}) == "cgi_stream"
    finally:
        _HOST_STREAMS.pop(ADDRESS, None)
