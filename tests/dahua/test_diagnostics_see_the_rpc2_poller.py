"""A dump should say which of the three event transports is carrying events.

There are three, and until now a diagnostics dump named none of them. #780 added an
RPC2 event poller for devices whose `eventManager.cgi` is absent, and nothing about it
reached the dump at all: every event field described the CGI stream, so on a device using
the poller the dump described a transport that was not in use.

That is what #728 has been stuck on. A reporter whose events had stopped could not tell
a dead transport from a quiet camera, and the one field that looked like an answer was
reporting a task that had not existed since #615 (fixed in the change this builds on).

The three states worth telling apart, now visible:

    transport: "vto_listener"   a doorbell; the CGI stream is never registered for one
    transport: "rpc2_poll"      no CGI event path, so the poll runs in the stream's task
    transport: "cgi_stream"     everything else
    transport: null             no events configured, so nothing was started

And for the poller specifically, `last_cycle_age_seconds` is the live signal: one that
described itself and then died keeps `used: true` while that age grows without bound.
"""

import time
from types import SimpleNamespace

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua import diagnostics as diag_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.diagnostics import (
    _events_block,
    _rpc2_poll_block,
    _transport,
)

ADDRESS = "10.0.0.94"


class _Task:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


def _coordinator(address=ADDRESS, vto_task=None):
    return SimpleNamespace(
        _address=address,
        _vto_task=vto_task,
        _vto_client=None,
        _dahua_event_listeners={},
        _dahua_event_timestamp={},
        _recent_events=[],
        get_event_list=lambda: ["VideoMotion"],
        get_channel=lambda: 0,
    )


def _described(address=ADDRESS, **kwargs):
    state = {
        "code_count": 9,
        "cycle_seconds": 2.0,
        "eased_cycle_seconds": 6,
        "idle_after_seconds": 120,
        "last_cycle": time.monotonic(),
        "eased_off": False,
    }
    state.update(kwargs)
    client_module._HOST_RPC2_EVENT_POLL[address] = state
    return state


# --- which transport ------------------------------------------------------


def test_a_doorbell_is_named_as_the_vto_listener():
    """A doorbell takes the VTO listener exclusively: `if not is_doorbell` is what
    registers a CGI stream, so one never appears for it."""
    assert _transport(_coordinator(vto_task=_Task()), {"used": False}) == "vto_listener"


def test_a_device_with_no_cgi_event_path_is_named_as_the_poll():
    assert _transport(_coordinator(), {"used": True}) == "rpc2_poll"


def test_an_ordinary_camera_is_named_as_the_cgi_stream():
    from custom_components import dahua

    dahua._HOST_STREAMS[ADDRESS] = SimpleNamespace(_task=_Task())
    try:
        assert _transport(_coordinator(), {"used": False}) == "cgi_stream"
    finally:
        dahua._HOST_STREAMS.clear()


def test_no_events_configured_names_nothing_rather_than_guessing():
    """Not a fault. No configured events means no transport was started."""
    assert _transport(_coordinator(), {"used": False}) is None


def test_a_doorbell_is_a_doorbell_even_beside_a_stale_poll_entry():
    """Order matters and is pinned: the VTO listener is the exclusive transport for a
    doorbell, so nothing else may claim it."""
    assert _transport(_coordinator(vto_task=_Task()), {"used": True}) == "vto_listener"


def test_the_events_block_names_the_transport():
    """The helper being right is not the point; the dump is what gets pasted."""
    _described()

    assert _events_block(_coordinator())["transport"] == "rpc2_poll"


def test_the_events_block_carries_the_poll_detail():
    """Naming the transport is not enough. The rate, the age of the last cycle and what
    is held active are what make a dump diagnosable, and a block that is built and then
    not passed along is the exact failure this file's neighbours keep catching."""
    _described(code_count=42)

    block = _events_block(_coordinator())["rpc2_poll"]

    assert block["used"] is True
    assert block["polled_code_count"] == 42


# --- the poller's own state -----------------------------------------------


def test_a_host_that_never_polled_says_so_and_claims_nothing_else():
    assert _rpc2_poll_block(_coordinator()) == {"used": False}


def test_another_hosts_poll_is_not_this_ones():
    _described(address="10.0.0.2")

    assert _rpc2_poll_block(_coordinator(address=ADDRESS))["used"] is False


def test_the_poll_rate_is_reported():
    """getEventIndexes takes one code at a time, so the code count is also the request
    count per cycle, and that is why a long list polls slowly."""
    _described(code_count=42, cycle_seconds=8.4)

    block = _rpc2_poll_block(_coordinator())

    assert block["polled_code_count"] == 42
    assert block["cycle_seconds"] == 8.4


def test_the_eased_off_rate_and_threshold_are_reported():
    _described(eased_cycle_seconds=6, idle_after_seconds=120, eased_off=True)

    block = _rpc2_poll_block(_coordinator())

    assert block["eased_cycle_seconds"] == 6
    assert block["idle_after_seconds"] == 120
    assert block["eased_off"] is True


def test_a_completed_cycle_is_reported_as_an_age():
    """An epoch would be unreadable next to a reporter's own clock. The age is the
    thing: a poller that died has a `used` of true and an age that keeps growing."""
    _described(last_cycle=time.monotonic() - 30)

    age = _rpc2_poll_block(_coordinator())["last_cycle_age_seconds"]

    assert 29 <= age <= 31


def test_a_poller_that_has_not_finished_a_cycle_reports_no_age():
    """It describes itself before its first cycle, so this is the state a poller that
    could never complete one sits in."""
    _described(last_cycle=None)

    assert _rpc2_poll_block(_coordinator())["last_cycle_age_seconds"] is None


# --- what it is holding active -------------------------------------------


def test_the_active_events_are_reported():
    """A code stuck in here is a sensor stuck on, which is the shape of #728's other
    half and was previously invisible."""
    _described()
    client_module._HOST_RPC2_EVENT_STATE[ADDRESS] = {
        ("SmartMotionHuman", 0),
        ("CrossRegionDetection", 0),
    }

    assert _rpc2_poll_block(_coordinator())["active"] == [
        "CrossRegionDetection-0",
        "SmartMotionHuman-0",
    ]


def test_nothing_active_is_an_empty_list_not_a_missing_key():
    _described()

    assert _rpc2_poll_block(_coordinator())["active"] == []


def test_the_active_set_is_read_for_this_host_only():
    _described()
    client_module._HOST_RPC2_EVENT_STATE["10.0.0.2"] = {("VideoMotion", 0)}

    assert _rpc2_poll_block(_coordinator())["active"] == []


# --- the poller has to publish it, or none of the above is real -----------


class _StopPoll(Exception):
    """Escape the poller's endless loop, as test_rpc2_event_poll.py does."""


class _FakeRpc2Client:
    def __init__(self, cycles):
        self._cycles = cycles
        self._served = 0

    async def request(self, method, params=None, object_id=None, **kwargs):
        if method == "eventManager.attach":
            return {"result": True, "params": {"SID": 1234}}
        if method != "eventManager.getEventIndexes":
            return {"result": True, "params": {}}
        self._served += 1
        if self._served > self._cycles:
            raise _StopPoll()
        return {"result": True, "params": {"indexes": []}}


class _Refusing(_FakeRpc2Client):
    async def request(self, method, params=None, object_id=None, **kwargs):
        if method == "eventManager.attach":
            return {"result": True, "params": {"SID": 1234}}
        raise _StopPoll()


def _polling_client(monkeypatch, fake, address=ADDRESS):
    client = DahuaClient("u", "p", address, 80, 554, object())
    holder = SimpleNamespace(client=fake, task=object(), keepalive=None)

    async def _shared(self):
        return holder

    monkeypatch.setattr(DahuaClient, "_shared_rpc2", _shared)
    monkeypatch.setattr(client_module, "RPC2_EVENT_POLL_SECONDS", 0)
    monkeypatch.setattr(client_module, "RPC2_EVENT_MAX_REQUESTS_PER_SECOND", 1000)
    return client


async def test_the_poller_publishes_what_it_is_doing(monkeypatch):
    """Driving the real poller. A diagnostics block reading a store nothing writes is
    the same class of bug as the field this change set out to fix."""
    client = _polling_client(monkeypatch, _FakeRpc2Client(cycles=2))

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(lambda data, channel: None, ["VideoMotion"], 0)

    published = client_module._HOST_RPC2_EVENT_POLL[ADDRESS]
    assert published["code_count"] == 1
    assert published["last_cycle"] is not None


async def test_the_description_exists_before_the_first_cycle(monkeypatch):
    """Published before the loop, so a poller that can never complete a cycle still
    says what it was trying to do."""
    client = _polling_client(monkeypatch, _Refusing(cycles=0))

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(lambda data, channel: None, ["VideoMotion"], 0)

    published = client_module._HOST_RPC2_EVENT_POLL[ADDRESS]
    assert published["code_count"] == 1
    assert published.get("last_cycle") is None


async def test_the_block_reads_what_the_poller_writes(monkeypatch):
    """End to end across the two files, so a renamed key on either side fails here
    rather than rendering an empty field in somebody's dump."""
    client = _polling_client(monkeypatch, _FakeRpc2Client(cycles=1))

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(lambda data, channel: None, ["VideoMotion"], 0)

    block = _rpc2_poll_block(_coordinator())

    assert block["used"] is True
    assert block["polled_code_count"] == 1
    assert block["cycle_seconds"] is not None
    assert block["last_cycle_age_seconds"] is not None


async def test_an_expanded_all_is_counted_as_the_codes_it_became(monkeypatch):
    """ "All" cannot be polled, so it is expanded. The count has to describe what is
    actually asked for, since that is what sets the cycle."""
    from custom_components.dahua.config_flow import ALL_EVENTS

    client = _polling_client(monkeypatch, _FakeRpc2Client(cycles=1))

    with pytest.raises(_StopPoll):
        await client._stream_events_rpc2(lambda data, channel: None, ["All"], 0)

    published = client_module._HOST_RPC2_EVENT_POLL[ADDRESS]
    assert published["code_count"] == len([c for c in ALL_EVENTS if c != "All"])
    assert published["code_count"] > 1


# --- and it is forgotten with the host --------------------------------------


def test_the_poll_state_is_dropped_when_the_last_entry_for_a_host_goes(monkeypatch):
    """Otherwise it is remembered for the life of the process, and a dump for a
    different device at a recycled address would describe the old one's poller.

    Safe only here: the active set deliberately outlives a reload, because a Start whose
    Stop was lost is still owed one. `_async_forget_host` runs from `async_remove_entry`
    once the last entry for the address has gone, so there is nobody left to owe."""
    from custom_components import dahua

    _described()
    client_module._HOST_RPC2_EVENT_STATE[ADDRESS] = {("VideoMotion", 0)}
    monkeypatch.setattr(
        dahua, "ir", SimpleNamespace(async_delete_issue=lambda *args, **kwargs: None)
    )

    dahua._async_forget_host(SimpleNamespace(), ADDRESS)

    assert ADDRESS not in client_module._HOST_RPC2_EVENT_POLL
    assert ADDRESS not in client_module._HOST_RPC2_EVENT_STATE


# --- nothing sensitive ------------------------------------------------------


def test_only_names_and_counts_are_published():
    """The poller holds a client holding a password. None of it may appear."""
    _described()
    client_module._HOST_RPC2_EVENT_STATE[ADDRESS] = {("VideoMotion", 0)}

    for value in _rpc2_poll_block(_coordinator()).values():
        assert isinstance(value, (bool, int, float, str, list, type(None))), value
        if isinstance(value, list):
            for item in value:
                assert isinstance(item, str), item
                assert "object at 0x" not in item
