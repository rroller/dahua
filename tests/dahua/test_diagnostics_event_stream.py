"""Diagnostics should report the event stream that exists, not the one that used to.

`events.stream_task_running` read `coordinator._event_task`. #615 moved the stream off
the coordinator onto one shared `DahuaHostEventStream` per address, because an NVR with
eleven channels was holding eleven identical streams and discarding ten copies of every
event. `_event_task` has been initialised to None and assigned nothing ever since, so
the field reported **False for every device on every version after that day**.

A field that is always False is worse than a missing one. On #728 somebody posted
diagnostics showing `stream_task_running: false` next to a camera whose events had
stopped, which reads as the cause and is not. It is my own field and my own #615 that
killed it.

What replaces it is chosen to separate the two failures #728 has been conflating, which
no data on that thread has ever been able to tell apart:

    task_running false                              nothing is attached at all
    task_running true, received_data false,
      last_attach_failed true                       the device refuses the attach
    task_running true, received_data true,
      and events.active_count 0                     events arrive, dispatch drops them

alpha520098 reports the third and jhall299's dump looks like one of the first two, and
until now the difference was invisible.
"""

import time
from types import SimpleNamespace

import pytest

import custom_components.dahua as dahua
from custom_components.dahua.diagnostics import _events_block, _stream_block

ADDRESS = "10.0.0.94"


class _Task:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


def _stream(**kwargs):
    """A stand-in for DahuaHostEventStream, with its real attribute names."""
    defaults = {
        "_task": _Task(),
        "_events": frozenset({"VideoMotion", "CrossRegionDetection"}),
        "_received_data": True,
        "_failing": False,
        "_consecutive_failures": 0,
        "_owner": None,
        "_by_channel": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _coordinator(address=ADDRESS, **kwargs):
    defaults = {
        "_address": address,
        # The attribute the old field read. Never assigned in the real coordinator,
        # which is the whole bug, so it stays None in every test here.
        "_event_task": None,
        "_vto_task": None,
        "_vto_client": None,
        "_dahua_event_listeners": {},
        "_dahua_event_timestamp": {},
        "_recent_events": [],
        "get_event_list": lambda: ["VideoMotion"],
        "get_channel": lambda: 0,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _clean_streams():
    """The registry is a module global, so a leaked entry would reach other tests."""
    dahua._HOST_STREAMS.clear()
    yield
    dahua._HOST_STREAMS.clear()


# --- the bug itself ---------------------------------------------------------

def test_a_live_stream_is_reported_as_running():
    """The regression test. `_event_task` is None, as it always is, and a stream is
    attached and working. Reading the coordinator attribute answers False here."""
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream(_task=_Task(done=False))

    assert _stream_block(coordinator)["task_running"] is True


def test_the_events_block_carries_it():
    """The helper being right is not the point; the dump is what people paste."""
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream(_task=_Task(done=False))

    block = _events_block(coordinator)

    assert block["stream"]["task_running"] is True


def test_the_field_that_was_always_false_is_gone():
    """Leaving it beside a correct one would be two answers to one question, and the
    wrong one is the one people already know to look for."""
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream()

    assert "stream_task_running" not in _events_block(coordinator)


def test_nothing_reads_the_dead_coordinator_attribute():
    """A running stream plus a *finished* `_event_task` must still read as running. If
    the attribute crept back in, this is what would catch it."""
    coordinator = _coordinator(_event_task=_Task(done=True))
    dahua._HOST_STREAMS[ADDRESS] = _stream(_task=_Task(done=False))

    assert _events_block(coordinator)["stream"]["task_running"] is True


# --- no stream is not a failure --------------------------------------------

def test_no_stream_registered_says_so_without_guessing():
    """No configured events means no stream is started, which is a choice and not a
    fault, so nothing else is claimed about it."""
    assert _stream_block(_coordinator()) == {"registered": False}


def test_a_stream_on_another_host_is_not_this_ones():
    """One entry per channel per host; a doorbell's stream is not the camera's."""
    dahua._HOST_STREAMS["10.0.0.2"] = _stream()

    assert _stream_block(_coordinator(address=ADDRESS))["registered"] is False


def test_a_coordinator_with_no_address_does_not_raise():
    """Diagnostics that raise return a 500 with no explanation."""
    assert _stream_block(_coordinator(address=None))["registered"] is False


# --- the three states it has to tell apart ---------------------------------

def test_the_device_refusing_the_attach_is_visible():
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream(
        _task=_Task(done=False), _received_data=False, _failing=True,
        _consecutive_failures=4)

    block = _stream_block(coordinator)

    assert block["task_running"] is True
    assert block["received_data"] is False
    assert block["last_attach_failed"] is True
    assert block["consecutive_failures"] == 4


def test_events_arriving_while_dispatch_drops_them_is_visible():
    """alpha520098's symptom, and the one no data has ever shown: the stream is up and
    the device is talking, and not one event has reached a sensor."""
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream(_task=_Task(done=False), _received_data=True)

    block = _events_block(coordinator)

    assert block["stream"]["task_running"] is True
    assert block["stream"]["received_data"] is True
    assert block["active_count"] == 0


def test_nothing_attached_at_all_is_visible():
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream(_task=_Task(done=True))

    assert _stream_block(coordinator)["task_running"] is False


def test_a_stream_that_has_no_task_yet_is_not_running():
    dahua._HOST_STREAMS[ADDRESS] = _stream(_task=None)

    assert _stream_block(_coordinator())["task_running"] is False


# --- what the device was actually asked for -------------------------------

def test_the_attached_events_are_the_union_the_device_was_asked_for():
    """A code that is not in here cannot arrive, whatever this entry has configured.
    The union is across every channel on the host, so one channel's selection explains
    another channel's missing sensor."""
    dahua._HOST_STREAMS[ADDRESS] = _stream(
        _events=frozenset({"VideoMotion", "AlarmLocal", "CrossRegionDetection"}))

    assert _stream_block(_coordinator())["attached_events"] == [
        "AlarmLocal", "CrossRegionDetection", "VideoMotion"]


def test_an_empty_attachment_is_reported_as_empty_not_missing():
    dahua._HOST_STREAMS[ADDRESS] = _stream(_events=frozenset())

    assert _stream_block(_coordinator())["attached_events"] == []


def test_configured_and_attached_are_both_reported():
    """They differ exactly when another channel on the host widened the stream, and
    telling them apart is the point of reporting both."""
    coordinator = _coordinator(get_event_list=lambda: ["VideoMotion"])
    dahua._HOST_STREAMS[ADDRESS] = _stream(
        _events=frozenset({"VideoMotion", "AlarmLocal"}))

    block = _events_block(coordinator)

    assert block["configured"] == ["VideoMotion"]
    assert block["stream"]["attached_events"] == ["AlarmLocal", "VideoMotion"]


# --- who is lending the stream its client ---------------------------------

def test_the_entry_lending_its_client_is_named():
    """The stream borrows one channel's client, so a reload of that entry moves the
    stream and disturbs channels nobody touched."""
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream(_owner=coordinator)

    assert _stream_block(coordinator)["this_entry_owns_it"] is True


def test_a_borrower_that_is_not_this_entry_says_so():
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream(_owner=_coordinator())

    assert _stream_block(coordinator)["this_entry_owns_it"] is False


def test_how_many_channels_share_the_stream_is_reported():
    """An NVR report is one of eleven against one stream, which is the single most
    common thing missing from a "my camera dropped out" issue."""
    others = [_coordinator(), _coordinator(), _coordinator()]
    dahua._HOST_STREAMS[ADDRESS] = _stream(
        _by_channel={0: [others[0]], 3: [others[1], others[2]]})

    block = _stream_block(_coordinator())

    assert block["channels_registered"] == [0, 3]
    assert block["coordinators_registered"] == 3


# --- and nothing sensitive goes in ----------------------------------------

def test_no_coordinator_or_client_object_is_published():
    """`_owner` is a coordinator holding a client holding a password. Only the boolean
    derived from it may appear, and the same goes for the registered coordinators."""
    coordinator = _coordinator()
    dahua._HOST_STREAMS[ADDRESS] = _stream(
        _owner=coordinator, _by_channel={0: [coordinator]})

    block = _stream_block(coordinator)

    assert isinstance(block["this_entry_owns_it"], bool), (
        "the owner must be reduced to a boolean, not described: %r"
        % (block["this_entry_owns_it"],))
    for value in block.values():
        assert isinstance(value, (bool, int, str, list)), value
        for text in ([value] if isinstance(value, str) else
                     [v for v in value if isinstance(v, str)]
                     if isinstance(value, list) else []):
            assert "namespace" not in text.lower(), text
            assert "object at 0x" not in text, text
