"""Turning audio on updates what the integration thinks the stream carries.

`_stream_codecs` was written once, by `_async_read_stream_codecs` during
initialisation, and never again. The encoder audio switch writes
`Encode[c].MainFormat[i].AudioEnable`, the poll reads the new value back, and
the camera entity went on reporting `audio: False` from the answer read when
the entry loaded. Two things followed from that (#997 review):

* `get_stream_has_audio` was stale until the entry was reloaded;
* the `webrtc_cannot_play` card stayed up after the thing it complains about
  had been fixed, since H.265 plus no audio is the pair it reports and the
  audio half never changed.

The fix costs no request. The audio poll already reads the whole `Encode`
table every cycle and `parse_stream_codecs` takes exactly that answer, so the
same response refreshes the codecs on the way past.

An empty answer must keep what is held. `async_get_config` returns `{}` for a
refusal as well as for a device with nothing to say, so discarding the codecs
on a momentary refusal would report the stream as carrying nothing and raise
the card rather than clear it.

Reintroduced one at a time against the real source:

| reintroduced | caught by |
| --- | --- |
| codecs read only at setup | `the_poll_updates_what_the_stream_carries` |
| an empty read overwriting them | `a_refused_read_keeps_the_last_codecs` |
| the card left alone by the poll | `the_card_goes_when_the_audio_arrives` |
| the card raise unguarded | `a_card_that_cannot_be_raised_does_not_fail_the_poll` |
| a partial answer assigned wholesale | `a_partial_answer_keeps_the_fields_it_did_not_mention` |
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import dahua_utils

ADDRESS = "192.168.1.90"
CHANNEL = 0

# An H.265 main stream with its audio off, which is the pair the card reports.
SILENT = {
    "table.Encode[0].MainFormat[0].Video.Compression": "H.265",
    "table.Encode[0].MainFormat[0].AudioEnable": "false",
}
# The same table after the switch turned audio on.
AUDIBLE = dict(SILENT, **{"table.Encode[0].MainFormat[0].AudioEnable": "true"})


class _Device:
    def __init__(self, answers):
        self._answers = list(answers)

    async def async_get_config(self, name):
        assert name == "Encode"
        answer = self._answers[0] if len(self._answers) == 1 else self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


def _coordinator(answers=(AUDIBLE,)):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Device(answers)
    coordinator._channel = CHANNEL
    coordinator._address = ADDRESS
    coordinator._encode_audio_keys = ("table.Encode[0].MainFormat[0].AudioEnable",)
    coordinator._encode_audio_last_values = {}
    coordinator._stream_codecs = dahua_utils.parse_stream_codecs(SILENT, CHANNEL)
    coordinator.hass = None
    coordinator.cards = []
    coordinator._async_report_unplayable_stream = lambda: coordinator.cards.append(
        dahua_utils.webrtc_cannot_play(coordinator._stream_codecs)
    )
    return coordinator


def test_the_fixture_starts_where_the_bug_was():
    """The starting value is the wrong answer, not the right one, so a test
    asserting the right one cannot pass by never running the code."""
    coordinator = _coordinator()

    assert coordinator.get_stream_has_audio(0) is False
    assert dahua_utils.webrtc_cannot_play(coordinator._stream_codecs) is True


async def test_the_poll_updates_what_the_stream_carries():
    coordinator = _coordinator()

    await coordinator._async_get_encode_config()

    assert coordinator.get_stream_has_audio(0) is True


async def test_the_codec_comes_from_the_same_answer():
    """Not just the audio half. A channel moved to H.264 on the device is the
    other way this metadata goes stale."""
    coordinator = _coordinator(
        answers=(
            {
                "table.Encode[0].MainFormat[0].Video.Compression": "H.264",
                "table.Encode[0].MainFormat[0].AudioEnable": "false",
            },
        )
    )

    await coordinator._async_get_encode_config()

    assert coordinator.get_stream_codec(0) == "H.264"


async def test_the_card_goes_when_the_audio_arrives():
    """The card is H.265 and no audio together. Once audio is on the pair is
    broken and the card has to be reconsidered, not left standing."""
    coordinator = _coordinator()

    await coordinator._async_get_encode_config()

    assert coordinator.cards == [False]


async def test_a_refused_read_keeps_the_last_codecs():
    """`async_get_config` answers {} for a refusal too. Wiping the codecs would
    report a stream carrying nothing and raise the card it had just cleared."""
    coordinator = _coordinator(answers=(AUDIBLE, {}))
    await coordinator._async_get_encode_config()

    await coordinator._async_get_encode_config()

    assert coordinator.get_stream_has_audio(0) is True
    assert coordinator.get_stream_codec(0) == "H.265"


async def test_an_empty_read_does_not_touch_the_card_either():
    coordinator = _coordinator(answers=({},))

    await coordinator._async_get_encode_config()

    assert coordinator.cards == []


async def test_the_audio_values_are_still_returned():
    """The method's own job, which this must not have displaced."""
    coordinator = _coordinator()

    values = await coordinator._async_get_encode_config()

    assert values == {"table.Encode[0].MainFormat[0].AudioEnable": "true"}


async def test_a_card_that_cannot_be_raised_does_not_fail_the_poll():
    """The fan-out has no `return_exceptions`, so anything raised here takes
    every entity on the channel down with it. The codecs are what the camera
    attributes read; the card is the least important thing in the method."""
    coordinator = _coordinator()

    def explode():
        raise RuntimeError("no issue registry")

    coordinator._async_report_unplayable_stream = explode

    values = await coordinator._async_get_encode_config()

    assert values == {"table.Encode[0].MainFormat[0].AudioEnable": "true"}
    assert coordinator.get_stream_has_audio(0) is True


async def test_a_read_that_raises_still_does_not_fail_the_poll():
    """The pre-existing guard, kept honest: PROBE_FAILED is caught and the
    previous values are returned."""
    coordinator = _coordinator(answers=(AUDIBLE, TimeoutError()))
    await coordinator._async_get_encode_config()

    values = await coordinator._async_get_encode_config()

    assert values == {"table.Encode[0].MainFormat[0].AudioEnable": "true"}
    assert coordinator.get_stream_has_audio(0) is True


# --- a partial answer ---------------------------------------------------------
#
# ⭐ @rroller on the #997 review, after the whole-channel guard went in: the
# guard asks "did this answer mention the channel", which is one question
# short of "did it mention this FIELD of this stream". `parse_stream_codecs`
# seeds every field it did not see with None, so a reply carrying only
# `AudioEnable` produces a nonempty record the guard accepts, and assigning it
# erases a codec the channel really has.

# What a device sends when only the audio flag came back for this stream.
AUDIO_ONLY = {"table.Encode[0].MainFormat[0].AudioEnable": "false"}
CODEC_ONLY = {"table.Encode[0].MainFormat[0].Video.Compression": "H.264"}


def test_the_parser_really_does_seed_the_missing_field():
    """The premise, asserted rather than assumed. If this ever stops being
    true the merge below is solving a problem that has gone away, and the
    tests after it would pass for the wrong reason."""
    assert dahua_utils.parse_stream_codecs(AUDIO_ONLY, CHANNEL) == {
        0: {"codec": None, "audio": False}
    }


async def test_a_partial_answer_keeps_the_fields_it_did_not_mention():
    """⭐ The reported case: start knowing H.265 with audio off, receive only
    this channel's `AudioEnable`, and the codec must survive."""
    coordinator = _coordinator(answers=(AUDIO_ONLY,))

    await coordinator._async_get_encode_config()

    assert coordinator.get_stream_codec(0) == "H.265"
    assert coordinator.get_stream_has_audio(0) is False


async def test_a_partial_answer_does_not_move_the_card():
    """And the consequence rroller named. Clearing `webrtc_cannot_play` on an
    answer that said nothing about the codec is deciding on an erasure."""
    coordinator = _coordinator(answers=(AUDIO_ONLY,))

    await coordinator._async_get_encode_config()

    assert coordinator.cards == [
        True
    ], "H.265 and no audio, both still true, so the card stands"


async def test_a_partial_answer_still_applies_what_it_did_say():
    """The merge must not become "ignore partial answers". A codec-only reply
    is how a channel moved to H.264 on the device is noticed."""
    coordinator = _coordinator(answers=(CODEC_ONLY,))

    await coordinator._async_get_encode_config()

    assert coordinator.get_stream_codec(0) == "H.264"
    assert coordinator.get_stream_has_audio(0) is False, "from before, untouched"
    assert coordinator.cards == [False], "H.264 is playable, so the card goes"


async def test_a_stream_the_answer_did_not_mention_is_untouched():
    """Per stream as well as per field. A reply about the main stream alone
    must not erase what is known about the sub stream."""
    coordinator = _coordinator(answers=(AUDIO_ONLY,))
    coordinator._stream_codecs = dahua_utils.parse_stream_codecs(
        dict(
            SILENT,
            **{
                "table.Encode[0].ExtraFormat[0].Video.Compression": "H.264",
                "table.Encode[0].ExtraFormat[0].AudioEnable": "true",
            },
        ),
        CHANNEL,
    )

    await coordinator._async_get_encode_config()

    assert coordinator.get_stream_codec(1) == "H.264"
    assert coordinator.get_stream_has_audio(1) is True


async def test_a_stream_that_is_new_in_the_answer_is_added():
    """The other direction: a sub stream that was not there before arrives
    whole, rather than being dropped for not already being known."""
    coordinator = _coordinator(
        answers=(
            {
                "table.Encode[0].ExtraFormat[0].Video.Compression": "H.264",
                "table.Encode[0].ExtraFormat[0].AudioEnable": "true",
            },
        )
    )

    await coordinator._async_get_encode_config()

    assert coordinator.get_stream_codec(1) == "H.264"
    assert coordinator.get_stream_has_audio(1) is True
    assert coordinator.get_stream_codec(0) == "H.265", "the main stream is untouched"
