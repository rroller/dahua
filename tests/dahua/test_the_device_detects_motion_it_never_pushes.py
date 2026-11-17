"""Tell people the motion poller exists, when the device proves they need it.

#996 added an option that reads motion state from the device instead of
waiting to be pushed, for recorders and XVRs that send smart detection
normally and never send ordinary VideoMotion. @mediarath wrote it from their
own XVR. A second recorder turned up on #728 and the owner found the setting
only after being told.

Nothing in the interface connects "motion does not work at all" to a tickbox
in the entry's options, which is the gap this closes.

**The evidence has to be positive.** `getEventIndexes` answers identically for
a real code with nothing active and for a code it has never heard of, measured
on a DHI-NVR5464-16P-EI:

    code=VideoMotion          200  channels[0]=14
    code=CrossLineDetection   200  Error: No Events
    code=NotARealCodeAtAll    200  Error: No Events

So silence proves nothing and a timeout would be worse than useless: "no
motion for six hours" is what a camera pointed at a locked gate reports when
it is working perfectly. Only "the device says motion is happening right now
and we have never been told once" is a fault.
"""

import asyncio

import aiohttp
import pytest

from custom_components import dahua as dahua_module
from custom_components.dahua import _host_stream
from custom_components.dahua.host import (
    MOTION_PUSH_CHECK_INTERVAL_SECONDS,
    MOTION_PUSH_CHECK_LIMIT,
    DahuaHostEventStream,
)

ADDRESS = "10.0.0.9"
ISSUE_ID = "motion_not_pushed_" + ADDRESS


def motion(index=0, action="Start"):
    body = "Code=VideoMotion;action=%s;index=%d\r\n" % (action, index)
    return b"--myboundary\nContent-Type: text/plain\n\n" + body.encode()


@pytest.fixture(autouse=True)
def _clean_streams():
    dahua_module._HOST_STREAMS.clear()
    yield
    dahua_module._HOST_STREAMS.clear()


@pytest.fixture(autouse=True)
async def _stop_host_streams(hass):
    yield
    pending = []
    for stream in list(dahua_module._HOST_STREAMS.values()):
        for attribute in ("_task", "_video_motion_poll_task"):
            task = getattr(stream, attribute, None)
            if task is not None and not task.done():
                task.cancel()
                pending.append(task)
    dahua_module._HOST_STREAMS.clear()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class _Client:
    def __init__(self, active=(), raises=False, on_all=None):
        self.active = set(active)
        self.raises = raises
        self.asked = 0
        # Every code list the stream attached with, in order. What the device
        # was asked for is the observable for #1202, not a flag on the stream.
        self.attached_with = []
        # What this device does when asked for codes=[All]: None serves it,
        # "refuse" answers 400, "silent" attaches and sends nothing, and
        # "unauthorised" answers 401, which is credentials and not the request.
        self.on_all = on_all

    async def stream_events(self, on_receive, events, channel):
        self.attached_with.append(list(events))
        if list(events) == ["All"]:
            if self.on_all == "refuse":
                raise aiohttp.ClientResponseError(
                    None, (), status=400, message="Bad Request"
                )
            if self.on_all == "unauthorised":
                raise aiohttp.ClientResponseError(
                    None, (), status=401, message="Unauthorized"
                )
            if self.on_all == "silent":
                raise asyncio.TimeoutError()
            if self.on_all == "talks_then_drops":
                on_receive(b"--myboundary\nContent-Length:9\n\nHeartbeat\n", 0)
                raise aiohttp.ClientResponseError(
                    None, (), status=400, message="Bad Request"
                )
        await asyncio.Event().wait()

    async def async_get_event_indexes_cgi(self, code):
        assert code == "VideoMotion"
        self.asked += 1
        if self.raises:
            raise RuntimeError("the device hung up")
        return set(self.active)


class _Coordinator:
    def __init__(
        self,
        channel=0,
        events=("VideoMotion",),
        motion_enabled=True,
        poll_video_motion=False,
        client=None,
    ):
        self._channel = channel
        self.events = list(events)
        self.client = client or _Client()
        self.poll_video_motion = poll_video_motion
        self._motion_enabled = motion_enabled
        self.handled = []

    def get_channel(self):
        return self._channel

    def get_address(self):
        return ADDRESS

    def handle_event(self, event):
        self.handled.append(event)

    def is_motion_detection_enabled(self):
        return self._motion_enabled


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _issues(hass):
    from homeassistant.helpers import issue_registry as ir

    return {key[1] for key in ir.async_get(hass).issues if key[0] == "dahua"}


async def _stream_with(hass, *coordinators):
    stream = _host_stream(hass, ADDRESS)
    for coordinator in coordinators:
        stream.register(coordinator)
    await _settle()
    return stream


async def _an_hour_later(stream):
    """Past the hourly rate limit, the way the limit tests below do it, and
    with any restart the last check asked for allowed to attach."""
    stream._motion_push_last_check -= MOTION_PUSH_CHECK_INTERVAL_SECONDS
    await _settle()


async def _until_the_card(stream):
    """Two proofs an hour apart, which is what a card now takes on a stream
    with an explicit list (#1202): the first asks for [All] instead, and the
    second finds that did not help either."""
    await stream.async_check_video_motion_is_pushed()
    await _an_hour_later(stream)
    await stream.async_check_video_motion_is_pushed()
    await _settle()


# --- the fault, and only the fault -------------------------------------------


async def test_a_device_detecting_motion_it_never_pushed_gets_a_card(hass):
    """Still the outcome, one proof later than it used to be: [All] is asked
    for first, and the card follows only when that did not help."""
    client = _Client(active={2})
    stream = await _stream_with(hass, _Coordinator(client=client))

    await _until_the_card(stream)

    assert ISSUE_ID in _issues(hass)


async def test_a_pushed_event_during_the_read_prevents_a_false_card(hass):
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))

    async def read(code):
        stream.on_receive(motion(), 0)
        return {0}

    client.async_get_event_indexes_cgi = read
    await stream.async_check_video_motion_is_pushed()
    assert ISSUE_ID not in _issues(hass)


async def test_removing_the_last_device_entry_withdraws_its_card(hass):
    stream = await _stream_with(hass, _Coordinator(client=_Client(active={0})))
    await _until_the_card(stream)
    assert ISSUE_ID in _issues(hass)
    dahua_module._async_forget_host(hass, ADDRESS)
    assert ISSUE_ID not in _issues(hass)


async def test_selecting_all_events_includes_the_motion_push_check(hass):
    stream = await _stream_with(
        hass, _Coordinator(events=["All"], client=_Client(active={0}))
    )
    await stream.async_check_video_motion_is_pushed()
    assert ISSUE_ID in _issues(hass)


async def test_silence_is_not_evidence(hass):
    """An empty answer means "nothing active" and "no such code" alike, so it
    cannot be the trigger. This is the test that stops the card appearing on
    every quiet camera in the world."""
    client = _Client(active=set())
    stream = await _stream_with(hass, _Coordinator(client=client))

    await stream.async_check_video_motion_is_pushed()

    assert ISSUE_ID not in _issues(hass)


async def test_a_device_that_has_pushed_once_is_never_asked(hass):
    """One pushed event proves the device can, and no amount of quiet
    afterwards unproves it."""
    client = _Client(active={2})
    stream = await _stream_with(hass, _Coordinator(client=client))

    stream.on_receive(motion(0), 0)
    await stream.async_check_video_motion_is_pushed()

    assert client.asked == 0
    assert ISSUE_ID not in _issues(hass)


async def test_a_push_on_an_unconfigured_channel_still_counts(hass):
    """The question is whether the device pushes these at all, not whether
    anybody wanted that one. A recorder sends every channel's events down the
    one stream, so a channel nobody added is still evidence."""
    client = _Client(active={2})
    stream = await _stream_with(hass, _Coordinator(channel=0, client=client))

    stream.on_receive(motion(7), 0)
    await stream.async_check_video_motion_is_pushed()

    assert client.asked == 0


async def test_a_pushed_event_clears_a_card_already_raised(hass):
    client = _Client(active={2})
    stream = await _stream_with(hass, _Coordinator(client=client))
    await _until_the_card(stream)
    assert ISSUE_ID in _issues(hass)

    stream.on_receive(motion(0), 0)
    await stream.async_check_video_motion_is_pushed()

    assert ISSUE_ID not in _issues(hass)


# --- the free fix before the card (#1202) -------------------------------------
#
# An IP5M-B1186E on 2.800 took a seven code explicit list, sent heartbeats on it
# for days, and never sent one event; six codes worked, and codes=[All] on the
# same camera delivered `VideoMotion;action=Start`. The heartbeats are why the
# refusal fallback never fired and why the dump looked healthy. This check holds
# the only positive proof that the list is not working, so it acts on it first.


async def test_the_first_proof_asks_for_all_events_instead_of_raising_a_card(hass):
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))
    assert client.attached_with == [["VideoMotion"]]

    await stream.async_check_video_motion_is_pushed()
    await _settle()

    assert client.attached_with[-1] == ["All"]
    assert ISSUE_ID not in _issues(hass)
    assert stream._all_events_for_unpushed_motion == "active"


async def test_all_events_that_bring_motion_stay_and_no_card_comes(hass):
    """It worked, so it is left working. One pushed VideoMotion settles the
    question for the life of the host, as it always has."""
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))
    await stream.async_check_video_motion_is_pushed()
    await _settle()

    stream.on_receive(motion(0), 0)
    await _an_hour_later(stream)
    await stream.async_check_video_motion_is_pushed()
    await _settle()

    assert ISSUE_ID not in _issues(hass)
    assert client.attached_with[-1] == ["All"]


async def test_all_events_that_bring_nothing_are_put_back_and_the_card_raised(hass):
    """#996's XVR: it does not push VideoMotion however it is asked. Leaving it
    on a broader subscription would be a change made for nothing."""
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))

    await _until_the_card(stream)

    assert ISSUE_ID in _issues(hass)
    assert client.attached_with == [["VideoMotion"], ["All"], ["VideoMotion"]]
    assert stream._all_events_for_unpushed_motion == "failed"


async def test_the_request_is_moved_once_and_put_back_once(hass):
    """Not every hour for a day. The check runs up to 24 times."""
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))

    for _ in range(6):
        await stream.async_check_video_motion_is_pushed()
        await _an_hour_later(stream)

    assert client.attached_with == [["VideoMotion"], ["All"], ["VideoMotion"]]
    assert ISSUE_ID in _issues(hass)


async def test_a_stream_already_on_all_after_a_refusal_goes_straight_to_the_card(
    hass,
):
    """There is nothing left to ask it, and the refusal's [All] is not undone."""
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))
    stream._tried_all_events = True
    stream._restart_if_needed()
    await _settle()
    assert client.attached_with[-1] == ["All"]

    await stream.async_check_video_motion_is_pushed()
    await _settle()

    assert ISSUE_ID in _issues(hass)
    assert stream._all_events_for_unpushed_motion is None
    assert client.attached_with[-1] == ["All"]


async def test_a_channel_joining_does_not_put_back_the_list_it_went_quiet_on(hass):
    """Registration recomputes the request. Without carrying this the way a
    refusal is carried, a second channel arriving would quietly undo it."""
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))
    await stream.async_check_video_motion_is_pushed()
    await _settle()
    assert client.attached_with[-1] == ["All"]

    stream.register(_Coordinator(channel=1, client=client))
    await _settle()

    assert client.attached_with[-1] == ["All"]


async def test_a_device_quiet_after_the_move_stays_on_all_and_gets_no_card(hass):
    """The check only acts when it catches motion in progress, so a camera
    that goes quiet after the move is never proved either way. It stays on
    [All], which the device is serving, and the events are filtered back to
    the selection locally: nothing is lost by staying, and nothing is raised
    on silence, which is the rule #728 was built on."""
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))
    await stream.async_check_video_motion_is_pushed()
    await _settle()
    client.active = set()

    for _ in range(5):
        await _an_hour_later(stream)
        await stream.async_check_video_motion_is_pushed()

    assert client.attached_with == [["VideoMotion"], ["All"]]
    assert stream._all_events_for_unpushed_motion == "active"
    assert ISSUE_ID not in _issues(hass)


@pytest.mark.parametrize("on_all", ["refuse", "silent"])
async def test_all_events_the_device_will_not_serve_go_straight_back(hass, on_all):
    """A device that refuses [All], or attaches and sends nothing on it, would
    otherwise sit on a request that delivers nothing at all -- worse than the
    list it left, which may have been carrying everything but VideoMotion.
    Back at once, without waiting an hour for another proof."""
    client = _Client(active={0}, on_all=on_all)
    stream = await _stream_with(hass, _Coordinator(client=client))

    await stream.async_check_video_motion_is_pushed()
    await _settle()

    assert client.attached_with == [["VideoMotion"], ["All"], ["VideoMotion"]]
    assert stream._all_events_for_unpushed_motion == "failed"
    assert ISSUE_ID not in _issues(hass), "the card waits for the next proof"


async def test_all_events_that_failed_lead_to_the_card_on_the_next_proof(hass):
    client = _Client(active={0}, on_all="refuse")
    stream = await _stream_with(hass, _Coordinator(client=client))
    await stream.async_check_video_motion_is_pushed()
    await _settle()

    await _an_hour_later(stream)
    await stream.async_check_video_motion_is_pushed()
    await _settle()

    assert ISSUE_ID in _issues(hass)
    assert client.attached_with[-1] == ["VideoMotion"]


async def test_all_events_that_talked_and_then_dropped_are_kept(hass):
    """A stream that attached and delivered something took the request; a
    socket ending afterwards, status or not, is not [All] being refused. The
    same rule `_should_try_all_events` applies the other way round."""
    client = _Client(active={0}, on_all="talks_then_drops")
    stream = await _stream_with(hass, _Coordinator(client=client))

    await stream.async_check_video_motion_is_pushed()
    await _settle()

    assert stream._all_events_for_unpushed_motion == "active"
    assert client.attached_with == [["VideoMotion"], ["All"]]


async def test_a_trial_begun_on_the_last_check_can_still_be_judged(hass):
    """@rroller's reproduction on #1222: start one short of the limit with
    motion active, take the proof, advance an hour, check again. Sharing the
    budget, the trial began on the last check and was never judged: the stream
    stayed on [All] and the card a proof used to raise never came."""
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))
    stream._motion_push_checks = MOTION_PUSH_CHECK_LIMIT - 1
    stream._motion_push_last_check = -MOTION_PUSH_CHECK_INTERVAL_SECONDS

    await stream.async_check_video_motion_is_pushed()
    await _settle()
    assert stream._all_events_for_unpushed_motion == "active"
    assert stream._motion_push_checks == MOTION_PUSH_CHECK_LIMIT

    await _an_hour_later(stream)
    await stream.async_check_video_motion_is_pushed()
    await _settle()

    assert ISSUE_ID in _issues(hass)
    assert client.attached_with[-1] == ["VideoMotion"]


async def test_a_trial_gets_one_window_and_no_more(hass):
    """Its own budget, the same size as the first proof's, so a device that is
    quiet for the whole of it is not asked for ever either."""
    client = _Client(active={0})
    stream = await _stream_with(hass, _Coordinator(client=client))
    await stream.async_check_video_motion_is_pushed()
    await _settle()
    assert client.asked == 1
    client.active = set()

    for _ in range(MOTION_PUSH_CHECK_LIMIT + 5):
        await _an_hour_later(stream)
        await stream.async_check_video_motion_is_pushed()

    assert client.asked == 1 + MOTION_PUSH_CHECK_LIMIT
    assert stream._all_events_for_unpushed_motion == "active"


async def test_a_credentials_refusal_on_all_is_not_read_as_all_failing(hass):
    """401 is the password, with its own budget. Going back to the list would
    not help and would misreport what happened."""
    client = _Client(active={0}, on_all="unauthorised")
    stream = await _stream_with(hass, _Coordinator(client=client))

    await stream.async_check_video_motion_is_pushed()
    await _settle()

    assert stream._all_events_for_unpushed_motion == "active"
    assert client.attached_with == [["VideoMotion"], ["All"]]


# --- the reasons not to ask ---------------------------------------------------


async def test_nobody_is_told_about_a_setting_they_already_enabled(hass):
    client = _Client(active={2})
    stream = await _stream_with(
        hass, _Coordinator(poll_video_motion=True, client=client)
    )

    await stream.async_check_video_motion_is_pushed()

    # Only the card is asserted here, not the request count: registering a
    # coordinator with the option on starts the real poller, which asks this
    # same endpoint every two seconds. That is the feature working, and it
    # makes the count useless as a signal in this one test.
    assert ISSUE_ID not in _issues(hass)


async def test_enabling_the_setting_takes_the_card_away(hass):
    """Otherwise somebody ticks the box and cannot tell whether it worked."""
    coordinator = _Coordinator(client=_Client(active={2}))
    stream = await _stream_with(hass, coordinator)
    await _until_the_card(stream)
    assert ISSUE_ID in _issues(hass)

    coordinator.poll_video_motion = True
    await stream.async_check_video_motion_is_pushed()

    assert ISSUE_ID not in _issues(hass)


async def test_a_deselected_event_is_a_choice_not_a_fault(hass):
    """An empty selection turns the event connection off deliberately. Telling
    that user their motion is broken is telling them off for a setting."""
    client = _Client(active={2})
    stream = await _stream_with(
        hass, _Coordinator(events=["AlarmLocal"], client=client)
    )

    await stream.async_check_video_motion_is_pushed()

    assert client.asked == 0


async def test_motion_switched_off_on_the_device_is_not_a_fault_either(hass):
    client = _Client(active={2})
    stream = await _stream_with(hass, _Coordinator(motion_enabled=False, client=client))

    await stream.async_check_video_motion_is_pushed()

    assert client.asked == 0


async def test_one_channel_still_detecting_is_enough(hass):
    """A twelve channel recorder with motion off on eleven of them is still a
    recorder failing to push the twelfth."""
    client = _Client(active={2})
    stream = await _stream_with(
        hass,
        _Coordinator(channel=0, motion_enabled=False, client=client),
        _Coordinator(channel=1, motion_enabled=True, client=client),
    )

    await _until_the_card(stream)

    assert ISSUE_ID in _issues(hass)


# --- the cost ----------------------------------------------------------------


async def test_the_question_is_asked_at_most_once_an_hour(hass):
    client = _Client(active=set())
    stream = await _stream_with(hass, _Coordinator(client=client))

    for _ in range(5):
        await stream.async_check_video_motion_is_pushed()

    assert client.asked == 1, "the poll runs every two minutes; this must not"


async def test_the_question_stops_being_asked_eventually(hass):
    """A camera watching a locked gate genuinely never sees motion. Asking it
    for ever is load bought with nothing.

    The count is set rather than reached by looping, because looping would
    have to defeat the hourly rate limit as well and would then be testing
    the clock instead of the bound.
    """
    client = _Client(active=set())
    stream = await _stream_with(hass, _Coordinator(client=client))
    stream._motion_push_checks = MOTION_PUSH_CHECK_LIMIT

    await stream.async_check_video_motion_is_pushed()

    assert client.asked == 0


async def test_one_short_of_the_limit_still_asks(hass):
    """Otherwise the bound could be off by its whole value and nothing would
    notice: a check that never runs also never asks."""
    client = _Client(active=set())
    stream = await _stream_with(hass, _Coordinator(client=client))
    stream._motion_push_checks = MOTION_PUSH_CHECK_LIMIT - 1
    # Backdated past the rate limit. Home Assistant's test harness starts
    # `time.monotonic` near zero, so the class default of 0.0 reads as "asked
    # a moment ago" here while on a real system it reads as the host's uptime.
    stream._motion_push_last_check = -MOTION_PUSH_CHECK_INTERVAL_SECONDS

    await stream.async_check_video_motion_is_pushed()

    assert client.asked == 1


async def test_a_device_that_will_not_answer_is_not_accused(hass):
    client = _Client(raises=True)
    stream = await _stream_with(hass, _Coordinator(client=client))

    await stream.async_check_video_motion_is_pushed()

    assert ISSUE_ID not in _issues(hass)


async def test_a_bare_stream_does_not_raise(hass):
    """Several test files build one with object.__new__ and set three
    attributes. False from the class default means "no evidence", which is the
    safe reading for a card raised only on positive evidence."""
    stream = object.__new__(DahuaHostEventStream)
    stream._by_channel = {}
    stream._address = ADDRESS
    stream._hass = hass
    stream._owner = None

    await stream.async_check_video_motion_is_pushed()

    assert ISSUE_ID not in _issues(hass)
