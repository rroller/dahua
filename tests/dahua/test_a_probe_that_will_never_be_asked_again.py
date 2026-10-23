"""Which unanswered probes are worth waiting for, and which are simply absent.

Reported by @kmich on #1114, with the diagnosis already done: the
leftover-rows card could not appear on **any of his seventeen entries**, on a
VTH2421F-P, on IPCs and on an NVR alike. Every one reported

    cloud_upgrade: {"answered": false, "status": null, "error": "ValueError"}
    capability_retries: {"cloud_upgrade": 3}

while three of his monitors really did have leftover `disarming` and
`event_notifications` rows to offer.

**Two separate faults, and either one alone is enough to hold the card shut.**

1. ⭐ *A reply with no table was being filed as silence.* The device answered
   `configManager.getConfig` perfectly well; the reply just had no
   `_DHCloudUpgrade_` table in it, because this firmware has none.
   `get_cloud_upgrade_info` raised a bare `ValueError`, and
   `_note_probe_refusal` decides "did the device answer?" by looking for an
   HTTP `status` or an RPC2 `code`. A bare `ValueError` has neither. So the
   one shape that is *most certainly* an answer was the one recorded as no
   answer at all. `Rpc2TableAbsent` says so explicitly.

2. ⭐ *A probe that had given up still held the gate.* `_entry_is_wholly_healthy`
   waited on anything in `probes_that_did_not_answer`, whose justification is
   that "the retry builds the entity when the device finally replies". That
   stops being true once `_async_retry_capabilities` has spent
   `MAX_CAPABILITY_RETRIES`, and it was never true for a probe outside
   `RETRYABLE_CAPABILITIES`, which is never re-asked at all. After that the
   row is not pending, it is absent.

⚠️ Fixing only the first would leave the second waiting on any probe that
genuinely never answers, which is a real state: a camera that times out on one
table three times running gets no card for the life of the entry. Fixing only
the second would still mislabel every `_DHCloudUpgrade_`-less device as silent
in diagnostics, where the field is read to decide what a model does. Both.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.coordinator import (
    MAX_CAPABILITY_RETRIES,
    RETRYABLE_CAPABILITIES,
)
from custom_components.dahua.rpc2 import DahuaRpc2Client, Rpc2TableAbsent


def _coordinator(refusals=None, retries=None):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._probe_refusals = dict(refusals or {})
    c._capability_retries = dict(retries or {})
    return c


def _silent(name):
    return {name: {"answered": False, "status": None, "error": "TimeoutError"}}


# A capability that really is re-asked, so these tests are not written against
# a name the retry loop has never heard of.
RETRYABLE = sorted(RETRYABLE_CAPABILITIES)[0]


# --- the reader: a reply with no table ---------------------------------------


async def test_a_reply_without_the_table_raises_the_answered_kind():
    """⚠️ Not a bare `ValueError`. The type is the whole fix: it is what tells
    the probe bookkeeping that the device replied."""
    client = object.__new__(DahuaRpc2Client)
    client._session_id = "s1"

    async def get_config(params):
        # What his devices actually send back: a reply, and no such table.
        return {}

    client.get_config = get_config

    with pytest.raises(Rpc2TableAbsent):
        await client.get_cloud_upgrade_info()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"table": None},
        {"table": []},
        {"table": "not a list"},
        {"table": [None]},
        {"table": ["not a dict"]},
    ],
)
async def test_every_shape_of_missing_table_is_the_answered_kind(params):
    client = object.__new__(DahuaRpc2Client)
    client._session_id = "s1"

    async def get_config(_params):
        return params

    client.get_config = get_config

    with pytest.raises(Rpc2TableAbsent):
        await client.get_cloud_upgrade_info()


async def test_a_table_that_is_there_is_still_returned():
    """The fix must not turn a working read into a refusal."""
    client = object.__new__(DahuaRpc2Client)
    client._session_id = "s1"

    async def get_config(_params):
        return {"table": [{"Version": "2.800.000"}]}

    client.get_config = get_config

    assert await client.get_cloud_upgrade_info() == {"Version": "2.800.000"}


def test_the_absent_kind_is_still_a_value_error():
    """Existing handlers catch `ValueError`, and this must not slip past them."""
    assert issubclass(Rpc2TableAbsent, ValueError)


def test_the_absent_kind_declares_that_the_device_answered():
    assert Rpc2TableAbsent("x").answered is True


# --- which probes are worth waiting for ---------------------------------------


def test_a_probe_with_retries_left_may_still_answer():
    c = _coordinator(_silent(RETRYABLE), {RETRYABLE: MAX_CAPABILITY_RETRIES - 1})

    assert c.probes_that_may_still_answer() == {RETRYABLE}


def test_a_probe_that_has_never_been_retried_may_still_answer():
    """No entry in `_capability_retries` means the loop has not reached it
    yet, which is the most pending a probe can be."""
    c = _coordinator(_silent(RETRYABLE))

    assert c.probes_that_may_still_answer() == {RETRYABLE}


def test_a_probe_that_has_spent_its_budget_may_not():
    """⭐ kmich's case. `_async_retry_capabilities` skips a name once
    `spent >= MAX_CAPABILITY_RETRIES`, so nothing will ever ask again."""
    c = _coordinator(_silent(RETRYABLE), {RETRYABLE: MAX_CAPABILITY_RETRIES})

    assert c.probes_that_may_still_answer() == set()
    # ⚠ And it is still unanswered, which is a different question and stays
    # true. Diagnostics reports what the device did; the card asks what is
    # still to come.
    assert c.probes_that_did_not_answer() == {RETRYABLE}


def test_a_probe_nothing_retries_may_not_either():
    """Not in `RETRYABLE_CAPABILITIES`, so it is asked once at setup and never
    again. Waiting for it is waiting for nothing."""
    c = _coordinator(_silent("a_capability_no_retry_loop_knows"))

    assert c.probes_that_may_still_answer() == set()


def test_an_answered_refusal_is_in_neither_set():
    c = _coordinator({"coaxial_control": {"answered": True, "status": 400}})

    assert c.probes_that_did_not_answer() == set()
    assert c.probes_that_may_still_answer() == set()


def test_a_coordinator_that_has_recorded_nothing_is_waiting_for_nothing():
    """`object.__new__` skips `__init__`, and a dozen tests build coordinators
    that way, so neither attribute may be assumed to exist."""
    c = object.__new__(DahuaDataUpdateCoordinator)

    assert c.probes_that_may_still_answer() == set()


# --- and the two halves meet ---------------------------------------------------


def test_the_cloud_upgrade_probe_no_longer_reports_silence(monkeypatch):
    """⭐⭐ End to end on the exact reported case: a device that replies
    without `_DHCloudUpgrade_` must not look like one that said nothing.

    Driven through `_note_probe_refusal` with the real exception the real
    reader raises, rather than asserting the two halves separately and
    trusting them to meet.
    """
    c = _coordinator()

    client = object.__new__(DahuaRpc2Client)
    client._session_id = "s1"

    async def get_config(_params):
        return {"params": "but no table"}

    client.get_config = get_config

    async def run():
        try:
            await client.get_cloud_upgrade_info()
        except Exception as probe_error:  # the probe's own handler
            c._note_probe_refusal("cloud_upgrade", probe_error)

    import asyncio

    asyncio.run(run())

    assert c._probe_refusals["cloud_upgrade"]["answered"] is True
    assert c.probes_that_did_not_answer() == set()
    assert c.probes_that_may_still_answer() == set()
