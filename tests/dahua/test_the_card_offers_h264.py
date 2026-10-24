"""The unplayable-stream card says less than it used to, and now offers a fix.

#1048 raised a card for H.265 with no audio track and asserted that Home
Assistant "cannot play that combination in a browser". The detection is right
and still is. The assertion was one step further than the integration can see.

Re-measured on the same DHI-NVR5464-16P-EI on 2026-10-24, with the device in
exactly the state the card describes:

    dahua_<serial>_2_Main    (carded: H.265, AudioEnable false)
      (plain)  -> codec_name:hevc
      ?audio   -> DESCRIBE failed: 404 Not Found      <- still broken
      ?video   -> codec_name:hevc
    dahua_<serial>_1_Main    (control: H.265, AudioEnable true)
      ?audio   -> codec_name:pcm_alaw                 <- works

So the 404 #1048 measured is unchanged. What changed is that it is no longer
reached. Each stream has two producers, the direct RTSP source and the
`ffmpeg:...#audio=opus` fallback. A viewer that can decode H.265 is served by
the first and never touches the second; a viewer that cannot falls through to
the fallback, which asks for the audio-only view, and dies on that 404. The
owner of the recorder above opened a carded channel and it played.

Which kind of viewer is watching is the one thing the server cannot see. So the
card now reports the condition and names what it depends on, and the fix it used
to describe in prose is offered as a flow.

**The write itself is NOT hardware-verified.** The round trip was blocked by
this environment's sandbox before it could run, so `async_set_stream_compression`
is proved here against the URL it builds and the refusal it reports, and is
unproven against a real encoder. That is why the flow reads back rather than
trusting a 200, and why the PR says so.
"""

from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua.const import DOMAIN
from custom_components.dahua.dahua_utils import (
    parse_stream_compression_options,
    stream_can_be_set_to,
)
from custom_components.dahua.repairs import (
    SwitchStreamToH264RepairFlow,
    async_create_fix_flow,
)

ADDRESS = "10.0.0.1"

# Trimmed from a real `encode.cgi?action=getConfigCaps` reply. One call answers
# for every channel, which is why the flow reads it once for the whole host.
MEASURED_CAPS = {
    "caps[0].MainFormat[0].Video.CompressionTypes": "H.264,H.265",
    "caps[0].MainFormat[0].Video.BitRateOptions": "1024,8704",
    "caps[0].ExtraFormat[0].Video.CompressionTypes": "H.264,MJPG,H.265",
    "caps[0].ExtraFormat[0].Audio.CompressionTypes": "PCM,G.711A,G.711Mu,G.726,AAC",
    # A second channel, whose main stream will not take H.264 at all.
    "caps[2].MainFormat[0].Video.CompressionTypes": "H.265",
    "caps[2].ExtraFormat[0].Video.CompressionTypes": "MJPG",
    # A third, which is offerable.
    "caps[3].MainFormat[0].Video.CompressionTypes": "H.264,H.265",
}


# --- reading what the device will accept -------------------------------------


def test_the_options_are_read_per_stream():
    options = parse_stream_compression_options(MEASURED_CAPS, 0)

    assert options[0] == ["H.264", "H.265"]
    assert options[1] == ["H.264", "MJPG", "H.265"]


def test_the_indexes_match_the_ones_the_codecs_use():
    """`parse_stream_codecs` and this must agree, or the flow would read one
    stream's options and write another's codec. MainFormat[0] is 0 and
    ExtraFormat[n] is n+1, which is what every camera entity already uses."""
    assert sorted(parse_stream_compression_options(MEASURED_CAPS, 0)) == [0, 1]


def test_another_channels_options_are_not_mixed_in():
    """One reply carries every channel, so the per-channel read has to filter."""
    options = parse_stream_compression_options(MEASURED_CAPS, 2)

    assert options[0] == ["H.265"], "channel 2's own, not channel 0's"
    assert options[1] == ["MJPG"]


def test_only_the_video_option_list_is_read():
    """The same block carries `Audio.CompressionTypes` and `BitRateOptions`.
    Reading either as a codec list would offer H.264 on the strength of an
    audio field."""
    options = parse_stream_compression_options(MEASURED_CAPS, 0)

    assert "PCM" not in options[1]
    assert "1024" not in options[0]


def test_a_channel_the_answer_did_not_mention_has_no_options():
    assert parse_stream_compression_options(MEASURED_CAPS, 9) == {}


def test_a_refused_read_is_not_a_table():
    """`async_get_encode_caps` returns {} when the device has no such endpoint,
    which must not read as "this stream supports nothing in particular"."""
    assert parse_stream_compression_options({}, 0) == {}
    assert parse_stream_compression_options(None, 0) == {}


def test_absence_is_not_permission():
    """The whole point of asking. A channel with no answer, and a stream whose
    list does not name the codec, are both refusals: writing a codec the device
    never offered is how a setConfig answers 200 with the body `Error`, or
    stores it and drives nothing (#647)."""
    options = parse_stream_compression_options(MEASURED_CAPS, 2)

    assert stream_can_be_set_to(options, 0, "H.264") is False
    assert stream_can_be_set_to(options, 0, "H.265") is True
    assert stream_can_be_set_to({}, 0, "H.264") is False
    assert stream_can_be_set_to(None, 0, "H.264") is False


# --- the flow ----------------------------------------------------------------


DEVICE = "{0}:80".format(ADDRESS)


class _Client:
    def __init__(self, caps=None, refuse=(), device=DEVICE):
        self.caps = MEASURED_CAPS if caps is None else caps
        self.refuse = set(refuse)
        self.device_key = device
        self.caps_reads = 0
        self.writes = []

    async def async_get_encode_caps(self):
        self.caps_reads += 1
        return self.caps

    async def async_set_stream_compression(self, channel, stream_index, value):
        self.writes.append((channel, stream_index, value))
        if channel in self.refuse:
            raise RuntimeError("declined")
        return True


class _Coordinator:
    """A channel that is behind the card, with a codec that follows the write.

    `stuck` models the device this flow exists to be careful about: it accepts
    the write, answers 200, and goes on sending what it was sending.
    """

    def __init__(self, channel, carded, client, stuck=False, silent=False):
        self._channel = channel
        self._carded = carded
        self.client = client
        self.stuck = stuck
        self.silent = silent
        self.codec = "H.265"
        self.rechecks = 0

    def channels_that_cannot_be_played(self):
        return set(self._carded)

    def get_stream_codec(self, index):
        return self.codec if index == 0 else "H.264"

    async def async_recheck_stream_codecs(self):
        self.rechecks += 1
        if self.silent:
            # The device answered, but with nothing about this channel. The
            # real coordinator keeps what it holds and reports False.
            return False
        if not self.stuck and (self._channel, 0, "H.264") in self.client.writes:
            self.codec = "H.264"
        return True


def _entry(hass, coordinators, address=ADDRESS, port="80"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Recorder",
        unique_id="SERIAL",
        data={
            "username": "u",
            "password": "p",
            "address": address,
            "port": port,
            "rtsp_port": "554",
            "channel": 0,
            "name": "Recorder",
        },
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = coordinators
    return entry


def _setup(hass, carded=(0, 2, 3), refuse=(), stuck=(), silent=()):
    client = _Client(refuse=refuse)
    coordinators = {
        channel: _Coordinator(
            channel,
            carded,
            client,
            stuck=channel in stuck,
            silent=channel in silent,
        )
        for channel in sorted(set(carded) | {0})
    }
    _entry(hass, coordinators)
    return client, coordinators


def _flow(hass, device=DEVICE):
    flow = SwitchStreamToH264RepairFlow({"address": ADDRESS, "device": device})
    flow.hass = hass
    return flow


def _raise_card(hass, device=DEVICE):
    ir.async_create_issue(
        hass,
        DOMAIN,
        "webrtc_cannot_play_{0}".format(device),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="webrtc_cannot_play",
        data={"address": ADDRESS, "device": device},
    )


def _issue(hass, device=DEVICE):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, "webrtc_cannot_play_{0}".format(device)
    )


async def test_the_card_gets_this_flow(hass):
    flow = await async_create_fix_flow(
        hass,
        "webrtc_cannot_play_{0}".format(DEVICE),
        {"address": ADDRESS, "device": DEVICE},
    )

    assert isinstance(flow, SwitchStreamToH264RepairFlow)


async def test_only_channels_that_offer_h264_are_listed(hass):
    """Channel 2's main stream offers H.265 alone, so it cannot be moved and
    must not be offered. Offering it would be a write the device declines."""
    _setup(hass)

    result = await _flow(hass).async_step_confirm()

    assert result["type"] == "form"
    listed = result["data_schema"].schema["channels"].options
    assert sorted(listed) == ["0", "3"], "channel 2 cannot take H.264"


async def test_the_form_comes_before_any_write(hass):
    client, _ = _setup(hass)

    await _flow(hass).async_step_confirm()

    assert client.writes == []


async def test_the_capability_table_is_read_once_for_the_host(hass):
    """It is 44 KB and answers for every channel at once. Reading it per
    channel would be the same reply twelve times on a recorder."""
    client, _ = _setup(hass)

    await _flow(hass).async_step_confirm()

    assert client.caps_reads == 1


async def test_only_the_ticked_channels_are_written(hass):
    """The main stream is what the device records, so this is never all of
    them just because the card named them all."""
    client, _ = _setup(hass)

    await _flow(hass).async_step_confirm({"channels": ["3"]})

    assert client.writes == [(3, 0, "H.264")]


async def test_the_main_stream_is_the_one_written(hass):
    """The card judges stream 0 and this writes stream 0. A sub stream moving
    instead would clear nothing and change a stream nobody complained about."""
    client, _ = _setup(hass)

    await _flow(hass).async_step_confirm({"channels": ["0", "3"]})

    assert [(c, i) for c, i, _ in client.writes] == [(0, 0), (3, 0)]


async def test_a_channel_that_was_not_offered_is_not_written(hass):
    """Submitting a channel the form never listed, which is what a replayed or
    hand-built submission looks like."""
    client, _ = _setup(hass)

    await _flow(hass).async_step_confirm({"channels": ["2", "3"]})

    assert client.writes == [(3, 0, "H.264")]


async def test_ticking_nothing_changes_nothing(hass):
    client, _ = _setup(hass)

    result = await _flow(hass).async_step_confirm({"channels": []})

    assert client.writes == []
    assert result["reason"] == "nothing_to_change"


async def test_the_codecs_are_read_back_after_the_write(hass):
    """A Dahua write answering 200 is not the stream having changed."""
    _, coordinators = _setup(hass)

    await _flow(hass).async_step_confirm({"channels": ["3"]})

    assert coordinators[3].rechecks == 1


async def test_a_device_that_accepts_and_ignores_is_reported(hass):
    """⭐ The failure this flow is shaped around. The device answers OK and
    keeps sending H.265, which this family really does (#647). Judging on the
    200 would tell the user it worked and clear a card that should stand."""
    _, coordinators = _setup(hass, stuck=(3,))

    result = await _flow(hass).async_step_confirm({"channels": ["3"]})

    assert result["reason"] == "write_refused"
    assert coordinators[3].codec == "H.265"


async def test_a_write_that_raises_is_reported_too(hass):
    _, _ = _setup(hass, refuse=(3,))

    result = await _flow(hass).async_step_confirm({"channels": ["3"]})

    assert result["reason"] == "write_refused"


async def test_one_refusal_does_not_stop_the_others(hass):
    """Three ticked and one refusing is still two channels fixed."""
    client, coordinators = _setup(hass, refuse=(0,))

    result = await _flow(hass).async_step_confirm({"channels": ["0", "3"]})

    assert (3, 0, "H.264") in client.writes
    assert coordinators[3].codec == "H.264"
    assert result["reason"] == "write_refused", "but it still says so"


async def test_a_clean_run_finishes(hass):
    _, coordinators = _setup(hass)

    result = await _flow(hass).async_step_confirm({"channels": ["0", "3"]})

    assert result["type"] == "create_entry"
    assert coordinators[0].codec == "H.264"


async def test_a_host_that_is_no_longer_set_up_aborts(hass):
    result = await _flow(hass).async_step_confirm()

    assert result["reason"] == "not_configured"


async def test_an_unloaded_entry_is_not_reached_into(hass):
    """`runtime_data` is gone once an entry unloads, and a flow that ran
    against a half-torn-down entry would write to a client being closed."""
    client, coordinators = _setup(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)

    result = await _flow(hass).async_step_confirm()

    assert result["reason"] == "not_configured"
    assert client.caps_reads == 0


async def test_a_card_nothing_is_behind_is_removed(hass):
    """Recomputed at the button, like the leftover-rows flow. Somebody who
    fixed every channel on the device itself should not be left holding a card
    whose button does nothing."""
    _raise_card(hass)
    _setup(hass, carded=())

    result = await _flow(hass).async_step_confirm()

    assert result["reason"] == "nothing_to_change"
    assert _issue(hass) is None


async def test_a_card_no_channel_can_act_on_is_removed(hass):
    """Every carded channel offers H.265 only. The condition is real and the
    device will not move, so holding a card with an inert button open is worse
    than saying so."""
    _raise_card(hass)
    _setup(hass, carded=(2,))

    result = await _flow(hass).async_step_confirm()

    assert result["reason"] == "nothing_to_change"
    assert _issue(hass) is None


async def test_another_hosts_channels_are_left_alone(hass):
    """The card is per address and so is the flow."""
    client, _ = _setup(hass)
    other_client = _Client(device="10.0.0.2:80")
    _entry(
        hass,
        {0: _Coordinator(0, (0,), other_client)},
        address="10.0.0.2",
    )

    await _flow(hass).async_step_confirm({"channels": ["0"]})

    assert other_client.writes == []


# --- the two client calls ----------------------------------------------------


def _client():
    from unittest.mock import AsyncMock

    from custom_components.dahua.client import DahuaClient

    client = object.__new__(DahuaClient)
    client.get = AsyncMock(return_value={})
    return client


async def test_the_caps_read_asks_the_encoder_not_the_config_manager():
    """`configManager.getConfig&name=Encode` says what a stream IS. Only
    `encode.cgi?action=getConfigCaps` says what it may be set TO, and reading
    the wrong one would make every channel look like it accepts anything."""
    client = _client()

    await client.async_get_encode_caps()

    assert client.get.await_args.args[0] == "/cgi-bin/encode.cgi?action=getConfigCaps"


async def test_the_caps_read_asks_for_no_channel():
    """One reply covers the whole host. A channel argument here would be the
    same 44 KB once per channel on a recorder."""
    client = _client()

    await client.async_get_encode_caps()

    assert "channel" not in client.get.await_args.args[0]


async def test_a_device_with_no_such_endpoint_has_nothing_to_offer():
    import aiohttp
    from unittest.mock import AsyncMock

    client = _client()
    client.get = AsyncMock(
        side_effect=aiohttp.ClientResponseError(None, None, status=404)
    )

    assert await client.async_get_encode_caps() == {}


async def test_the_main_stream_write_names_mainformat():
    client = _client()

    await client.async_set_stream_compression(3, 0, "H.264")

    assert client.get.await_args.args[0] == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Encode[3].MainFormat[0].Video.Compression=H.264"
    )


async def test_a_sub_stream_write_shifts_the_index_back():
    """Stream n is ExtraFormat[n-1], the same convention the codec parser uses.
    Off by one here writes a different stream and reads back as a refusal."""
    client = _client()

    await client.async_set_stream_compression(3, 2, "H.264")

    assert client.get.await_args.args[0] == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Encode[3].ExtraFormat[1].Video.Compression=H.264"
    )


async def test_the_write_is_verified():
    """`verify_ok` is what turns a 200 carrying the body `Error` into a
    failure. Without it a declined write is indistinguishable from a done one."""
    client = _client()

    await client.async_set_stream_compression(3, 0, "H.264")

    assert client.get.await_args.args[1] is True


async def test_the_write_names_one_field():
    """A whole-table setConfig is refused for size on at least one recorder,
    and naming any key the table lacks makes the device decline the lot."""
    client = _client()

    await client.async_set_stream_compression(3, 0, "H.264")

    assert client.get.await_args.args[0].count("=") == 2, "action, and the one field"


# --- the card and its flow have to agree about the text ----------------------


def _card_strings():
    import io
    import json
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "custom_components"
        / "dahua"
        / "translations"
        / "en.json"
    )
    return json.load(io.open(path, encoding="utf-8"))["issues"]["webrtc_cannot_play"]


def test_a_fixable_card_carries_no_description_of_its_own():
    """Home Assistant treats `description` and `fix_flow` as mutually exclusive
    on an issue, and hassfest fails the whole integration when both are there:

        [ERROR] [TRANSLATIONS] Invalid translations/en.json: two or more values
        in the same group of exclusion 'fixable' at
        'issues.webrtc_cannot_play.<fixable>'

    That is a CI failure for every other PR too, so it is pinned here rather
    than left to the next person to rediscover."""
    card = _card_strings()

    assert "fix_flow" in card
    assert "description" not in card
    assert card["title"], "the title is all the card itself shows"


async def test_the_form_supplies_every_placeholder_its_text_uses(hass):
    """Derived from the string, not a copy of it. A placeholder the step
    description names and the flow does not supply renders as a literal
    `{channels}` on the user's screen, which no other test would catch."""
    import re

    _setup(hass)
    description = _card_strings()["fix_flow"]["step"]["confirm"]["description"]
    needed = set(re.findall(r"\{(\w+)\}", description))

    result = await _flow(hass).async_step_confirm()

    assert needed, "the description should be using placeholders at all"
    assert needed <= set(result["description_placeholders"])


async def test_the_text_names_every_carded_channel_not_just_the_fixable_ones(hass):
    """Channel 2 cannot take H.264, so it is not in the picker. It is still one
    of the channels the condition is about, and leaving it out of the text
    would describe the device wrongly."""
    _setup(hass, carded=(0, 2, 3))

    result = await _flow(hass).async_step_confirm()

    assert result["description_placeholders"]["channels"] == "0, 2, 3"
    assert sorted(result["data_schema"].schema["channels"].options) == ["0", "3"]


# --- the two cases @rroller reproduced on #1127 ------------------------------


OTHER_DEVICE = "{0}:8080".format(ADDRESS)


def _two_boxes_on_one_address(hass):
    """One IP, two Dahua boxes, which a bridge forwarding 80 and 8080 gives you.

    Port 80 has the carded H.265 channel 0. Port 8080 has a channel 0 that is
    already fine. Both call it channel 0, which is the whole trap.
    """
    carded_client = _Client()
    carded = {0: _Coordinator(0, (0,), carded_client)}
    _entry(hass, carded)

    innocent_client = _Client(device=OTHER_DEVICE)
    innocent = {0: _Coordinator(0, (), innocent_client)}
    innocent[0].codec = "H.264"
    _entry(hass, innocent, port="8080")
    return carded_client, carded, innocent_client, innocent


async def test_two_boxes_on_one_address_are_not_confused(hass):
    """⭐ @rroller on #1127. `_entries_for_address` is about the network path,
    and merging its entries let one box's channel 0 overwrite the other's. He
    confirmed the repair for the port 80 recorder writing H.264 to the port
    8080 one, reporting success, and clearing the warning while the recorder
    it was about was still sending H.265."""
    carded_client, carded, innocent_client, _innocent = _two_boxes_on_one_address(hass)
    _raise_card(hass)

    await _flow(hass).async_step_confirm({"channels": ["0"]})

    assert innocent_client.writes == [], "the other box is not written to"
    assert carded_client.writes == [(0, 0, "H.264")]
    assert carded[0].codec == "H.264"


async def test_the_other_boxs_card_is_not_cleared(hass):
    """The same mix-up seen from the registry. Acting on one device's card
    must not delete the other device's."""
    _two_boxes_on_one_address(hass)
    _raise_card(hass)
    _raise_card(hass, device=OTHER_DEVICE)

    await _flow(hass).async_step_confirm({"channels": ["0"]})

    assert _issue(hass, OTHER_DEVICE) is not None


async def test_a_card_from_before_this_cannot_guess_between_two_boxes(hass):
    """A card raised by the older code carries no `device`. With one device on
    the address that is unambiguous and still works; with two there is nothing
    to match on, and guessing is exactly what wrote to the wrong recorder."""
    carded_client, _carded, innocent_client, _innocent = _two_boxes_on_one_address(hass)

    result = await _flow(hass, device=None).async_step_confirm({"channels": ["0"]})

    assert result["reason"] == "not_configured"
    assert carded_client.writes == []
    assert innocent_client.writes == []


async def test_a_card_from_before_this_still_works_with_one_box(hass):
    """The common case, and it must not regress: one address, one device, a
    card with no `device` in its data."""
    client, coordinators = _setup(hass)

    await _flow(hass, device=None).async_step_confirm({"channels": ["3"]})

    assert client.writes == [(3, 0, "H.264")]
    assert coordinators[3].codec == "H.264"


async def test_an_unconfirmed_read_back_keeps_the_warning(hass):
    """⭐ @rroller on #1127. `async_get_config` answers {} for an HTTP refusal,
    so the setup reader this used to call assigned an empty codec map and then
    re-judged the card from it: the known H.265 state was erased and the
    warning deleted, having learned nothing about the write at all. He
    reproduced a write followed by {}, reported as `write_refused` and leaving
    no issue in the registry.

    Not confirmed is a failure, not a pass."""
    _raise_card(hass)
    _, coordinators = _setup(hass, silent=(3,))

    result = await _flow(hass).async_step_confirm({"channels": ["3"]})

    assert result["reason"] == "write_refused"
    assert coordinators[3].codec == "H.265", "what is held is kept"
    assert _issue(hass) is not None, "and the card it was about stays up"


async def test_an_unconfirmed_read_back_does_not_sink_a_confirmed_sibling(hass):
    """Per channel, like everything else here. One channel whose read-back
    said nothing must not make a channel that really did move look refused,
    nor the other way round."""
    _, coordinators = _setup(hass, silent=(3,))

    result = await _flow(hass).async_step_confirm({"channels": ["0", "3"]})

    assert coordinators[0].codec == "H.264", "this one is confirmed"
    assert coordinators[3].codec == "H.265"
    assert result["reason"] == "write_refused", "but the run still says so"


async def test_unconfirmed_is_a_failure_even_if_the_held_codec_looks_right(hass):
    """The contract is "confirmed H.264", not "H.264 from somewhere".

    Without this the `not confirmed` clause is redundant, because a read-back
    that keeps what is held leaves the codec reading H.265 and the codec check
    catches it anyway. That redundancy is the whole point: the flow must not
    depend on the held value happening to disagree. Here the held value agrees
    and the read-back still did not confirm it, which is what an optimistic
    cache or a concurrent poll would look like.
    """
    _, coordinators = _setup(hass, silent=(3,))
    coordinators[3].codec = "H.264"

    result = await _flow(hass).async_step_confirm({"channels": ["3"]})

    assert result["reason"] == "write_refused"
