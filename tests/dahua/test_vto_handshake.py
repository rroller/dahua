"""The doorbell's login handshake and event dispatch, driven end to end.

`DahuaVTOClient` is an `asyncio.Protocol`, so the whole thing can be driven without
Home Assistant, without a socket and without the device: give it a transport that
records what was written, and feed it the frames a VTO sends back through
`data_received`. That is what these do -- the real login handshake, from
`connection_made` to an event arriving on the callback.

It is worth having because of what a defect in here looks like from the outside. The
event connection is the only path a doorbell press takes, and every failure in this
module is caught and logged rather than raised, so a broken handshake does not read as
an error. It reads as a doorbell that has gone quiet, which is also what a doorbell
nobody pressed looks like. #526, cancel_call no longer working, sat as the most
commented open issue for exactly that reason.

vto.py was the lowest-covered module in the integration at 31%.
"""

import json

from custom_components.dahua.vto import DahuaVTOClient

HEADER_SIZE = 32

# Obviously fake. The real ones are never in a test, and the doorbell's T2UServer
# table carries cloud credentials, so its UUID here is invented too.
HOST = "10.0.0.7"
USERNAME = "admin"
PASSWORD = "not-a-real-password"


class _Transport:
    """Records what the client wrote, like the stub in test_cancel_call_button."""

    def __init__(self):
        self.written = []
        self.closing = False

    def is_closing(self):
        return self.closing

    def write(self, message):
        self.written.append(message)


def _frame(payload):
    """A reply as the device sends it: DHIP header, JSON, newline.

    The header only has to be the right length and binary; parse_response finds the
    JSON inside it either way.
    """
    return (b"\x00\x00\x00DHIP\x8c-\x96{\x08\x00\x00\x00{\x01\x00\x00\x00\x00\x00\x00"
            + json.dumps(payload).encode("utf-8") + b"\n")


def _sent(client):
    """Everything the client has written, decoded. Strips the outgoing DHIP header."""
    return [json.loads(message[HEADER_SIZE:].decode("utf-8"))
            for message in client.transport.written]


def _requests_for(client, method):
    return [message for message in _sent(client) if message.get("method") == method]


def _client(events=None):
    """The real client with a recording transport. Constructed rather than
    object.__new__'d, so __init__ is exercised too -- it needs a running loop for its
    disconnected future, which is why these tests are async."""
    client = DahuaVTOClient(HOST, USERNAME, PASSWORD, False,
                            events.append if events is not None else (lambda event: None))
    client.transport = _Transport()
    return client


def _finish(client):
    """Drop the scheduled keepalive so the loop does not close with it pending."""
    if client._keep_alive_handle is not None:
        client._keep_alive_handle.cancel()
        client._keep_alive_handle = None


def _do_handshake(client):
    """Take the client from connected to attached, the way the device does.

    Returns the id the device should quote on events, which is the attach request's.
    """
    client.connection_made(client.transport)

    pre_login = _requests_for(client, "global.login")[0]
    client.data_received(_frame({
        "id": pre_login["id"],
        "session": 1722306858,
        "error": {"code": 268632079, "message": "Component error: login challenge!"},
        "params": {"random": "1234567890", "realm": "Login to 00408C123456"},
    }))

    login = _requests_for(client, "global.login")[1]
    client.data_received(_frame({
        "id": login["id"],
        "session": 1722306858,
        "result": True,
        "params": {"keepAliveInterval": 60},
    }))

    return _requests_for(client, "eventManager.attach")[0]["id"]


# --- the handshake ----------------------------------------------------------

async def test_connecting_asks_for_the_login_challenge():
    """The first thing on the wire is a login with an empty password, which is what
    makes the device answer with the challenge."""
    client = _client()

    client.connection_made(client.transport)

    requests = _requests_for(client, "global.login")
    assert len(requests) == 1, "expected exactly the pre-login: %s" % (_sent(client),)
    assert requests[0]["params"]["password"] == ""
    assert requests[0]["params"]["userName"] == USERNAME
    _finish(client)


async def test_the_challenge_is_answered_with_a_hash_and_the_session_is_kept():
    """The device's challenge carries the random, the realm and a session id. All
    three have to be picked up: the session goes on every later request, and the other
    two go into the hash."""
    client = _client()
    client.connection_made(client.transport)
    pre_login = _requests_for(client, "global.login")[0]

    client.data_received(_frame({
        "id": pre_login["id"],
        "session": 1722306858,
        "error": {"code": 268632079, "message": "Component error: login challenge!"},
        "params": {"random": "1234567890", "realm": "Login to 00408C123456"},
    }))

    assert client.random == "1234567890"
    assert client.realm == "Login to 00408C123456"
    assert client.sessionId == 1722306858

    login = _requests_for(client, "global.login")[1]
    assert login["session"] == 1722306858, "the login did not quote the session"
    assert login["params"]["password"] == DahuaVTOClient._get_hashed_password(
        "1234567890", "Login to 00408C123456", USERNAME, PASSWORD)
    _finish(client)


async def test_an_error_that_is_not_the_challenge_does_not_start_a_login():
    """A refusal is not a challenge. Answering one with a login would be a second
    failed attempt, and these devices lock a source address out after a few."""
    client = _client()
    client.connection_made(client.transport)
    pre_login = _requests_for(client, "global.login")[0]

    client.data_received(_frame({
        "id": pre_login["id"],
        "error": {"code": 268632085, "message": "Component error: user or password not valid!"},
        "params": {},
    }))

    assert len(_requests_for(client, "global.login")) == 1, (
        "a second login went out after a refusal")
    _finish(client)


async def test_the_plaintext_password_is_never_written_to_the_wire():
    """The whole point of the challenge scheme. Checked over every byte the client
    wrote across the entire handshake, not just the login frame."""
    client = _client()

    _do_handshake(client)

    everything = b"".join(client.transport.written)
    assert PASSWORD.encode("utf-8") not in everything, (
        "the plaintext password was written to the socket")
    _finish(client)


async def test_a_successful_login_loads_the_details_and_attaches():
    """One reply carrying keepAliveInterval is what turns a logged-in socket into a
    working doorbell: four things get read and the event manager gets attached. If any
    of these stopped going out, the integration would look connected and report
    nothing."""
    client = _client()

    _do_handshake(client)

    methods = [message["method"] for message in _sent(client)]
    assert "eventManager.attach" in methods, "no event manager attach, so no events"
    assert "magicBox.getSoftwareVersion" in methods
    assert "magicBox.getDeviceType" in methods
    # Two getConfig calls: AccessControl for the hold time, T2UServer for the serial.
    tables = [message["params"]["name"]
              for message in _requests_for(client, "configManager.getConfig")]
    assert sorted(tables) == ["AccessControl", "T2UServer"], tables
    _finish(client)


async def test_the_keepalive_is_scheduled_five_seconds_inside_the_devices_interval():
    """The device says how long it will tolerate silence, and the client has to speak
    before that, not at it."""
    client = _client()

    _do_handshake(client)

    assert client.keep_alive_interval == 55, "60 second interval, asked at 55"
    assert client._keep_alive_handle is not None, "nothing scheduled, so the device "\
        "will drop the connection when the interval expires"
    _finish(client)


async def test_a_login_reply_without_an_interval_does_not_attach():
    """Defensive, and it matters: attaching without a keepalive would give a
    connection that works for a minute and then dies silently."""
    client = _client()
    client.connection_made(client.transport)
    pre_login = _requests_for(client, "global.login")[0]
    client.data_received(_frame({
        "id": pre_login["id"], "session": 1,
        "error": {"message": "Component error: login challenge!"},
        "params": {"random": "1", "realm": "r"},
    }))
    login = _requests_for(client, "global.login")[1]

    client.data_received(_frame({"id": login["id"], "params": {}}))

    assert _requests_for(client, "eventManager.attach") == []
    assert client._keep_alive_handle is None
    _finish(client)


# --- events reaching Home Assistant -----------------------------------------

async def test_an_event_reaches_the_callback():
    """The end of the whole path: a doorbell press arrives as a frame and comes out
    on the hook into Home Assistant."""
    events = []
    client = _client(events)
    attach_id = _do_handshake(client)

    client.data_received(_frame({
        "id": attach_id,
        "method": "client.notifyEventStream",
        "params": {"SID": 513, "eventList": [
            {"Action": "Pulse", "Code": "CallNoAnswered", "Index": 999}]},
    }))

    assert len(events) == 1, "the press did not reach Home Assistant"
    assert events[0]["Code"] == "CallNoAnswered"
    _finish(client)


async def test_two_events_in_one_frame_both_reach_the_callback():
    """The device batches, and the eventList is a list for a reason."""
    events = []
    client = _client(events)
    attach_id = _do_handshake(client)

    client.data_received(_frame({
        "id": attach_id,
        "method": "client.notifyEventStream",
        "params": {"SID": 513, "eventList": [
            {"Action": "Start", "Code": "VideoMotion"},
            {"Action": "Pulse", "Code": "CallNoAnswered"}]},
    }))

    assert [event["Code"] for event in events] == ["VideoMotion", "CallNoAnswered"]
    _finish(client)


async def test_an_event_carries_the_device_identity_but_not_the_firmware():
    """handle_notify_event_stream copies details onto each event, filtered by
    DAHUA_ALLOWED_DETAILS. The identity fields are wanted downstream; version and
    build date are not on that list and must not be pasted onto every event."""
    events = []
    client = _client(events)
    attach_id = _do_handshake(client)
    client.dahua_details = {
        "deviceType": "VTO2211G-WP", "serialNumber": "not-a-real-serial",
        "version": "4.300.0000000.0", "buildDate": "2021-01-01",
    }

    client.data_received(_frame({
        "id": attach_id, "method": "client.notifyEventStream",
        "params": {"eventList": [{"Code": "CallNoAnswered"}]},
    }))

    assert events[0]["deviceType"] == "VTO2211G-WP"
    assert events[0]["serialNumber"] == "not-a-real-serial"
    assert "version" not in events[0], "the firmware version was pasted onto an event"
    assert "buildDate" not in events[0]
    _finish(client)


async def test_a_message_on_the_attach_handler_that_is_not_an_event_is_ignored():
    """The attach reply itself comes back on this handler. Treating it as an event
    would put a message with no eventList through the event path."""
    events = []
    client = _client(events)
    attach_id = _do_handshake(client)

    client.data_received(_frame({"id": attach_id, "result": True, "params": {}}))

    assert events == []
    _finish(client)


async def test_a_reply_nobody_is_waiting_for_does_not_raise():
    """An id with no handler falls through to handle_default. This has to hold: the
    dispatch loop runs for every frame, and an exception here would be swallowed and
    the rest of the batch lost."""
    events = []
    client = _client(events)
    _do_handshake(client)

    client.data_received(_frame({"id": 99999, "result": True}))

    assert events == []
    _finish(client)


# --- the buffer -------------------------------------------------------------

async def test_a_frame_split_across_two_reads_is_reassembled():
    """TCP does not promise frame boundaries. The buffer exists for this, and the
    event has to arrive once the rest turns up -- not be dropped, and not arrive
    twice."""
    events = []
    client = _client(events)
    attach_id = _do_handshake(client)
    whole = _frame({
        "id": attach_id, "method": "client.notifyEventStream",
        "params": {"eventList": [{"Code": "CallNoAnswered"}]},
    })
    half = len(whole) // 2

    client.data_received(whole[:half])
    assert events == [], "dispatched a half-read frame"

    client.data_received(whole[half:])

    assert len(events) == 1, "the event did not survive being split"
    _finish(client)


async def test_two_frames_in_one_read_are_both_processed():
    """The while loop over the newline. Two presses in one read must both arrive."""
    events = []
    client = _client(events)
    attach_id = _do_handshake(client)
    event = {"id": attach_id, "method": "client.notifyEventStream",
             "params": {"eventList": [{"Code": "CallNoAnswered"}]}}

    client.data_received(_frame(event) + _frame(event))

    assert len(events) == 2
    _finish(client)


async def test_a_read_with_no_newline_yet_is_held_not_dropped():
    events = []
    client = _client(events)
    _do_handshake(client)

    client.data_received(b'{"id": 1, "partial": ')

    assert bytes(client.buffer).endswith(b'"partial": ')
    _finish(client)


async def test_anything_the_device_says_counts_as_having_heard_from_it():
    """`received_data` is what tells a doorbell that is refusing us from one that is
    simply quiet, and most front doors are quiet for hours. It has to be set by any
    traffic at all, including a frame that parses to nothing."""
    client = _client()
    assert client.received_data is False

    client.data_received(b"not even json\n")

    assert client.received_data is True
    _finish(client)


# --- what the loaders read --------------------------------------------------

async def test_the_hold_time_comes_from_the_local_access_protocol_only():
    """The AccessControl table has a row per protocol and only the local one applies.
    Reading the wrong row would give the door the wrong relock delay."""
    client = _client()
    _do_handshake(client)
    access = [message for message in _requests_for(client, "configManager.getConfig")
              if message["params"]["name"] == "AccessControl"][0]

    client.data_received(_frame({"id": access["id"], "params": {"table": [
        {"AccessProtocol": "Remote", "UnlockReloadInterval": 99},
        {"AccessProtocol": "Local", "UnlockReloadInterval": 3},
    ]}}))

    assert client.hold_time == 3, "the hold time was read off the wrong protocol"
    _finish(client)


async def test_the_version_and_type_and_serial_are_recorded():
    """These are what the event path then attaches to each event, and what the device
    registry shows."""
    client = _client()
    _do_handshake(client)
    sent = _sent(client)
    version = [m for m in sent if m["method"] == "magicBox.getSoftwareVersion"][0]
    device_type = [m for m in sent if m["method"] == "magicBox.getDeviceType"][0]
    t2u = [m for m in _requests_for(client, "configManager.getConfig")
           if m["params"]["name"] == "T2UServer"][0]

    client.data_received(_frame({"id": version["id"], "params": {"version": {
        "Version": "4.300.0000000.0", "BuildDate": "2021-01-01"}}}))
    client.data_received(_frame({"id": device_type["id"],
                                 "params": {"type": "VTO2211G-WP"}}))
    client.data_received(_frame({"id": t2u["id"], "params": {
        "table": {"UUID": "not-a-real-serial"}}}))

    assert client.dahua_details == {
        "version": "4.300.0000000.0", "buildDate": "2021-01-01",
        "deviceType": "VTO2211G-WP", "serialNumber": "not-a-real-serial",
    }
    _finish(client)


async def test_a_version_reply_without_a_version_does_not_raise():
    """The getters default rather than index, and a device that answers oddly must
    not take the handshake down with it."""
    client = _client()
    _do_handshake(client)
    version = [m for m in _sent(client)
               if m["method"] == "magicBox.getSoftwareVersion"][0]

    client.data_received(_frame({"id": version["id"], "params": {}}))

    assert client.dahua_details["version"] is None
    _finish(client)


# --- keeping the connection, and losing it ----------------------------------

async def test_the_keepalive_handler_does_not_leak_a_handler_per_beat():
    """Every keepalive registers a handler under a fresh request id, and the handler
    removes its own. On a connection that lives for weeks at one beat every 55
    seconds, not removing it is an unbounded dict."""
    client = _client()
    _do_handshake(client)
    before = len(client.data_handlers)

    for _ in range(5):
        client._keep_alive_handle.cancel()
        client.keep_alive()
        beat = _requests_for(client, "global.keepAlive")[-1]
        client.data_received(_frame({"id": beat["id"], "params": {}}))

    assert len(client.data_handlers) == before, (
        "handlers grew from %d to %d over five beats"
        % (before, len(client.data_handlers)))
    _finish(client)


async def test_the_keepalive_reschedules_itself():
    """If the reply did not schedule the next one the connection goes quiet after a
    single beat."""
    client = _client()
    _do_handshake(client)
    client._keep_alive_handle.cancel()
    client.keep_alive()
    beat = _requests_for(client, "global.keepAlive")[-1]

    client.data_received(_frame({"id": beat["id"], "params": {}}))

    assert client._keep_alive_handle is not None
    _finish(client)


async def test_eof_cancels_the_keepalive_and_reports_the_disconnect():
    """`disconnected` is what the reconnect loop waits on, and a keepalive still
    scheduled after the socket is gone fires into nothing."""
    client = _client()
    _do_handshake(client)
    assert client._keep_alive_handle is not None

    client.eof_received()

    assert client._keep_alive_handle is None
    assert client.disconnected.done()


async def test_connection_lost_does_the_same():
    client = _client()
    _do_handshake(client)

    client.connection_lost(OSError("connection reset by peer"))

    assert client._keep_alive_handle is None
    assert client.disconnected.done()


async def test_losing_the_connection_twice_does_not_raise():
    """eof_received and connection_lost both fire on an ordinary close, so the future
    is already resolved the second time. Setting it again would raise InvalidStateError
    inside the transport callback."""
    client = _client()
    _do_handshake(client)

    client.eof_received()
    client.connection_lost(None)

    assert client.disconnected.done()


# --- not writing to a socket that has gone ----------------------------------

async def test_nothing_is_written_to_a_closing_transport():
    """send checks is_closing first. Writing to a closed transport raises, and that
    exception would surface inside whichever handler happened to call send."""
    client = _client()
    _do_handshake(client)
    client.transport.closing = True
    before = len(client.transport.written)

    request_id = client.send("global.keepAlive", lambda message: None)

    assert len(client.transport.written) == before, "wrote to a closing transport"
    # The handler is still registered and the id still returned, so a caller waiting
    # on the reply times out rather than waiting for ever on a request never sent.
    assert request_id in client.data_handlers
    _finish(client)


# --- the parts that must not take the connection down -----------------------
#
# Every failure in this module is caught and logged rather than raised. That is
# deliberate, because this runs on a transport callback where an exception reaches
# asyncio rather than any caller -- but it also means these branches are exactly the
# ones that turn a fault into a doorbell that has gone quiet. Worth pinning on
# purpose, rather than covering by accident.

async def test_a_home_assistant_callback_that_raises_does_not_stop_later_events():
    """`on_receive_vto_event` is a hook into Home Assistant, so it can raise for
    reasons that have nothing to do with the doorbell. If that killed the event path,
    one bad event would silence the device until the next reconnect."""
    seen = []

    def explode(event):
        seen.append(event)
        raise RuntimeError("something in Home Assistant went wrong")

    client = DahuaVTOClient(HOST, USERNAME, PASSWORD, False, explode)
    client.transport = _Transport()
    attach_id = _do_handshake(client)
    event = {"id": attach_id, "method": "client.notifyEventStream",
             "params": {"eventList": [{"Code": "CallNoAnswered"}]}}

    client.data_received(_frame(event))
    client.data_received(_frame(event))

    assert len(seen) == 2, "the second press never arrived"
    _finish(client)


async def test_an_event_frame_with_no_event_list_does_not_raise():
    """A malformed frame from the device, which is not hypothetical on this hardware.
    Iterating None would raise inside the transport callback."""
    events = []
    client = _client(events)
    attach_id = _do_handshake(client)

    client.data_received(_frame({"id": attach_id,
                                 "method": "client.notifyEventStream",
                                 "params": {"SID": 513}}))

    assert events == []
    _finish(client)


async def test_one_bad_frame_does_not_stop_the_next_one():
    """The try sits inside the loop over frames, so a handler that raises costs the
    rest of its own packet but not the packets behind it. Two presses arriving in
    separate frames, the first handled by something that throws."""
    events = []
    client = _client(events)
    attach_id = _do_handshake(client)
    bad_id = client.send("global.keepAlive", lambda message: 1 / 0)
    event = _frame({"id": attach_id, "method": "client.notifyEventStream",
                    "params": {"eventList": [{"Code": "CallNoAnswered"}]}})

    client.data_received(_frame({"id": bad_id, "params": {}}) + event)

    assert len(events) == 1, "the good frame behind the bad one was lost"
    _finish(client)


async def test_a_handler_handed_nothing_returns_quietly():
    """Each handler opens with a None guard. Nothing in data_received passes None
    today -- it skips them -- so these are defensive, and this is what says they
    behave if that ever changes, rather than raising AttributeError on .get."""
    client = _client()
    _do_handshake(client)

    # Every handler registered across the whole handshake, including the loaders.
    assert len(client.data_handlers) >= 6, "the handshake registered almost nothing"
    for handler in list(client.data_handlers.values()):
        handler(None)

    _finish(client)


async def test_the_keepalive_reschedules_even_when_it_cannot_find_its_own_handler():
    """The handler removes itself by message id, and warns if that id is not
    registered. What matters is that it reschedules anyway -- the next beat is set up
    before that check, not after it -- because a keepalive that stops is a connection
    the device drops.

    The handler is called directly here on purpose. Going through data_received with
    an unknown id would dispatch to handle_default instead, so the assertion would
    hold without handle_keep_alive ever running.
    """
    client = _client()
    _do_handshake(client)
    client._keep_alive_handle.cancel()
    client.keep_alive()
    beat_id = _requests_for(client, "global.keepAlive")[-1]["id"]
    handler = client.data_handlers[beat_id]
    client._keep_alive_handle = None

    handler({"id": 424242})

    assert client._keep_alive_handle is not None, (
        "an unrecognised keepalive reply stopped the keepalive")
    _finish(client)


async def test_the_keepalive_handler_handed_nothing_still_reschedules():
    """Same reason: the reschedule happens before the None guard, so a reply that
    arrives as nothing does not silently end the keepalive."""
    client = _client()
    _do_handshake(client)
    client._keep_alive_handle.cancel()
    client.keep_alive()
    handler = client.data_handlers[_requests_for(client, "global.keepAlive")[-1]["id"]]
    client._keep_alive_handle = None

    handler(None)

    assert client._keep_alive_handle is not None
    _finish(client)


async def test_a_transport_that_is_already_gone_does_not_raise_on_connect():
    """connection_made wraps pre_login for this. A transport that fails the moment it
    is used would otherwise raise inside asyncio's own callback."""

    class _DeadTransport(_Transport):
        def is_closing(self):
            raise OSError("transport is gone")

    client = _client()
    client.transport = _DeadTransport()

    client.connection_made(client.transport)

    assert client.transport.written == []
    _finish(client)
