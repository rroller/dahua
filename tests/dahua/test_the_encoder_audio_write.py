"""What the encoder audio switch and selector actually send.

The entities were covered; the write under them was not, which is how the
index ceiling in `async_set_encode_audio_value` survived. Discovery keeps
every `table.Encode[c].<format>[<n>].AudioEnable` key the device reports and
`_AUDIO_ENABLE_KEY` in switch.py matches any number, so a device serving
`MainFormat[3]` got a switch that read a real value and raised
`ValueError("Unsupported encoder format index")` on every press.

The rest of the validation is kept and tested here, because it is what stops
a caller writing a field name the device does not have: a `setConfig` naming
an absent field answers 200 with the body `Error`, so a wrong key produces a
plausible answer for ever.
"""

import pytest

from custom_components.dahua.client import DahuaClient


class _Recording:
    """The real client, with only `get` replaced so the URL can be read."""

    def __init__(self):
        self.urls = []

    async def get(self, url, verify_ok=False):
        self.urls.append((url, verify_ok))
        return {}


def _client():
    client = object.__new__(DahuaClient)
    recording = _Recording()
    client.get = recording.get
    return client, recording


# --- the index the device reported ------------------------------------------


@pytest.mark.parametrize("format_index", [0, 1, 2, 3])
async def test_every_index_the_device_can_report_is_writable(format_index):
    """`MainFormat[3]` is the case from the review. The others are here so a
    ceiling reintroduced one higher is caught as well."""
    client, recording = _client()

    await client.async_set_encode_audio_value(
        0, "MainFormat", format_index, "AudioEnable", True
    )

    url, verify_ok = recording.urls[0]
    assert "Encode[0].MainFormat[%d].AudioEnable=true" % format_index in url
    assert verify_ok, "a 200 with the body Error is a refusal, and must be read"


async def test_an_index_that_is_not_one_is_still_refused():
    """The check that is left. A caller passing something that is not an index
    would otherwise build a URL naming a field the device does not have, which
    answers 200 and changes nothing."""
    client, _recording = _client()

    for bad in (-1, "0", None):
        with pytest.raises(ValueError):
            await client.async_set_encode_audio_value(
                0, "MainFormat", bad, "AudioEnable", True
            )


# --- the rest of the validation ---------------------------------------------


async def test_the_extra_format_is_written_the_same_way():
    client, recording = _client()

    await client.async_set_encode_audio_value(2, "ExtraFormat", 1, "AudioEnable", False)

    assert "Encode[2].ExtraFormat[1].AudioEnable=false" in recording.urls[0][0]


async def test_a_format_the_table_does_not_have_is_refused():
    client, recording = _client()

    with pytest.raises(ValueError):
        await client.async_set_encode_audio_value(
            0, "SnapFormat", 0, "AudioEnable", True
        )

    assert recording.urls == [], "nothing should reach the device"


async def test_the_audio_source_is_url_encoded():
    client, recording = _client()

    await client.async_set_encode_audio_value(
        0, "MainFormat", 0, "Audio.AudioSource", "Coaxial"
    )

    assert "Encode[0].MainFormat[0].Audio.AudioSource=Coaxial" in recording.urls[0][0]


async def test_a_source_the_device_does_not_offer_is_refused():
    client, recording = _client()

    with pytest.raises(ValueError):
        await client.async_set_encode_audio_value(
            0, "MainFormat", 0, "Audio.AudioSource", "Telepathy"
        )

    assert recording.urls == []


async def test_a_field_nobody_reads_is_refused():
    client, recording = _client()

    with pytest.raises(ValueError):
        await client.async_set_encode_audio_value(
            0, "MainFormat", 0, "Compression", "AAC"
        )

    assert recording.urls == []


async def test_audio_enable_takes_a_boolean_not_a_word():
    """`"false"` is truthy, and would have been written as the string it is."""
    client, recording = _client()

    with pytest.raises(ValueError):
        await client.async_set_encode_audio_value(
            0, "MainFormat", 0, "AudioEnable", "false"
        )

    assert recording.urls == []
