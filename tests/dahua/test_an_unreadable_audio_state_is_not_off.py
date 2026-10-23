"""Two ways #997 reported a state it had not learned, both from rroller's review.

**An unreadable `AudioEnable` read as off.** The switch compared the value to
the literal `true` and called everything else False, so an empty field, or one
carrying something unexpected, reported the audio as disabled. A switch is
what automations act on, and that is the one wrong answer that makes them act:
the device may have had audio on the whole time.

**An `Encode` answer about another channel erased this one's codecs.** The
poll guarded on the table being nonempty, which does not establish that this
channel is in it. A recorder can answer with channel 1's rows and none of
channel 0's; `parse_stream_codecs` returns {} for a channel it did not find,
the assignment replaced what channel 0 really had, and
`_async_report_unplayable_stream` then re-decided on that emptiness and
cleared the `webrtc_cannot_play` card without having learned anything about
the stream it is about.

Both are the same mistake in two places: treating "I could not read it" as
"I read that there is nothing".
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.switch import DahuaEncodeAudioSwitch

KEY = "table.Encode[0].MainFormat[0].AudioEnable"
MAIN = "table.Encode[{0}].MainFormat[0]"


def _switch(value, present=True):
    entity = object.__new__(DahuaEncodeAudioSwitch)
    entity._key = KEY
    entity._coordinator = type("C", (), {"data": {KEY: value} if present else {}})()
    return entity


# --- the switch ---------------------------------------------------------------


def test_a_device_saying_true_is_on():
    assert _switch("true").is_on is True


def test_a_device_saying_false_is_off():
    """The negative control. Unknown must not swallow a real false, or the
    switch stops being able to show the state it is most often in."""
    assert _switch("false").is_on is False


@pytest.mark.parametrize("value", ["TRUE", "True"])
def test_the_comparison_is_not_case_sensitive(value):
    assert _switch(value).is_on is True


@pytest.mark.parametrize("value", [" true ", "false\n"])
def test_surrounding_whitespace_does_not_change_the_answer(value):
    """CGI answers `key=value` text and a trailing carriage return is ordinary
    on this wire, so a value that differs only in whitespace is still the
    device saying what it said."""
    assert _switch(value).is_on is (value.strip() == "true")


@pytest.mark.parametrize("value", ["", "   ", "unknown", "1", "0", "yes", "null"])
def test_anything_else_is_unknown_rather_than_off(value):
    """The reported bug. Every one of these used to read as off."""
    assert _switch(value).is_on is None


def test_a_missing_key_is_unknown():
    assert _switch(None, present=False).is_on is None


def test_a_value_that_is_not_a_string_does_not_raise():
    """`is_on` is read on every state write, so it may not raise whatever the
    poll put in the dict."""
    assert _switch(object()).is_on is None


# --- the codecs ---------------------------------------------------------------


def _coordinator(channel=0, held=None):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._address = "10.0.0.5"
    c._stream_codecs = held if held is not None else {}
    c._async_report_unplayable_stream = lambda: None
    return c


def _encode_for(channel, codec="H.265", audio="false"):
    return {
        MAIN.format(channel) + ".Video.Compression": codec,
        MAIN.format(channel) + ".AudioEnable": audio,
    }


def test_an_answer_about_this_channel_is_taken():
    c = _coordinator(channel=0)

    c._async_refresh_stream_codecs(_encode_for(0, "H.264", "true"))

    assert c._stream_codecs[0] == {"codec": "H.264", "audio": True}


def test_an_answer_about_only_another_channel_changes_nothing():
    """rroller's case. Channel 0 holds real metadata and the recorder answers
    with channel 1 only."""
    held = {0: {"codec": "H.265", "audio": False}}
    c = _coordinator(channel=0, held=held)

    c._async_refresh_stream_codecs(_encode_for(1))

    assert c._stream_codecs == held


def test_the_card_is_not_re_decided_on_an_answer_about_another_channel():
    """The second half, and the part that was user-visible: the verdict was
    recomputed from the emptiness and the card cleared."""
    held = {0: {"codec": "H.265", "audio": False}}
    c = _coordinator(channel=0, held=held)
    reported = []
    c._async_report_unplayable_stream = lambda: reported.append(True)

    c._async_refresh_stream_codecs(_encode_for(1))

    assert reported == [], "re-decided the card having learned nothing"


def test_an_empty_answer_still_changes_nothing():
    """Unchanged, and the rule the fix above follows: `async_get_config`
    returns {} for a refusal as well as for a device with nothing to say."""
    held = {0: {"codec": "H.265", "audio": False}}
    c = _coordinator(channel=0, held=held)

    c._async_refresh_stream_codecs({})

    assert c._stream_codecs == held


def test_a_real_answer_still_re_decides_the_card():
    """The negative control for the guard. If nothing re-decided, the card
    could never clear when the stream really was fixed, which is the bug the
    re-decision was added for."""
    c = _coordinator(channel=0, held={0: {"codec": "H.265", "audio": False}})
    reported = []
    c._async_report_unplayable_stream = lambda: reported.append(True)

    c._async_refresh_stream_codecs(_encode_for(0, "H.264", "true"))

    assert reported == [True]


def test_each_channel_reads_its_own_rows_from_a_shared_answer():
    """A recorder answers for several channels at once, so the guard must not
    make a channel ignore an answer that does mention it.

    Both results are keyed `0` because `parse_stream_codecs` keys by **stream**
    index and MainFormat is stream 0 on every channel. The channel is what
    selects the rows, not what the answer is filed under.
    """
    both = {**_encode_for(0, "H.264", "true"), **_encode_for(1, "H.265", "false")}
    zero, one = _coordinator(channel=0), _coordinator(channel=1)

    zero._async_refresh_stream_codecs(both)
    one._async_refresh_stream_codecs(both)

    assert zero._stream_codecs[0] == {"codec": "H.264", "audio": True}
    assert one._stream_codecs[0] == {"codec": "H.265", "audio": False}
