"""An encoder that would not answer at setup is asked again.

The audio enable switch and the audio source select are built from
`_encode_audio_keys`, which setup filled once from `Encode` and nothing ever
refilled. Two different devices lost both entities for the life of the entry:

- one that **refused** the read, because `async_get_config` turns an HTTP 400,
  403 or 500 into `{}`, which is indistinguishable from a device that answered
  and has no audio. No refusal was recorded either, so the dump showed nothing.
- one that went **silent**, which was recorded under `encode` and then never
  retried, because that name was not in `RETRYABLE_CAPABILITIES`.

The loop closed itself: the poll reads `Encode` only `if self._encode_audio_keys`,
so setup's empty answer meant nothing asked again, ever.

Two changes, and they fix different halves rather than one thing twice.

`encode` joins the retry registry, which is what gets the entities back: a
silent probe is re-asked on a later poll, when the setup storm is over.

The probe also reads strictly, and that changes **only what the dump says**.
Leniently a refusal arrives as `{}` with no exception, so `_note_probe_answered`
pops the record and nothing anywhere shows that the device refused, or with
what status. Both readers leave a refusal un-retried, which is correct, so this
half buys the record and not the behaviour. Worth being exact about: the
obvious claim, that the strict read is what stops a refusal being mistaken for
a retry case, is wrong.

The gate moved into the probe rather than staying at the call site, because the
retry has to apply the same one. That is @rroller's third finding on #1201.

Reintroduced against the real source, with the tests that failed:

| reintroduced | caught by |
| --- | --- |
| the registry entry removed | `silence_is_asked_again`, `a_refusal_is_recorded...` |
| the lenient reader restored | `a_refusal_is_recorded_as_an_answer` |
| the good read not recorded as answered | `silence_is_asked_again` |
| the retry reading the flag its own probe wrote | `silence_is_asked_again` |
| the gate dropped | `a_channel_with_neither_platform_is_not_asked` |

`an_encoder_already_known_is_not_announced` catches none of those on its own,
and is kept because it pins the property the others only imply: a retry that
finds what it already had must not report a discovery, or every poll rebuilds
the platforms.
"""

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module

from .integration_source import source

CHANNEL = 2
PREFIX = "table.Encode[%d]." % CHANNEL

# What a camera with audio answers, as the flat shape the coordinator sees.
ANSWERED = {
    PREFIX + "MainFormat[0].AudioEnable": "true",
    PREFIX + "MainFormat[0].Audio.AudioSource": "Mic",
    PREFIX + "ExtraFormat[0].AudioEnable": "false",
    # Another channel's row, which must not be taken for this one's.
    "table.Encode[7].MainFormat[0].AudioEnable": "true",
    # A field of this channel that is not audio.
    PREFIX + "MainFormat[0].Video.Width": "1920",
}

# What a camera with no microphone answers: the table, without the audio keys.
NO_AUDIO = {PREFIX + "MainFormat[0].Video.Width": "1920"}


class _Client:
    """Answers the one read this probe makes, as the test dictates.

    `answers` is consumed in order and the last one repeats, so a test can say
    "silent, then it answers" without counting calls.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.reads = 0

    async def async_probe_encode(self):
        self.reads += 1
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def async_get_config(self, name):
        """The lenient sibling, modelled exactly as the real client has it.

        Without this the probe reverting to `async_get_config` would fail on a
        missing attribute, which proves nothing. With it, the revert fails on
        the behaviour that matters: a refusal arriving as `{}` rather than as
        an exception, so nothing is recorded and the device is never re-asked.
        """
        assert name == "Encode"
        try:
            return await self.async_probe_encode()
        except ClientResponseError:
            return {}


def _coordinator(*answers, options=None):
    """A coordinator carrying the probe, the retry machinery and nothing else."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Client(*answers)
    coordinator._channel = CHANNEL
    coordinator._channel_number = CHANNEL + 1
    coordinator._address = "10.0.0.4"
    coordinator._encode_audio_keys = ()
    coordinator._encode_audio_last_values = {}
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.hass = None
    coordinator.config_entry = type(
        "E", (), {"entry_id": "entry-1", "options": options or {}}
    )()
    coordinator.get_serial_number = lambda: "SERIAL1_%d" % CHANNEL
    return coordinator


@pytest.fixture
def discoveries(monkeypatch):
    """What the coordinator announced, captured where it looks the name up."""
    announced = []
    monkeypatch.setattr(
        coordinator_module,
        "async_notify_capability_discovered",
        lambda hass, entry_id, channel_key: announced.append((entry_id, channel_key)),
    )
    return announced


def _refused(status=400):
    return ClientResponseError(None, (), status=status)


# --- silence is not an answer -------------------------------------------------


async def test_a_timed_out_probe_leaves_the_question_open():
    coordinator = _coordinator(TimeoutError())

    assert await coordinator._async_probe_encode_audio() is False
    assert coordinator._encode_audio_keys == ()
    assert coordinator.probes_that_did_not_answer() == {"encode"}


async def test_silence_is_asked_again(discoveries):
    """The point of the file. The audio switch and the audio source select
    exist from the next poll rather than never."""
    coordinator = _coordinator(TimeoutError(), ANSWERED)
    await coordinator._async_probe_encode_audio()
    assert coordinator.probes_that_did_not_answer() == {"encode"}

    await coordinator._async_retry_unanswered_capabilities()

    assert sorted(coordinator._encode_audio_keys) == [
        PREFIX + "ExtraFormat[0].AudioEnable",
        PREFIX + "MainFormat[0].Audio.AudioSource",
        PREFIX + "MainFormat[0].AudioEnable",
    ]
    assert coordinator.probes_that_did_not_answer() == set()
    assert discoveries, "the platforms were never told to build the entities"


# --- a refusal is the device answering ----------------------------------------


async def test_a_refusal_is_recorded_as_an_answer(discoveries):
    """What the strict reader buys. Through `async_get_config` this came back
    as `{}` with no exception, so nothing was recorded and the two cases could
    not be told apart. Driven from silence first, because a question nothing
    ever asked is not pending either and would pass this vacuously."""
    coordinator = _coordinator(TimeoutError(), _refused())
    await coordinator._async_probe_encode_audio()
    assert coordinator.probes_that_did_not_answer() == {"encode"}

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.probes_that_did_not_answer() == set()
    assert coordinator._encode_audio_keys == ()

    # ⭐ The assertion the strict reader is actually for, and the only one here
    # that separates it from `async_get_config`. Leniently, the 400 came back
    # as `{}`, `_note_probe_answered` popped the record, and the dump showed
    # nothing at all about `encode` -- not that it was refused, not with what
    # status. Both readers stop the retry; only this one says why.
    assert coordinator._probe_refusals["encode"]["status"] == 400
    assert coordinator._probe_refusals["encode"]["answered"] is True

    reads = coordinator.client.reads
    await coordinator._async_retry_unanswered_capabilities()
    assert coordinator.client.reads == reads, "a device that answered was re-asked"


async def test_a_camera_with_no_microphone_is_answered_not_pending():
    """The empty-answer control. A table without the audio keys is a settled
    no, and must not look like a refusal or be chased."""
    coordinator = _coordinator(NO_AUDIO)

    assert await coordinator._async_probe_encode_audio() is False
    assert coordinator._encode_audio_keys == ()
    assert coordinator.probes_that_did_not_answer() == set()


async def test_another_channels_row_is_not_this_channels_audio():
    """The answer is host wide and this channel's row is the capability."""
    coordinator = _coordinator(ANSWERED)

    await coordinator._async_probe_encode_audio()

    assert all(key.startswith(PREFIX) for key in coordinator._encode_audio_keys)
    assert coordinator._encode_audio_last_values == {
        key: ANSWERED[key] for key in coordinator._encode_audio_keys
    }


# --- asked once, and only where something reads it ----------------------------


async def test_an_encoder_already_known_is_not_announced():
    """A retry that finds what it already had is not a discovery, or every
    poll would rebuild the platforms."""
    coordinator = _coordinator(ANSWERED)
    await coordinator._async_probe_encode_audio()

    assert await coordinator._async_retry_encode() is False


async def test_a_channel_with_neither_platform_is_not_asked():
    """The gate, applied inside the probe so the retry gets it too. Neither
    entity exists with both platforms off, so the read buys nothing."""
    coordinator = _coordinator(ANSWERED, options={"switch": False, "select": False})

    assert await coordinator._async_probe_encode_audio() is False
    assert coordinator.client.reads == 0


async def test_one_platform_is_enough():
    coordinator = _coordinator(ANSWERED, options={"switch": False})

    assert await coordinator._async_probe_encode_audio() is True
    assert coordinator.client.reads == 1


# --- one gate, one place ------------------------------------------------------


def test_the_encoder_is_discovered_in_exactly_one_place():
    """@rroller on #1201: a gate with two call sites is how the two come to
    disagree. Setup and the retry go through the one named probe."""
    text = source()

    assert text.count("async_probe_encode(") == 2
    assert "await self._async_probe_encode_audio()" in text
