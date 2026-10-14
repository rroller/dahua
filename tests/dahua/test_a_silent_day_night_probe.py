"""A Day/Night probe that went silent is asked again, and its select turns up.

Part of #1057. The mechanism landed in #1059 for the two lighting probes; this
is the Day/Night mode question wired into it. Until now a channel whose
`VideoInOptions` read timed out was recorded as a channel with no Day/Night
mode, and that verdict lasted until someone reloaded the entry. On the
nineteen channel recorder in #1043 which probes expired was different on every
run, so a reload was a fresh lottery rather than a fix.

Two things make this worth doing and neither is obvious from the diff:

* The table is a host-wide read, so once any channel of a recorder gets an
  answer the shared read cache serves it to the next channel that asks. The
  retries after the first are nearly free.
* The poll decides each cycle whether to fetch it, gated on
  `supports_day_night_color()`, so a select created from a late answer reads a
  real value from the next poll. An entity that can only ever read unknown
  would be worse than no entity, which is what AGENTS.md says and what makes
  this capability a safe one to convert.

The distinction the whole fix rests on is kept: a device that *answers* must
not be asked twice. An HTTP refusal never reaches the probe at all, because
`async_get_config` turns a `ClientResponseError` into `{}`, and a 200 carrying
no row for this channel is equally an answer. Only silence stays open.

Six mutations, each applied to the real source and each named with the tests
that failed, not with the tests that ought to:

| reintroduced | caught by |
| --- | --- |
| the registry entry removed | `late_gates_its_select`, `not_announced_again`, `stays_silent_is_let_go` |
| a no-row answer treated as supported | `a_table_without_this_channels_row` |
| the good read not recorded as an answer | `late_gates_its_select`, `not_announced_again` |
| silence not recorded as pending | `timed_out_probe_leaves_the_question_open`, and four others |
| the retry reading the flag its own probe just wrote | `late_gates_its_select`, `not_announced_again` |
| the setup probe inlined again rather than shared | `judgment_is_made_in_exactly_one_place` |

The derive check below catches none of those, which is worth saying because it
looks like it should: it asserts the registry is a subset of the probe names,
so a key that is *missing* passes it. It is there for a key that is misspelled.

Those runs were of these assertions against the methods lifted out of
`coordinator.py` with ast, since the module cannot be imported without Home
Assistant. That proves the logic and not the wiring, so the file itself ran for
the first time on CI.
"""

import re

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module

from .integration_source import source

ADDRESS = "10.0.0.88"
CHANNEL = 3
SERIAL = "AMR044032F4F06B33E_3"

# This channel's row, as the device spells it. 0 is Color.
ANSWERED = {"table.VideoInOptions[{0}].DayNightColor".format(CHANNEL): "0"}

# A table that came back for the host but carries nothing for this channel,
# which is a recorder answering about a slot it does not serve.
ANSWERED_ABOUT_ANOTHER_CHANNEL = {"table.VideoInOptions[0].DayNightColor": "0"}


class _Client:
    """Answers the one read this probe makes, as the test dictates.

    `answers` is consumed in order and the last one repeats, so a test can say
    "silent, then it answers" without counting calls.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.reads = 0

    async def async_get_video_in_options(self):
        self.reads += 1
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


def _coordinator(*answers):
    """A coordinator carrying the probe, the retry machinery and nothing else.

    `object.__new__` rather than the constructor, as the rest of this suite
    does: building a real one wants a hass, an entry and a device.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Client(*answers)
    coordinator._channel = CHANNEL
    coordinator._address = ADDRESS
    coordinator._supports_day_night_color = False
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "entry-1"})()
    coordinator.get_serial_number = lambda: SERIAL
    # The accessor the select platform reads is allowed to veto on its own
    # grounds, and does for an indoor monitor with no camera. Not one here.
    coordinator.is_indoor_monitor_without_video = lambda: False
    return coordinator


@pytest.fixture
def discoveries(monkeypatch):
    """What the coordinator announced, captured where it looks the name up.

    `coordinator.py` does `from .late import async_notify_capability_discovered`,
    so patching `late` would not reach the bound name and the real dispatcher
    would be called with `hass=None`.
    """
    announced = []
    monkeypatch.setattr(
        coordinator_module,
        "async_notify_capability_discovered",
        lambda hass, entry_id, channel_key: announced.append((entry_id, channel_key)),
    )
    return announced


# --- silence is not an answer ------------------------------------------------


async def test_a_timed_out_probe_leaves_the_question_open():
    coordinator = _coordinator(TimeoutError())

    assert await coordinator._async_probe_day_night_color() is False
    assert coordinator.supports_day_night_color() is False
    assert coordinator.probes_that_did_not_answer() == {"day_night_color"}


async def test_a_table_without_this_channels_row_is_an_answer():
    """Judged on what came back, not on whether it raised. A recorder answers
    200 with rows for the channels it serves, and that is it saying no for the
    others. Asking again would be asking it to repeat itself."""
    coordinator = _coordinator(ANSWERED_ABOUT_ANOTHER_CHANNEL)

    assert await coordinator._async_probe_day_night_color() is False
    assert coordinator.supports_day_night_color() is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_an_answer_is_not_asked_for_twice():
    coordinator = _coordinator(ANSWERED)

    assert await coordinator._async_probe_day_night_color() is True
    assert coordinator.supports_day_night_color() is True
    assert coordinator.probes_that_did_not_answer() == set()


# --- asked again, and the select turns up ------------------------------------


async def test_the_answer_that_arrives_late_gates_its_select(discoveries):
    """Through the real registry, not a narrowed one: a test that monkeypatches
    `RETRYABLE_CAPABILITIES` down to this capability passes just as happily
    when the registry entry is deleted."""
    coordinator = _coordinator(TimeoutError(), ANSWERED)
    await coordinator._async_probe_day_night_color()

    assert "day_night_color" in coordinator_module.RETRYABLE_CAPABILITIES, (
        "the retry registry is what turns a pending probe into a second ask, "
        "and nothing else reaches _async_retry_day_night_color"
    )

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_day_night_color() is True
    assert coordinator.probes_that_did_not_answer() == set()
    assert discoveries == [("entry-1", SERIAL)], (
        "the platforms are told which channel answered, so a recorder rebuilds "
        "one channel rather than all nineteen"
    )


async def test_a_second_answer_is_not_announced_again(discoveries):
    """A probe that answers on the retry has nothing left to discover, and a
    channel that keeps being rebuilt would re-read the device each time."""
    coordinator = _coordinator(TimeoutError(), ANSWERED)
    await coordinator._async_probe_day_night_color()

    await coordinator._async_retry_unanswered_capabilities()
    await coordinator._async_retry_unanswered_capabilities()

    assert len(discoveries) == 1
    assert coordinator.client.reads == 2, (
        "once at setup and once on the retry. A third read would mean the "
        "answer was not recorded as one"
    )


async def test_a_channel_that_stays_silent_is_let_go(discoveries):
    """Twelve probes went unanswered on the recorder in #1043. Asking all of
    them every poll would spend twelve timeouts a cycle rather than twelve
    once."""
    coordinator = _coordinator(TimeoutError())
    await coordinator._async_probe_day_night_color()
    reads_at_setup = coordinator.client.reads

    for _ in range(10):
        await coordinator._async_retry_unanswered_capabilities()

    assert (
        coordinator.client.reads - reads_at_setup
        == coordinator_module.MAX_CAPABILITY_RETRIES
    )
    assert discoveries == []


# --- one judgment, in one place ----------------------------------------------

#: Every module of the integration at once, through the helper that exists so a
#: scan like this does not become a coupling to which file holds the code.
_SOURCE = source()


def test_every_retryable_capability_is_a_name_some_probe_records():
    """The registry is keyed by the name `_note_probe_refusal` was given. A key
    that matches no such name is retried for a question nothing ever marks
    pending, which fails by doing nothing at all.

    Derived from the source rather than listed here, because a list would be
    one more thing to keep in step."""
    recorded = set(re.findall(r'_note_probe_refusal\(\s*"([a-z_0-9 ]+)"', _SOURCE))

    assert recorded, "the scan found no probe names at all, so it proves nothing"
    assert (
        set(coordinator_module.RETRYABLE_CAPABILITIES) <= recorded
    ), "retried but never recorded as pending: %s" % (
        set(coordinator_module.RETRYABLE_CAPABILITIES) - recorded
    )


def test_the_day_night_judgment_is_made_in_exactly_one_place():
    """Setup and the retry share `_async_probe_day_night_color`. Inlining the
    probe back into setup is the mutation this catches: the two copies would
    then be free to disagree about whether a 200 with no row is an answer,
    which is precisely the distinction the fix rests on."""
    assert _SOURCE.count('_note_probe_refusal("day_night_color"') == 1
    assert _SOURCE.count("async_get_video_in_options()") == 2, (
        "once in the probe and once in the poll's fetch. A third call site "
        "means a second judgment"
    )
