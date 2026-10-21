"""A flood of one code must not push the event a reporter was asked for out.

Measured on a live VTO2000A. A doorbell was pressed, diagnostics were taken
about thirty seconds later, and the press was already gone: `MDResult` had
arrived forty times in the window, plus motion and video-blind edges, so all
ten remembered slots held the flood. `arrived_with_no_listener` read

    {"RtspSessionDisconnect-0": 5, "MDResult-0": 40, "KeepLightOn-0": 2}

while `recent` held nothing but `MDResult`, `VideoMotion` and `VideoBlind`.

That field exists so a reporter can be asked for a dump after pressing a
button, which is the single most common request on this tracker. On a busy
device it could not answer that.

A longer list loses the same race a little later, so the fix is not a bigger
number: the newest event of each **code** is kept alongside the chronological
list. A code arriving eight times a second occupies one slot instead of all of
them, and the cost is bounded by how many different things a device says
rather than by how often it says them.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.coordinator import (
    RECENT_EVENT_CODES,
    RECENT_EVENT_COUNT,
)
from custom_components.dahua.diagnostics import _latest_by_code_block


def _coordinator():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._address = "10.0.0.232"
    return c


def _event(code, **data):
    return {"Code": code, "Action": "Pulse", "Index": 0, "Data": dict(data)}


def _flood(coordinator, code="MDResult", times=40):
    for _ in range(times):
        coordinator._remember_event(_event(code))


# --- the reported case --------------------------------------------------------


def test_a_press_survives_the_flood_that_follows_it():
    """The measurement, reproduced: one press, then the forty MDResult events
    that really arrived in the next thirty seconds."""
    c = _coordinator()

    c._remember_event(_event("BackKeyLight", State=1))
    _flood(c)

    assert "BackKeyLight" in c._latest_event_by_code
    assert c._latest_event_by_code["BackKeyLight"]["event"]["Data"]["State"] == 1


def test_the_chronological_list_still_loses_it():
    """Not a regression, the existing field's job is unchanged: it answers
    "what happened just now", and just now really was forty MDResults. The new
    field is what answers "what did the press look like"."""
    c = _coordinator()

    c._remember_event(_event("BackKeyLight", State=1))
    _flood(c)

    codes = [row["event"]["Code"] for row in c._recent_events]
    assert "BackKeyLight" not in codes
    assert len(c._recent_events) == RECENT_EVENT_COUNT


def test_the_flood_itself_keeps_only_its_newest():
    c = _coordinator()

    for index in range(40):
        c._remember_event(_event("MDResult", n=index))

    assert c._latest_event_by_code["MDResult"]["event"]["Data"]["n"] == 39


def test_every_distinct_code_is_kept():
    """The real doorbell capture: eight different codes around one press."""
    c = _coordinator()
    seen = [
        "BackKeyLight",
        "KeepLightOn",
        "CallNoAnswered",
        "Invite",
        "MDResult",
        "VideoMotion",
        "VideoBlind",
        "RtspSessionDisconnect",
    ]
    for code in seen:
        c._remember_event(_event(code))
    _flood(c)

    assert sorted(c._latest_event_by_code) == sorted(seen)


# --- and it cannot grow without bound ----------------------------------------


def test_a_device_inventing_codes_cannot_grow_it_for_ever():
    """The cap is a defence rather than an expectation. A dump has to stay
    something a reporter can attach."""
    c = _coordinator()

    for index in range(RECENT_EVENT_CODES * 3):
        c._remember_event(_event("Code{0}".format(index)))

    assert len(c._latest_event_by_code) == RECENT_EVENT_CODES


def test_a_code_already_known_is_still_updated_once_the_cap_is_reached():
    """Otherwise the cap would freeze the field: the codes a device sends
    first would keep their first event for ever, which is worse than not
    keeping them at all."""
    c = _coordinator()
    c._remember_event(_event("BackKeyLight", State=1))
    for index in range(RECENT_EVENT_CODES * 3):
        c._remember_event(_event("Filler{0}".format(index)))

    c._remember_event(_event("BackKeyLight", State=0))

    assert c._latest_event_by_code["BackKeyLight"]["event"]["Data"]["State"] == 0


@pytest.mark.parametrize("code", [None, "", 7, {"a": 1}])
def test_an_event_with_no_usable_code_is_not_kept_by_code(code):
    """`_remember_event` runs on the event stream and is written to be
    incapable of raising: an unguarded exception there takes every camera on
    the host down until the stream reconnects (#705, #706)."""
    c = _coordinator()

    c._remember_event({"Code": code, "Data": {}})

    assert c._latest_event_by_code == {}
    assert len(c._recent_events) == 1, "it still belongs in the chronological list"


# --- what the dump shows ------------------------------------------------------


def test_the_dump_publishes_it_aged_and_redacted():
    c = _coordinator()
    c._remember_event(_event("BackKeyLight", State=1))
    row = c._latest_event_by_code["BackKeyLight"]
    row["seconds_ago_at_capture"] = 1_000

    block = _latest_by_code_block(c, 1_030)

    assert set(block) == {"BackKeyLight"}
    assert block["BackKeyLight"]["seconds_ago"] == 30
    assert block["BackKeyLight"]["event"]["Code"] == "BackKeyLight"


def test_the_dump_is_empty_rather_than_absent_on_a_quiet_device():
    assert _latest_by_code_block(_coordinator(), 0) == {}


def test_the_dump_tolerates_a_coordinator_that_never_saw_an_event():
    """Diagnostics runs against doubles and against a coordinator whose stream
    never started, and it may not raise for either."""
    assert _latest_by_code_block(SimpleNamespace(), 0) == {}
