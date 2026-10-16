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
from aiohttp import ClientResponseError

from custom_components.dahua.client import DahuaClient


class _Recording:
    """The real client, with only `get` replaced so the URL can be read.

    `cgi_status`, when given, is what every CGI request raises, which is how a
    device with no /cgi-bin/ config path behaves.
    """

    def __init__(self, cgi_status=None):
        self.urls = []
        self.fields = []
        self._cgi_status = cgi_status

    async def get(self, url, verify_ok=False):
        self.urls.append((url, verify_ok))
        if self._cgi_status is not None:
            raise ClientResponseError(None, None, status=self._cgi_status)
        return {}

    async def _rpc2_set_config_field(self, name, value):
        self.fields.append((name, value))
        return {}


def _client(cgi_status=None):
    client = object.__new__(DahuaClient)
    recording = _Recording(cgi_status)
    client.get = recording.get
    client._rpc2_set_config_field = recording._rpc2_set_config_field
    client._address = "192.168.1.90"
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


# --- a device with no CGI config path ----------------------------------------
#
# An SL300 answers 404 to every /cgi-bin/ path it has. The Encode read already
# falls back to RPC2, so discovery gives such a device these controls and the
# poll feeds them real values; the write had no fallback, so every press failed
# on a control that looked healthy (#997 review).


@pytest.mark.parametrize("absent", [404, 501])
async def test_the_switch_falls_back_to_an_rpc2_field_write(absent):
    """Field addressed, and the address is the whole path to the field. The
    read-modify-write in `_rpc2_set_config_value` assigns one key per channel,
    which cannot reach a field two levels below it, and sends `Encode` back
    whole, which is the shape a recorder refuses for size."""
    client, recording = _client(cgi_status=absent)

    await client.async_set_encode_audio_value(2, "MainFormat", 1, "AudioEnable", True)

    assert recording.fields == [("Encode[2].MainFormat[1].AudioEnable", True)]


async def test_the_rpc2_write_sends_a_boolean_rather_than_the_wire_string():
    """CGI takes `true` in a URL; RPC2 takes JSON. Sending the string would be
    accepted by the fake and refused by the device."""
    client, recording = _client(cgi_status=404)

    await client.async_set_encode_audio_value(0, "MainFormat", 0, "AudioEnable", False)

    assert recording.fields[0][1] is False


async def test_the_source_selector_falls_back_too():
    """The other field these controls write. It is percent-encoded for the URL
    and must not be for RPC2."""
    client, recording = _client(cgi_status=404)

    await client.async_set_encode_audio_value(
        0, "ExtraFormat", 0, "Audio.AudioSource", "Coaxial"
    )

    assert recording.fields == [
        ("Encode[0].ExtraFormat[0].Audio.AudioSource", "Coaxial")
    ]


async def test_another_cgi_error_is_not_turned_into_an_rpc2_write():
    """A 403 is the account being refused, which no other transport fixes."""
    client, recording = _client(cgi_status=403)

    with pytest.raises(ClientResponseError):
        await client.async_set_encode_audio_value(
            0, "MainFormat", 0, "AudioEnable", True
        )

    assert recording.fields == []


async def test_a_device_that_answers_cgi_is_not_sent_to_rpc2():
    """The fallback must stay a fallback. Every ordinary camera serves this
    path, and a write that went both ways would log in for nothing."""
    client, recording = _client()

    await client.async_set_encode_audio_value(0, "MainFormat", 0, "AudioEnable", True)

    assert recording.fields == []
    assert len(recording.urls) == 1
