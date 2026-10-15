"""What a device that never answers leaves behind, for whoever has to read it.

#1057's retry asks a capability question again on later polls and gives up
after `MAX_CAPABILITY_RETRIES`. Until now it said nothing at all about either:
the one `_LOGGER.info` fires only when something turns up, every failed
attempt is swallowed by design, and the attempt count lives on the coordinator
where no dump reports it.

So the case the mechanism was built for -- a recorder channel that stays
silent -- produced no entities, no log line, and no field in the diagnostics.
"I reloaded and the switch still is not there" could not be told apart from
"the retry never ran", which is the question a reporter is actually being
asked to answer.

Nothing about what is asked, or when, changes here.
"""

import logging

import aiohttp
import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.coordinator import MAX_CAPABILITY_RETRIES

ADDRESS = "10.0.0.88"
CHANNEL = 14


class _Client:
    """The one read the lighting_v2 probe makes, as the test dictates.

    Answers are consumed in order and the last one repeats, so a test can say
    "silent, silent, then the device finally says no" without counting calls.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.reads = 0

    async def async_get_lighting_v2(self):
        self.reads += 1
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


def _coordinator(*answers):
    """A coordinator carrying the retry machinery and nothing else.

    `lighting_v2` is the question, because it is the one #1043 measured timing
    out on twelve channels of a nineteen channel recorder.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Client(*answers)
    coordinator._channel = CHANNEL
    coordinator._address = ADDRESS
    coordinator._supports_lighting_v2 = False
    # What the setup probe left behind when the read timed out: recorded, and
    # recorded as not answered, which is what makes it retryable at all.
    coordinator._probe_refusals = {"lighting_v2": {"answered": False}}
    coordinator._capability_retries = {}
    coordinator.data = {}
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "entry-1"})()
    coordinator.get_serial_number = lambda: "AMR044032F4F06B33E_14"
    return coordinator


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    """The discovery signal wants a real hass, and none of this is about it."""
    monkeypatch.setattr(
        coordinator_module,
        "async_notify_capability_discovered",
        lambda hass, entry_id, channel_key: None,
    )


async def _ask_until_it_gives_up(coordinator):
    """Poll often enough to spend the budget, the way a quiet host does."""
    for _ in range(MAX_CAPABILITY_RETRIES + 2):
        await coordinator._async_retry_unanswered_capabilities()


# --- the log ----------------------------------------------------------------


async def test_a_question_that_is_given_up_on_says_so(caplog):
    coordinator = _coordinator(TimeoutError())

    with caplog.at_level(logging.INFO, logger="custom_components.dahua.coordinator"):
        await _ask_until_it_gives_up(coordinator)

    giving_up = [
        record for record in caplog.records if "did not answer about" in record.message
    ]
    assert len(giving_up) == 1, "said nothing, or said it on every later poll"
    said = giving_up[0].getMessage()
    assert "lighting_v2" in said
    assert str(CHANNEL) in said, "a recorder's channels are told apart by number"
    assert str(MAX_CAPABILITY_RETRIES) in said


async def test_it_is_not_said_while_there_are_attempts_left(caplog):
    """Said once the budget is spent, not on the first attempt that failed:
    otherwise it reports a loss that has not happened yet."""
    coordinator = _coordinator(TimeoutError())

    with caplog.at_level(logging.INFO, logger="custom_components.dahua.coordinator"):
        await coordinator._async_retry_unanswered_capabilities()

    assert not [
        record for record in caplog.records if "did not answer about" in record.message
    ]
    assert coordinator.client.reads == 1


async def test_a_question_that_gets_answered_is_not_reported_as_lost(caplog):
    """The capability turns up on the retry, which is the mechanism working.
    Nothing about that is a thing to give up on."""
    coordinator = _coordinator({"table.Lighting_V2[0][0][0].Mode": "Manual"})

    with caplog.at_level(logging.INFO, logger="custom_components.dahua.coordinator"):
        await _ask_until_it_gives_up(coordinator)

    assert coordinator._supports_lighting_v2 is True
    assert not [
        record for record in caplog.records if "did not answer about" in record.message
    ]
    assert [
        record
        for record in caplog.records
        if "answered a capability question" in record.message
    ], "the discovery line is still the one that fires"


async def test_every_attempt_is_visible_in_debug(caplog):
    """The question a reporter is really being asked is whether it ran at
    all, and that must be answerable without reading the entity list."""
    coordinator = _coordinator(TimeoutError())

    with caplog.at_level(logging.DEBUG, logger="custom_components.dahua.coordinator"):
        await _ask_until_it_gives_up(coordinator)

    attempts = [
        record.getMessage()
        for record in caplog.records
        if "asked" in record.message and "again about" in record.message
    ]
    assert len(attempts) == MAX_CAPABILITY_RETRIES
    assert "attempt 1 of %s" % MAX_CAPABILITY_RETRIES in attempts[0]
    assert "still nothing" in attempts[0]


async def test_a_device_that_finally_says_no_is_not_reported_as_silent(caplog):
    """The distinction the give-up line rests on.

    A refusal carrying a status is the device answering, and an answer is not
    a loss even when the answer is no: the entities it gates are correctly not
    created, and nothing was lost by asking. Only a question still unanswered
    when the budget runs out is worth a line.

    Silent twice and then refused on the last attempt is the only input that
    reaches this: a refusal on an earlier attempt takes the question out of
    the pending set, so it never gets to the budget at all.
    """
    refused = aiohttp.ClientResponseError(None, (), status=400)
    coordinator = _coordinator(TimeoutError(), TimeoutError(), refused)

    with caplog.at_level(logging.INFO, logger="custom_components.dahua.coordinator"):
        await _ask_until_it_gives_up(coordinator)

    assert coordinator._probe_refusals["lighting_v2"]["answered"] is True
    assert not [
        record for record in caplog.records if "did not answer about" in record.message
    ], "the device answered, so there is nothing to report as unanswered"


# --- and the dump -----------------------------------------------------------


async def test_the_attempts_spent_are_in_the_diagnostics():
    """A dump is what a reporter attaches, and this is the field that says
    the device was asked rather than never asked."""
    from custom_components.dahua.diagnostics import _capabilities_block

    coordinator = _coordinator(TimeoutError())
    await _ask_until_it_gives_up(coordinator)

    block = _capabilities_block(coordinator)

    assert block["capability_retries"] == {"lighting_v2": MAX_CAPABILITY_RETRIES}
    assert block["capability_retry_budget"] == MAX_CAPABILITY_RETRIES
    assert block["refusals"]["lighting_v2"]["answered"] is False


async def test_a_coordinator_that_never_retried_still_dumps():
    """Tests and old coordinators build these by hand, and the dump must not
    be the thing that raises."""
    from custom_components.dahua.diagnostics import _capabilities_block

    coordinator = object.__new__(DahuaDataUpdateCoordinator)

    block = _capabilities_block(coordinator)

    assert block["capability_retries"] == {}
