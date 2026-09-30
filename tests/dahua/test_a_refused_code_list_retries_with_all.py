"""A device that refuses a long event-code list is asked a different way.

Two reporters, two different cameras, one shape. On #728 an IPC-HFW4300S-V2 running
2014 firmware answers `codes=[VideoMotion]` with 200 and opens the stream, and answers
the nine-code list with **400**. On #832 a Hero A1 answers the same nine with **500**.
Both serve the event stream perfectly. Neither will take the list.

The integration already knew this could happen and already knows what to do about it.
`host.py` subscribes with `codes=[All]` and filters locally when it decides the request
has grown too long, and the local filter means the user's selection is unaffected. What
it got wrong was *when* to decide:

    use_all_events = len(wanted) > max(len(c.events) for c in self.coordinators)

That asks whether sharing one stream across channels made the request longer than any
single channel's own list. On a single camera the union of one list **is** that list, so
it is never longer, so `[All]` was never used. Single cameras are exactly the devices old
enough to refuse one, which is why #728 reads as "everyone with one camera" rather than
as a firmware quirk.

So the device's refusal is now the signal, rather than a guess about what it might
refuse. Only 404 and 501 ever meant "no CGI event path"; every other status was retried
for ever with the same list that had just failed.
"""

import asyncio

import pytest
from aiohttp import ClientResponseError
from types import SimpleNamespace

from custom_components.dahua.host import DahuaHostEventStream

ADDRESS = "10.0.0.5"
NINE_CODES = frozenset({
    "AlarmLocal", "AudioMutation", "CrossLineDetection", "CrossRegionDetection",
    "SmartMotionHuman", "SmartMotionVehicle", "VideoBlind", "VideoLoss",
    "VideoMotion",
})


def _refused(status):
    return ClientResponseError(
        request_info=SimpleNamespace(real_url="http://%s/x" % ADDRESS),
        history=(), status=status)


class _Attaches:
    """Records what each attach asked for, and answers as the script says.

    Running out of script raises CancelledError, which `_async_run` re-raises
    untouched. That is how Home Assistant stops it in production too.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.asked = []

    async def stream_events(self, on_receive, events, channel):
        self.asked.append(list(events))
        if not self.script:
            raise asyncio.CancelledError
        answer = self.script.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if answer == "talks":
            # Delivers, then ends. A live stream would stay open, but the loop
            # only moves on when an attach finishes, so holding it open here
            # means the test never reaches its next scripted attach.
            on_receive(b"Heartbeat", 0)
        return


def _stream(client, events=NINE_CODES, **kwargs):
    stream = object.__new__(DahuaHostEventStream)
    stream._address = ADDRESS
    stream._events = frozenset(events)
    stream._received_data = False
    stream._failing = False
    stream._consecutive_failures = 0
    stream._by_channel = {}
    stream._owner = type("_Owner", (), {"client": client})()
    for name, value in kwargs.items():
        setattr(stream, name, value)
    return stream


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """The retry delay is a minute at the short end; the suite times out at nine."""
    from custom_components.dahua import host as host_module

    monkeypatch.setattr(host_module, "event_stream_retry_delay",
                        lambda *args, **kwargs: 0)


async def _run(stream):
    with pytest.raises(asyncio.CancelledError):
        await stream._async_run()


# --- the reported failure ---------------------------------------------------

@pytest.mark.parametrize("status", [400, 500])
async def test_a_refused_list_is_retried_as_all(status):
    """#728 answers 400 and #832 answers 500. Both mean the same thing here: the
    device will not take this list."""
    client = _Attaches(_refused(status), "talks")

    await _run(_stream(client))

    assert client.asked[0] == sorted(NINE_CODES)
    assert client.asked[1] == ["All"], client.asked


async def test_the_retry_is_immediate():
    """Straight back round rather than through the backoff. This is not a device in
    trouble, it is one that wants the request put a different way and has said so,
    so making the user wait a minute for it would be a minute of no events."""
    client = _Attaches(_refused(400), "talks")

    await _run(_stream(client))

    assert len(client.asked) == 2


async def test_it_says_what_it_did_and_why(caplog):
    """A silent workaround is one nobody can confirm. The line names the status and
    says the selection is unaffected, because "subscribing to all events" reads
    like it will."""
    client = _Attaches(_refused(400), "talks")

    await _run(_stream(client))

    said = [r.getMessage() for r in caplog.records
            if r.name.startswith("custom_components.dahua")
            and r.levelname == "WARNING"]
    assert said, "the workaround was silent"
    assert "400" in said[0]
    assert ADDRESS in said[0]


async def test_it_is_tried_once_and_not_again():
    """A device that refuses the list and then refuses [All] has nothing left to
    offer. Retrying both for ever would double the requests at a device already
    saying no."""
    client = _Attaches(_refused(400), _refused(400), _refused(400))

    await _run(_stream(client))

    assert client.asked[0] == sorted(NINE_CODES)
    assert client.asked[1] == ["All"]
    assert client.asked[2] == ["All"], "it went back to the list that failed"


# --- and what it must not do ------------------------------------------------

async def test_a_stream_that_was_working_is_not_switched():
    """The socket ending after the device has been talking is not a refusal, and
    broadening a subscription that works would be a change nobody asked for."""
    client = _Attaches("talks")
    stream = _stream(client)
    stream._received_data = True

    await _run(stream)

    assert client.asked == [sorted(NINE_CODES)] * 2


async def test_a_credential_refusal_is_left_to_its_own_budget():
    """A 401 is not a device declining the shape of the request. Retrying it as
    [All] would spend one of the three attempts the lockout budget allows."""
    client = _Attaches(_refused(401))
    stream = _stream(client)

    await _run(stream)

    assert all(asked != ["All"] for asked in client.asked), client.asked


async def test_a_stream_already_on_all_is_not_switched_again():
    client = _Attaches(_refused(400), "talks")
    stream = _stream(client, _using_all_events=True)

    await _run(stream)

    assert stream._tried_all_events is False


async def test_a_failure_with_no_status_is_not_a_refusal():
    """A dropped connection says nothing about what the device would accept, and
    treating it as a refusal would broaden the subscription on a bad network."""
    client = _Attaches(OSError("connection reset"), "talks")

    await _run(_stream(client))

    assert all(asked != ["All"] for asked in client.asked), client.asked


# --- and it has to outlive a channel change ---------------------------------

def test_what_the_device_said_survives_the_heuristic():
    """`_restart_if_needed` recomputes the old guess whenever channels change. On
    its own that would drop the stream back onto a list already known to fail, and
    the only sign would be the events stopping again."""
    client = _Attaches()
    stream = _stream(client, _tried_all_events=True, _task=None)
    stream.coordinators = [SimpleNamespace(events=list(NINE_CODES))]
    stream._hass = SimpleNamespace()
    stream._by_channel = {0: [SimpleNamespace(
        events=list(NINE_CODES), get_channel=lambda: 0)]}

    stream._restart_if_needed()

    assert stream._using_all_events is True
