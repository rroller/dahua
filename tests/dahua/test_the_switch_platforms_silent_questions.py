"""The three questions the switch platform waits on are asked again.

Part of #1057, and the same mechanism #1059 built for the two lighting probes.
The alarm output, the disarming linkage and the event notifications were each
decided once at setup and never revisited, so a channel whose read timed out
was a channel with no alarm output switch and no disarming switches, until
somebody reloaded the entry. On the nineteen channel recorder in #1043 which
probes expired was different on every run, so a reload was another lottery.

What each one costs when it is lost:

* `alarm_output` gates the alarm output switch, and the poll reads its state
  only where the gate is set, so the switch arrives able to read a real value.
* `disarming_linkage` gates two switches, the linkage itself and the event
  notifications beside it.
* `event_notifications` gates no entity. It decides whether the poll reads the
  value the second of those switches shows, so a channel that lost it had a
  switch that could only ever read off.

The distinction the fix rests on is kept: a device that *answers* is not asked
twice. For the alarm output that includes answering "two outputs", which is a
fact about the hardware and a definite no to the switch, and answering with a
body that is not a number at all.

The judgment these two reads make is deliberately left as setup has always
made it. `async_get_disarming_linkage` goes through `async_get_config`, which
turns an HTTP refusal into `{}`, so a device without the table reads as having
it and gets two switches that can only read off. That is worth fixing and is
not what this is: tightening it would take those switches away from devices
that have them today, which wants its own change and its own measurement. The
tests below pin the current judgment so that change has to be deliberate.

Reintroduced one at a time against the real source, each named with the tests
that actually failed:

| reintroduced | caught by |
| --- | --- |
| the three registry entries removed | every `gates_` and `let_go` test |
| the setup probes inlined again | the three `in_exactly_one_place` tests |
| a good read not recorded as an answer | `gates_`, `not_announced_again` |
| the retry reading the flag its own probe just wrote | `not_announced_again` |
| two outputs treated as still pending | `two_outputs_is_an_answer` |
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module

from .integration_source import source

ADDRESS = "10.0.0.88"
CHANNEL = 3
SERIAL = "AMR044032F4F06B33E_3"

# One physical output, which is the only shape `supports_alarm_output` accepts.
ONE_OUTPUT = {"result": "1"}
# A device saying it has two. An answer, and a no: the multi-output getOutState
# encoding has never been verified here.
TWO_OUTPUTS = {"result": "2"}

LINKAGE = {"table.DisableLinkage.Enable": "false"}
NOTIFICATIONS = {"table.DisableEventNotify.Enable": "false"}


class _Device:
    """The three reads these capabilities come from, as the test dictates.

    Each list is consumed in order and the last entry repeats, so a test says
    "silent, then it answers" without counting calls. An `Exception` is raised
    rather than returned.
    """

    def __init__(
        self, alarm=(ONE_OUTPUT,), linkage=(LINKAGE,), notify=(NOTIFICATIONS,)
    ):
        self._answers = {
            "alarm": list(alarm),
            "linkage": list(linkage),
            "notify": list(notify),
        }
        self.reads = {"alarm": 0, "linkage": 0, "notify": 0}

    def _answer(self, name):
        self.reads[name] += 1
        answers = self._answers[name]
        answer = answers[0] if len(answers) == 1 else answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def async_get_alarm_output_slots(self):
        return self._answer("alarm")

    async def async_get_disarming_linkage(self):
        return self._answer("linkage")

    async def async_get_event_notifications(self):
        return self._answer("notify")


def _coordinator(**answers):
    """A coordinator carrying the probes, the retry machinery and nothing else.

    `object.__new__` rather than the constructor, as the rest of this suite
    does: building a real one wants a hass, an entry and a device.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Device(**answers)
    coordinator._channel = CHANNEL
    coordinator._address = ADDRESS
    coordinator._alarm_output_slots = 0
    coordinator._supports_disarming_linkage = False
    coordinator._supports_event_notifications = False
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "entry-1"})()
    coordinator.get_serial_number = lambda: SERIAL
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


# --- the alarm output --------------------------------------------------------


async def test_a_timed_out_alarm_output_probe_leaves_the_question_open():
    coordinator = _coordinator(alarm=(TimeoutError(),))

    assert await coordinator._async_probe_alarm_output() is False
    assert coordinator.supports_alarm_output() is False
    assert coordinator.probes_that_did_not_answer() == {"alarm_output"}


async def test_two_outputs_is_an_answer():
    """A definite no to the switch, because the multi-output encoding is not
    verified, and a settled fact about the hardware. Asking again would be
    asking the device to repeat itself.

    Driven from silence rather than straight to the answer. Starting here with
    a device that has never been asked proves nothing: `probes_that_did_not_
    answer` is derived from the refusals recorded, so with none recorded it is
    empty whatever the probe concludes, and this assertion would hold with the
    answer never being noted at all. Measured: that mutation passes a version
    of this test that skips the timeout.
    """
    coordinator = _coordinator(alarm=(TimeoutError(), TWO_OUTPUTS))
    await coordinator._async_probe_alarm_output()
    assert coordinator.probes_that_did_not_answer() == {"alarm_output"}

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_alarm_output() is False
    assert coordinator.probes_that_did_not_answer() == set(), (
        "the device said how many outputs it has, which is an answer, and a "
        "question that has been answered is closed"
    )

    reads = coordinator.client.reads["alarm"]
    await coordinator._async_retry_unanswered_capabilities()
    assert coordinator.client.reads["alarm"] == reads


async def test_a_body_that_is_not_a_number_is_an_answer():
    """The device served the path and said something this cannot use. That is
    firmware, not silence."""
    coordinator = _coordinator(alarm=(TimeoutError(), {"result": "nonsense"}))
    await coordinator._async_probe_alarm_output()

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator._alarm_output_slots == 0
    assert coordinator.probes_that_did_not_answer() == set()


async def test_an_alarm_output_that_arrives_late_gates_its_switch(discoveries):
    """Through the real registry, not a narrowed one: a test that monkeypatches
    `RETRYABLE_CAPABILITIES` down to this capability passes just as happily
    when the registry entry is deleted."""
    coordinator = _coordinator(alarm=(TimeoutError(), ONE_OUTPUT))
    await coordinator._async_probe_alarm_output()

    assert "alarm_output" in coordinator_module.RETRYABLE_CAPABILITIES

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_alarm_output() is True
    assert coordinator.probes_that_did_not_answer() == set()
    assert discoveries == [("entry-1", SERIAL)], (
        "the platforms are told which channel answered, so a recorder rebuilds "
        "one channel rather than all nineteen"
    )


# --- the disarming linkage, and the notifications beside it ------------------


async def test_a_timed_out_linkage_probe_leaves_the_question_open():
    coordinator = _coordinator(linkage=(TimeoutError(),))

    assert await coordinator._async_probe_disarming_linkage() is False
    assert coordinator.supports_disarming_linkage() is False
    assert coordinator.probes_that_did_not_answer() == {"disarming_linkage"}


async def test_an_empty_answer_still_counts_as_the_table_being_there():
    """Pinned rather than endorsed. `async_get_config` turns a refusal into
    `{}`, so this is what setup has always concluded, and the retry must not
    quietly decide otherwise while nobody is looking at the difference."""
    coordinator = _coordinator(linkage=({},))

    assert await coordinator._async_probe_disarming_linkage() is True
    assert coordinator.supports_disarming_linkage() is True
    assert coordinator.probes_that_did_not_answer() == set()


async def test_a_linkage_that_arrives_late_gates_its_two_switches(discoveries):
    coordinator = _coordinator(linkage=(TimeoutError(), LINKAGE))
    await coordinator._async_probe_disarming_linkage()

    assert "disarming_linkage" in coordinator_module.RETRYABLE_CAPABILITIES

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_disarming_linkage() is True
    assert discoveries == [("entry-1", SERIAL)]


async def test_event_notifications_that_arrive_late_are_read_by_the_poll(discoveries):
    """No entity is gated on this one, so the thing to assert is the flag the
    poll reads. Without it the switch beside the linkage shows off for ever."""
    coordinator = _coordinator(notify=(TimeoutError(), NOTIFICATIONS))
    await coordinator._async_probe_event_notifications()
    assert coordinator._supports_event_notifications is False

    assert "event_notifications" in coordinator_module.RETRYABLE_CAPABILITIES

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator._supports_event_notifications is True
    assert discoveries == [("entry-1", SERIAL)]


# --- asked again, but not for ever -------------------------------------------


async def test_a_second_answer_is_not_announced_again(discoveries):
    """A probe that answers on the retry has nothing left to discover, and a
    channel that keeps being rebuilt would re-read the device each time."""
    coordinator = _coordinator(
        alarm=(TimeoutError(), ONE_OUTPUT),
        linkage=(TimeoutError(), LINKAGE),
        notify=(TimeoutError(), NOTIFICATIONS),
    )
    await coordinator._async_probe_alarm_output()
    await coordinator._async_probe_disarming_linkage()
    await coordinator._async_probe_event_notifications()

    await coordinator._async_retry_unanswered_capabilities()
    await coordinator._async_retry_unanswered_capabilities()

    assert len(discoveries) == 1, (
        "three capabilities answering in one pass is one rebuild, and the "
        "second pass has nothing left to find"
    )
    assert coordinator.client.reads == {"alarm": 2, "linkage": 2, "notify": 2}, (
        "once at setup and once on the retry. A third read of any of them "
        "means that answer was not recorded as one"
    )


async def test_a_channel_that_stays_silent_is_let_go(discoveries):
    """Twelve probes went unanswered on the recorder in #1043. Asking all of
    them every poll would spend twelve timeouts a cycle rather than twelve
    once."""
    coordinator = _coordinator(
        alarm=(TimeoutError(),),
        linkage=(TimeoutError(),),
        notify=(TimeoutError(),),
    )
    await coordinator._async_probe_alarm_output()
    await coordinator._async_probe_disarming_linkage()
    await coordinator._async_probe_event_notifications()
    at_setup = dict(coordinator.client.reads)

    for _ in range(10):
        await coordinator._async_retry_unanswered_capabilities()

    budget = coordinator_module.MAX_CAPABILITY_RETRIES
    assert {
        name: count - at_setup[name] for name, count in coordinator.client.reads.items()
    } == {"alarm": budget, "linkage": budget, "notify": budget}
    assert discoveries == []


# --- one judgment, in one place ----------------------------------------------

#: Every module of the integration at once, through the helper that exists so a
#: scan like this does not become a coupling to which file holds the code.
_SOURCE = source()


def test_the_alarm_output_judgment_is_made_in_exactly_one_place():
    """Setup and the retry share `_async_probe_alarm_output`. Inlining it back
    into setup is the mutation this catches: the two copies would then be free
    to disagree about what counts as an answer."""
    assert _SOURCE.count('_note_probe_refusal("alarm_output"') == 1
    assert _SOURCE.count("async_get_alarm_output_slots()") == 1, (
        "only the probe asks how many outputs there are; the poll reads their "
        "state through async_get_alarm_output_state"
    )


def test_the_disarming_linkage_judgment_is_made_in_exactly_one_place():
    assert _SOURCE.count('_note_probe_refusal("disarming_linkage"') == 1
    assert _SOURCE.count("async_get_disarming_linkage()") == 2, (
        "once in the probe and once in the poll. A third call site means a "
        "second judgment"
    )


def test_the_event_notifications_judgment_is_made_in_exactly_one_place():
    assert _SOURCE.count('_note_probe_refusal("event_notifications"') == 1
    assert _SOURCE.count("async_get_event_notifications()") == 2
