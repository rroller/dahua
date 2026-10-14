"""A coaxial status probe that went silent is asked again.

Part of #1057, and the last of the capability questions that needs nothing
from the platforms: no entity is created from this one. `supports_siren` and
`supports_security_light` decide that from the model and from the RPC2
deterrence probe, and `_supports_coaxial_control` decides whether the poll
reads the status those entities show.

So a channel whose probe timed out keeps its siren switch and its security
light and they can only ever read off, for the life of the entry. On a flood
light it is worse than it sounds: `is_flood_light_on` reads the `WhiteLight`
field of this same status, so the lamp's own entity stops following the lamp.

The handler is deliberately narrower than the others and stays that way.
`PROBE_REFUSED` is an HTTP response or a timeout, so a connection failure
still fails setup rather than being read as a camera without the feature, and
the HTTP half is #854: three AD410s that never finished setup because an RPC2
refusal of this status was fatal.

Reintroduced against the real source, with the tests that failed:

| reintroduced | caught by |
| --- | --- |
| the registry entry removed | `arrives_late`, `not_announced_again`, `let_go` |
| the good read not recorded as an answer | `arrives_late`, `not_announced_again` |
| the retry reading the flag its own probe just wrote | `not_announced_again` |
| the setup probe inlined again | `in_exactly_one_place` |
"""

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module

from .integration_source import source

ADDRESS = "10.0.0.88"
CHANNEL = 3
SERIAL = "AMR044032F4F06B33E_3"

# What a camera with the hardware answers: the state of its two outputs.
ANSWERED = {"status.WhiteLight": "Off"}


class _Client:
    """Answers the one read this probe makes, as the test dictates.

    `answers` is consumed in order and the last one repeats, so a test can say
    "silent, then it answers" without counting calls.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.reads = 0
        self.channels = []

    async def async_get_coaxial_control_io_status(self, channel=1):
        self.reads += 1
        self.channels.append(channel)
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
    coordinator._channel_number = CHANNEL + 1
    coordinator._address = ADDRESS
    coordinator._supports_coaxial_control = False
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "entry-1"})()
    coordinator.get_serial_number = lambda: SERIAL
    # Which channel the status is asked for is decided by these two, and this
    # file is not about that decision: test_coaxial_status_channel.py is.
    coordinator.uses_recorder_deterrence = lambda: False
    coordinator.is_ptz3e10x_t180 = lambda: False
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


def _refused(status=400):
    return ClientResponseError(None, (), status=status)


# --- silence is not an answer ------------------------------------------------


async def test_a_timed_out_probe_leaves_the_question_open():
    coordinator = _coordinator(TimeoutError())

    assert await coordinator._async_probe_coaxial_control() is False
    assert coordinator._supports_coaxial_control is False
    assert coordinator.probes_that_did_not_answer() == {"coaxial_control"}


async def test_a_device_that_refuses_is_not_asked_again(discoveries):
    """A 400 is the camera saying it has no such output, which is an answer and
    the half worth keeping. Driven from silence first, because a question
    nothing has ever asked is not pending either and would pass this
    vacuously."""
    coordinator = _coordinator(TimeoutError(), _refused())
    await coordinator._async_probe_coaxial_control()
    assert coordinator.probes_that_did_not_answer() == {"coaxial_control"}

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator._supports_coaxial_control is False
    assert coordinator.probes_that_did_not_answer() == set()

    reads = coordinator.client.reads
    await coordinator._async_retry_unanswered_capabilities()
    assert coordinator.client.reads == reads


async def test_an_answer_is_not_asked_for_twice():
    coordinator = _coordinator(ANSWERED)

    assert await coordinator._async_probe_coaxial_control() is True
    assert coordinator._supports_coaxial_control is True
    assert coordinator.probes_that_did_not_answer() == set()


# --- asked again, and the poll starts reading the status ---------------------


async def test_a_status_that_arrives_late_is_read_by_the_poll(discoveries):
    """Through the real registry, not a narrowed one: a test that monkeypatches
    `RETRYABLE_CAPABILITIES` down to this capability passes just as happily
    when the registry entry is deleted."""
    coordinator = _coordinator(TimeoutError(), ANSWERED)
    await coordinator._async_probe_coaxial_control()

    assert "coaxial_control" in coordinator_module.RETRYABLE_CAPABILITIES

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator._supports_coaxial_control is True
    assert coordinator.probes_that_did_not_answer() == set()
    assert discoveries == [("entry-1", SERIAL)]


async def test_a_second_answer_is_not_announced_again(discoveries):
    """A probe that answers on the retry has nothing left to discover, and a
    channel that keeps being rebuilt would re-read the device each time."""
    coordinator = _coordinator(TimeoutError(), ANSWERED)
    await coordinator._async_probe_coaxial_control()

    await coordinator._async_retry_unanswered_capabilities()
    await coordinator._async_retry_unanswered_capabilities()

    assert len(discoveries) == 1
    assert coordinator.client.reads == 2, (
        "once at setup and once on the retry. A third read would mean the "
        "answer was not recorded as one"
    )


async def test_a_channel_that_stays_silent_is_let_go(discoveries):
    coordinator = _coordinator(TimeoutError())
    await coordinator._async_probe_coaxial_control()
    reads_at_setup = coordinator.client.reads

    for _ in range(10):
        await coordinator._async_retry_unanswered_capabilities()

    assert (
        coordinator.client.reads - reads_at_setup
        == coordinator_module.MAX_CAPABILITY_RETRIES
    )
    assert discoveries == []


async def test_the_retry_asks_about_the_same_channel_setup_did(discoveries):
    """The probe takes the channel from `get_coaxial_status_channel` rather
    than being handed one, so the retry cannot ask about a different output
    from the one setup asked about."""
    coordinator = _coordinator(TimeoutError(), ANSWERED)
    await coordinator._async_probe_coaxial_control()

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.client.channels == [1, 1]


# --- one judgment, in one place ----------------------------------------------

#: Every module of the integration at once, through the helper that exists so a
#: scan like this does not become a coupling to which file holds the code.
_SOURCE = source()


def test_the_coaxial_judgment_is_made_in_exactly_one_place():
    """Setup and the retry share `_async_probe_coaxial_control`. Inlining it
    back into setup is the mutation this catches: the two copies would then be
    free to disagree about which failures are an answer, and this handler is
    the narrow one."""
    assert _SOURCE.count('_note_probe_refusal("coaxial_control"') == 1
    assert _SOURCE.count("self.client.async_get_coaxial_control_io_status(") == 2, (
        "once in the probe and once in the poll's _async_coaxial_status. A "
        "third call site means a second judgment"
    )
