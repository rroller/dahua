"""The overlay visibility toggles, the alarm output mode, and two fallbacks.

Four `async_enable_*` methods show or hide an overlay by writing `EncodeBlend`, and none
was executed. They are easy to confuse with the `async_set_service_set_*` methods that
`test_more_write_urls.py` covers: those set an overlay's **text**, these set whether it
is **drawn**. Two different keys on the same camera, and a method that wrote the wrong
one would change the wrong overlay while answering `OK`.

The four are near-identical, which is exactly why a table is the right shape here. The
only thing separating them is the key:

    async_enable_channel_title    VideoWidget[c].ChannelTitle.EncodeBlend
    async_enable_time_overlay     VideoWidget[c].TimeTitle.EncodeBlend
    async_enable_text_overlay     VideoWidget[c].CustomTitle[g].EncodeBlend
    async_enable_custom_overlay   VideoWidget[c].UserDefinedTitle[g].EncodeBlend

A copy-paste between them is the likely defect, and a per-method test would not notice
it, because each assertion would be written from the same wrong line it is testing. The
table asserts they are all **different**.

Three other things in here, each a decision rather than a URL:

* **`AlarmOut[n].Mode` is three-state, not a boolean.** 0 is Auto, 1 is force ON and
  **2 is force OFF**. Writing 0 for off would return the output to automatic rather than
  switch it off, which on a siren is the difference between silence and whatever the
  camera decides next.
* **`async_get_config_lighting` treats HTTP 400 as "no such table" and returns `{}`.**
  Recorders answer 400 for lighting tables they do not carry, which is normal rather
  than a fault, and any other status still raises.
* **`_enable_motion_detection_cgi` has a fallback.** It asks with `DetectVersion=V3.0`
  first and, if the reply is not `OK`, asks again without it, because older cameras do
  not accept the two-key form.
"""

import pytest
from aiohttp import ClientResponseError
from types import SimpleNamespace

from custom_components.dahua.client import DahuaClient


class _Sent:
    """Records the URLs, and answers with whatever the test chose."""

    def __init__(self, answers):
        self.urls = []
        self.answers = list(answers)

    async def get(self, url, verify_response=False):
        self.urls.append(url)
        answer = self.answers.pop(0) if self.answers else "OK"
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _client(*answers):
    client = object.__new__(DahuaClient)
    sent = _Sent(answers)
    client.get = sent.get
    client._sent = sent
    return client


def _refused(status):
    return ClientResponseError(
        request_info=SimpleNamespace(real_url="http://10.0.0.5/x"),
        history=(), status=status)


# --- the four toggles, and the keys that tell them apart ---------------------

# method, the arguments after the channel, and the key fragment it must write.
TOGGLES = [
    ("async_enable_channel_title", (), "VideoWidget[4].ChannelTitle.EncodeBlend"),
    ("async_enable_time_overlay", (), "VideoWidget[4].TimeTitle.EncodeBlend"),
    ("async_enable_text_overlay", (2,), "VideoWidget[4].CustomTitle[2].EncodeBlend"),
    ("async_enable_custom_overlay", (2,), "VideoWidget[4].UserDefinedTitle[2].EncodeBlend"),
]


@pytest.mark.parametrize("method, extra, key", TOGGLES)
async def test_the_toggle_writes_its_own_key(method, extra, key):
    client = _client()

    await getattr(client, method)(4, *extra, True)

    url = client._sent.urls[0]
    assert "action=setConfig" in url, url
    assert "%s=true" % key in url, url


@pytest.mark.parametrize("method, extra, key", TOGGLES)
@pytest.mark.parametrize("enabled, written", [(True, "true"), (False, "false")])
async def test_the_boolean_is_written_the_way_the_device_spells_it(
        method, extra, key, enabled, written):
    """Python's `True` is not `true`, and the device takes the lower case form."""
    client = _client()

    await getattr(client, method)(4, *extra, enabled)

    assert "%s=%s" % (key, written) in client._sent.urls[0], client._sent.urls[0]


async def test_no_two_toggles_write_the_same_key():
    """The guard the per-method tests cannot be. These four are near-identical, so a
    copy-paste is the likely defect, and each assertion above would have been written
    from the same wrong line it checks."""
    written = {}
    for method, extra, _key in TOGGLES:
        client = _client()
        await getattr(client, method)(4, *extra, True)
        written[method] = client._sent.urls[0]

    assert len(set(written.values())) == len(TOGGLES), written


async def test_the_group_indexes_a_separate_overlay():
    """Two text overlays on one camera are two groups, so the group has to reach the
    URL or both write to the same one."""
    first, second = _client(), _client()

    await first.async_enable_text_overlay(4, 0, True)
    await second.async_enable_text_overlay(4, 1, True)

    assert first._sent.urls[0] != second._sent.urls[0]
    assert "CustomTitle[1]" in second._sent.urls[0]


# --- and what they do with the answer ----------------------------------------


@pytest.mark.parametrize("method, extra, _key", TOGGLES)
@pytest.mark.parametrize("answer", ["OK", "ok", "OK\r\n", "something ok here"])
async def test_an_answer_that_means_yes_does_not_raise(method, extra, _key, answer):
    """Firmware disagrees about the casing, so both are accepted. Checking one spelling
    would raise on every successful write from half the fleet."""
    client = _client(answer)

    await getattr(client, method)(4, *extra, True)


@pytest.mark.parametrize("method, extra, _key", TOGGLES)
async def test_a_refused_write_raises(method, extra, _key):
    """These are the only writes in the client that check the answer, and the point of
    that is a caller which finds out. Returning quietly would report success."""
    client = _client("Error")

    with pytest.raises(Exception):
        await getattr(client, method)(4, *extra, True)


# --- the alarm output, which is not a boolean -------------------------------


@pytest.mark.parametrize("enabled, mode", [(True, 1), (False, 2)])
async def test_the_alarm_output_is_forced_with_mode_one_or_two(enabled, mode):
    """0 is Auto, 1 is force ON, 2 is force OFF. Writing 0 for off hands the output
    back to the camera rather than switching it off, which on a siren is the difference
    between silence and whatever it decides next."""
    client = _client()

    await client.async_set_alarm_output_state(3, enabled)

    url = client._sent.urls[0]
    assert "AlarmOut[3].Mode=%d" % mode in url, url


async def test_turning_the_alarm_output_off_does_not_write_zero():
    """Named separately because 0 is the plausible wrong answer and it is a valid
    value, so the device accepts it and answers OK."""
    client = _client()

    await client.async_set_alarm_output_state(0, False)

    assert "Mode=0" not in client._sent.urls[0], client._sent.urls[0]


async def test_the_alarm_output_writes_mode_rather_than_enable():
    client = _client()

    await client.async_set_alarm_output_state(1, True)

    url = client._sent.urls[0]
    assert "AlarmOut[1].Mode=" in url, url
    assert "Enable" not in url, url


# --- a lighting table the device does not carry -----------------------------


async def test_a_lighting_table_the_device_refuses_is_empty_rather_than_fatal():
    """Recorders answer 400 for lighting tables they do not have, which is normal.
    Letting it raise would take the poll down on every recorder channel."""
    client = object.__new__(DahuaClient)

    async def async_get_config(name):
        raise _refused(400)

    client.async_get_config = async_get_config

    assert await client.async_get_config_lighting(0, 0) == {}


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_any_other_refusal_still_raises(status):
    """400 is the only one that means "no such table". A 401 is credentials and a 500
    is the device in trouble, and swallowing either would hide it as an empty table."""
    client = object.__new__(DahuaClient)

    async def async_get_config(name):
        raise _refused(status)

    client.async_get_config = async_get_config

    with pytest.raises(ClientResponseError):
        await client.async_get_config_lighting(0, 0)


async def test_the_lighting_table_is_asked_for_by_channel_and_profile():
    asked = []

    client = object.__new__(DahuaClient)

    async def async_get_config(name):
        asked.append(name)
        return {"table.Lighting[2][1].Mode": "Auto"}

    client.async_get_config = async_get_config

    await client.async_get_config_lighting(2, 1)

    assert asked == ["Lighting[2][1]"]


# --- motion detection, which asks twice ------------------------------------


async def test_motion_detection_asks_with_the_detect_version_first():
    client = _client("OK")

    await client._enable_motion_detection_cgi(5, True)

    assert len(client._sent.urls) == 1, client._sent.urls
    url = client._sent.urls[0]
    assert "MotionDetect[5].Enable=true" in url, url
    assert "MotionDetect[5].DetectVersion=V3.0" in url, url


async def test_an_older_camera_is_asked_again_without_the_detect_version():
    """The fallback. Older cameras refuse the two-key form, and the reply is not an
    HTTP error, it simply is not OK, so the only way to notice is to look at it."""
    client = _client("Error", "OK")

    await client._enable_motion_detection_cgi(5, True)

    assert len(client._sent.urls) == 2, client._sent.urls
    second = client._sent.urls[1]
    assert "MotionDetect[5].Enable=true" in second, second
    assert "DetectVersion" not in second, second


async def test_the_second_answer_is_the_one_returned():
    client = _client("Error", "OK second time")

    assert await client._enable_motion_detection_cgi(5, True) == "OK second time"


async def test_a_camera_that_accepted_the_first_form_is_not_asked_twice():
    """A second write would be a second change to the device for no reason, and these
    boxes are measurably intolerant of extra requests."""
    client = _client("OK")

    await client._enable_motion_detection_cgi(5, False)

    assert len(client._sent.urls) == 1
    assert "Enable=false" in client._sent.urls[0]
