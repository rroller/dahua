"""A smart motion probe that went silent is asked again, and its pair turns up.

Part of #1057. The mechanism landed in #1059 and this is the SmartMotionDetect
question wired into it. Until now a channel whose read of that table timed out
was recorded as a channel that cannot do smart motion detection, and that
verdict lasted until someone reloaded the entry. On the nineteen channel
recorder in #1043 which probes expired was different on every run, so a reload
was a fresh lottery rather than a fix.

This one costs two entities rather than one: the smart motion switch and, on the
same answer, the Sensitivity select. `switch.py` is wired through `late.py` here
to receive them, which the select platform already was.

The timing is the whole of the fix and is not visible in the diff. The retry
runs at the top of a poll, before that poll reads anything, and the rebuild it
asks for is a task. So the rebuild can run while the poll is still in flight,
and `supports_smart_motion_detection()` reads this channel's row out of `data`.
A rebuild that looked then would find no row, create nothing, and never be
asked again, because the probe has by then been recorded as answered. The probe
therefore puts the table it just read into `data`, where the switch and the
select already look. That is what `_the_row_is_there_before_the_poll_lands`
pins, and the version of this change that kept the rows beside `data` passes
every other test in this file.

The distinction the fix rests on is kept: a device that *answers* must not be
asked twice. An HTTP refusal never reaches the probe, because `async_get_config`
turns a `ClientResponseError` into {}, and a 200 carrying rows for other
channels is equally the device answering about this one. Only silence stays open.

Nine mutations, each applied to the real source, each named with the tests that
failed rather than the tests that ought to:

| reintroduced | caught by |
| --- | --- |
| the registry entry removed | `gates_its_switch_and_select`, `the_row_is_there_before_the_poll_lands`, `not_announced_again`, `stays_silent_is_let_go` |
| the answer kept beside `data` rather than put in it | `the_row_is_there_before_the_poll_lands`, `gates_its_switch_and_select` |
| a table with no row for this channel treated as supported | `a_table_without_this_channels_row` |
| the empty dict a 4xx becomes treated as silence | `an_empty_table_is_an_answer` |
| the good read not recorded as an answer | `gates_its_switch_and_select`, `not_announced_again` |
| silence not recorded as pending | `a_timed_out_probe_leaves_the_question_open`, and four others |
| the retry reading the gate its own probe just wrote | `gates_its_switch_and_select`, `not_announced_again` |
| the setup probe inlined again rather than shared | `judgment_is_made_in_exactly_one_place` |
| `switch.py` left adding its entities once | `gates_its_switch_and_select` (through `late.py`), `the_switch_platform_rebuilds` |

Those runs were against the real `coordinator.py` and `switch.py` with Home
Assistant stubbed, since neither can be imported without it. That proves the
logic and not the wiring, so the file itself ran for the first time on CI.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua import select as select_module
from custom_components.dahua import switch as switch_module

from . import adds_entities
from .integration_source import modules, source

ADDRESS = "10.0.0.88"
CHANNEL = 3
SERIAL = "AMR044032F4F06B33E_3"

# This channel's row, as a recorder spells it. Enable is false deliberately: the
# row is the capability and not the state, measured on a DHI-NVR5464-16P-EI
# where switching it off left the row in place reading false.
ANSWERED = {
    "table.SmartMotionDetect[{0}].Enable".format(CHANNEL): "false",
    "table.SmartMotionDetect[{0}].Sensitivity".format(CHANNEL): "Middle",
}

# A table that came back for the host and carries nothing for this channel. The
# recorder serves the slot, it just has no camera there that can do this.
ANSWERED_ABOUT_ANOTHER_CHANNEL = {"table.SmartMotionDetect[0].Enable": "true"}


class _Client:
    """Answers the one read this probe makes, as the test dictates.

    `answers` is consumed in order and the last one repeats, so a test can say
    "silent, then it answers" without counting calls.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.reads = 0

    async def async_get_smart_motion_detection(self):
        self.reads += 1
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


def _coordinator(*answers):
    """A coordinator carrying the probe, the retry machinery and nothing else.

    `object.__new__` rather than the constructor, as the rest of this suite
    does: building a real one wants a hass, an entry and a device.

    `data` starts empty and no test seeds a row into it. A test that did would
    pass whether or not the probe puts its answer where the entities read it,
    which is the one thing here that is easy to get wrong.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Client(*answers)
    coordinator._channel = CHANNEL
    coordinator._address = ADDRESS
    coordinator._supports_smart_motion_detection = False
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.data = {}
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "entry-1"})()
    coordinator.get_serial_number = lambda: SERIAL
    # An Amcrest doorbell reads its own rule table instead, and the accessors
    # below ask. `model` is what decides, and this is not one.
    coordinator.model = "DHI-NVR5464-16P-EI"
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


def _ready_for_platform_setup(coordinator):
    """Answer the other questions the two platforms ask of a channel.

    Only the smart motion pair is under test, so everything else says no. The
    smart motion answers are left alone: those are the real methods, and
    replacing them is how a test of a capability gate ends up testing itself.
    """
    for name in (
        "is_indoor_monitor_without_video",
        "creates_siren_entity",
        "supports_privacy_mode",
        "supports_alarm_output",
        "supports_disarming_linkage",
        "is_amcrest_doorbell",
        "supports_security_light",
        "supports_day_night_color",
        "supports_infrared_light",
        "is_indoor_monitor",
    ):
        setattr(coordinator, name, lambda: False)
    coordinator.get_ivs_rules = lambda: []
    coordinator.get_model = lambda: "DHI-NVR5464-16P-EI"
    coordinator.subentry_id = None
    return coordinator


async def _entities_both_platforms_create(coordinator, monkeypatch):
    """Run the real platform setups, as the rebuild after a late answer does.

    `late.py` calls each platform's `build` again when a capability turns up;
    running `async_setup_entry` is that same call, and it is the gate's verdict
    rather than the dispatcher that is in question here. The dispatcher half is
    covered by test_entities_that_arrive_late.py.
    """
    from types import SimpleNamespace

    async def _no_presets(_coordinator):
        return []

    monkeypatch.setattr(select_module, "_async_preset_ids", _no_presets)
    for module, names in (
        (
            switch_module,
            (
                ("DahuaMotionDetectionBinarySwitch", "motion switch"),
                ("DahuaSmartMotionDetectionBinarySwitch", "smart motion switch"),
            ),
        ),
        (
            select_module,
            (("DahuaSmartMotionSensitivitySelect", "smart motion select"),),
        ),
    ):
        for attribute, marker in names:
            monkeypatch.setattr(
                module, attribute, lambda *a, _marker=marker, **k: _marker
            )

    added = []
    subscribed = []
    for module in (switch_module, select_module):
        await module.async_setup_entry(
            SimpleNamespace(data={}),
            SimpleNamespace(
                entry_id="entry-1",
                options={},
                runtime_data={0: coordinator},
                async_on_unload=lambda unsub: subscribed.append(unsub),
            ),
            adds_entities(added),
        )
    return added, subscribed


# --- silence is not an answer ------------------------------------------------


async def test_a_timed_out_probe_leaves_the_question_open():
    coordinator = _coordinator(TimeoutError())

    assert await coordinator._async_probe_smart_motion_detect() is False
    assert coordinator.supports_smart_motion_detection() is False
    assert coordinator.probes_that_did_not_answer() == {"smart_motion_detect"}


async def test_a_table_without_this_channels_row_is_an_answer():
    """Judged on what came back, not on whether it raised. A recorder answers
    200 with rows for the channels that can do it, and that is it saying no for
    the others. Asking again would be asking it to repeat itself."""
    coordinator = _coordinator(TimeoutError(), ANSWERED_ABOUT_ANOTHER_CHANNEL)
    await coordinator._async_probe_smart_motion_detect()

    assert await coordinator._async_probe_smart_motion_detect() is False
    assert coordinator.supports_smart_motion_detection() is False
    assert coordinator.probes_that_did_not_answer() == set(), (
        "the question was open and the device has now answered it, so asking "
        "again is asking it to repeat itself"
    )


async def test_an_empty_table_is_an_answer():
    """What a 400 looks like by the time it gets here: `async_get_config`
    swallows the `ClientResponseError` and returns {}. The status is the device
    saying it does not serve the table, so it must not be retried."""
    coordinator = _coordinator(TimeoutError(), {})
    await coordinator._async_probe_smart_motion_detect()
    assert coordinator.probes_that_did_not_answer() == {"smart_motion_detect"}

    assert await coordinator._async_probe_smart_motion_detect() is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_an_answer_is_not_asked_for_twice():
    coordinator = _coordinator(ANSWERED)

    assert await coordinator._async_probe_smart_motion_detect() is True
    assert coordinator.supports_smart_motion_detection() is True
    assert coordinator.probes_that_did_not_answer() == set()


# --- asked again, and both controls turn up ----------------------------------


async def test_the_answer_that_arrives_late_gates_its_switch_and_select(
    discoveries, monkeypatch
):
    """Through the real registry, not a narrowed one: a test that monkeypatches
    `RETRYABLE_CAPABILITIES` down to this capability passes just as happily
    when the registry entry is deleted."""
    coordinator = _ready_for_platform_setup(_coordinator(TimeoutError(), ANSWERED))
    await coordinator._async_probe_smart_motion_detect()

    assert "smart_motion_detect" in coordinator_module.RETRYABLE_CAPABILITIES, (
        "the retry registry is what turns a pending probe into a second ask, "
        "and nothing else reaches _async_retry_smart_motion_detect"
    )
    before, subscribed = await _entities_both_platforms_create(coordinator, monkeypatch)
    assert before == [
        "motion switch"
    ], "the channel starts with neither control, which is what #1043 reported"
    assert len(subscribed) == 2, (
        "both platforms have to be listening, or the answer below is announced "
        "to nobody: %s" % subscribed
    )

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_smart_motion_detection() is True
    assert coordinator.probes_that_did_not_answer() == set()
    assert discoveries == [("entry-1", SERIAL)], (
        "the platforms are told which channel answered, so a recorder rebuilds "
        "one channel rather than all nineteen"
    )
    after, _ = await _entities_both_platforms_create(coordinator, monkeypatch)
    assert after == [
        "motion switch",
        "smart motion switch",
        "smart motion select",
    ]


async def test_the_row_is_there_before_the_poll_lands(discoveries):
    """The rebuild is a task and the retry runs before the poll reads anything.

    So the row has to be in `data` by the time the retry returns. Nothing in
    this test runs a poll, which is the point: the entity gate reads
    `data["table.SmartMotionDetect[3].Enable"]`, and a fix that recorded the
    rows anywhere else would leave the switch and the select uncreated with the
    question already marked answered.
    """
    coordinator = _coordinator(TimeoutError(), ANSWERED)
    await coordinator._async_probe_smart_motion_detect()
    assert coordinator.data == {}

    await coordinator._async_retry_unanswered_capabilities()

    assert (
        coordinator.data.get("table.SmartMotionDetect[{0}].Enable".format(CHANNEL))
        == "false"
    ), "the answer is not where the two entities read it"
    assert coordinator.get_smart_motion_sensitivity() == "Middle", (
        "the select reads its current option from the same row, so a control "
        "created from a late answer shows a real value rather than unknown"
    )


async def test_a_second_answer_is_not_announced_again(discoveries):
    """A probe that answers on the retry has nothing left to discover, and a
    channel that keeps being rebuilt would re-read the device each time."""
    coordinator = _coordinator(TimeoutError(), ANSWERED)
    await coordinator._async_probe_smart_motion_detect()

    await coordinator._async_retry_unanswered_capabilities()
    await coordinator._async_retry_unanswered_capabilities()

    assert len(discoveries) == 1
    assert coordinator.client.reads == 2, (
        "once at setup and once on the retry. A third read would mean the "
        "answer was not recorded as one"
    )


async def test_a_channel_that_stays_silent_is_let_go(discoveries):
    """Bounded, because a genuinely silent channel must not be asked for ever."""
    coordinator = _coordinator(TimeoutError())
    await coordinator._async_probe_smart_motion_detect()

    for _ in range(coordinator_module.MAX_CAPABILITY_RETRIES + 3):
        await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.client.reads == 1 + coordinator_module.MAX_CAPABILITY_RETRIES
    assert discoveries == []
    assert coordinator.supports_smart_motion_detection() is False


# --- the shape of the source, so the two halves cannot drift apart -----------

_SOURCE = source()
_SWITCH_SOURCE = next(
    text for path, text in modules().items() if path.name == "switch.py"
)


def test_the_smart_motion_judgment_is_made_in_exactly_one_place():
    """Setup and the retry share `_async_probe_smart_motion_detect`. Inlining
    the probe back into setup is the mutation this catches: the two copies
    would then be free to disagree about whether a 200 with no row for this
    channel is an answer, which is the distinction the fix rests on."""
    assert _SOURCE.count('_note_probe_refusal("smart_motion_detect"') == 1
    assert _SOURCE.count("async_get_smart_motion_detection()") == 2, (
        "once in the probe and once in the poll's fetch. A third call site "
        "means a second judgment, and none in the poll means an entity that "
        "can only ever read unknown"
    )


def test_the_switch_platform_rebuilds():
    """The select platform was already wired. Without this the retry announces
    a capability to a platform that added its entities once and went away."""
    assert "async_add_and_keep_watching(" in _SWITCH_SOURCE
    assert "async_add_devices(devices" not in _SWITCH_SOURCE, (
        "setup adding its own entities means the ones that arrive late are "
        "added by nobody"
    )
