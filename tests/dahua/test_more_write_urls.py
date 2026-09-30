"""The URLs the remaining write methods build.

Seven methods that each turn arguments into one CGI request and send it. None of them
was executed by the suite, and a wrong URL here fails in the quietest way this device
has: it answers `OK` to a setConfig naming a key it does not have, so the integration
reports success and nothing changes.

The two that most deserve pinning are the day/night pair, because they look
interchangeable and are not:

    async_set_video_profile_mode   VideoInMode[c].Config[0]                night = 1
    async_set_night_switch_mode    VideoInOptions[c].NightOptions.SwitchMode  night = 3

Same concept, two tables, two different numbers for "night", and `camera.py` picks
between them on a model-name whitelist. Swapping the numbers writes a value the table
accepts and does not mean.

The three overlay writers are the only writes in the client that check the answer and
raise. They accept `OK` and `ok`, because firmware disagrees about the casing, and a
check for one spelling would raise on every successful write from the other half of the
fleet.
"""

import pytest

from custom_components.dahua.client import DahuaClient


class _Sent:
    """Records the URLs, and answers with whatever the test chose."""

    def __init__(self, answer="OK"):
        self.urls = []
        self.answer = answer

    async def get(self, url, verify_response=False):
        self.urls.append((url, verify_response))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _client(answer="OK"):
    client = object.__new__(DahuaClient)
    sent = _Sent(answer)
    client.get = sent.get
    client._sent = sent
    return client


def _url(client):
    assert len(client._sent.urls) == 1, client._sent.urls
    return client._sent.urls[0][0]


# --- the day/night pair, which are not interchangeable ----------------------

@pytest.mark.parametrize("mode, expected", [
    ("night", "1"), ("Night", "1"), ("NIGHT", "1"),
    ("day", "0"), ("Day", "0"), ("anything else", "0"),
])
async def test_the_video_profile_mode_numbers(mode, expected):
    """Night is 1 here. Anything that is not night is day, deliberately, so an
    unexpected value leaves the camera on its normal profile rather than on
    whatever the string happened to coerce to."""
    client = _client()

    await client.async_set_video_profile_mode(2, mode)

    assert _url(client) == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&VideoInMode[2].Config[0]=%s" % expected)


@pytest.mark.parametrize("mode, expected", [
    ("night", "3"), ("Night", "3"),
    ("day", "0"), ("anything else", "0"),
])
async def test_the_night_switch_mode_numbers(mode, expected):
    """Night is **3** here, not 1, and it is a different table. This is the path a
    Lorex NVR takes, chosen in camera.py by a model-name whitelist."""
    client = _client()

    await client.async_set_night_switch_mode(5, mode)

    assert _url(client) == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&VideoInOptions[5].NightOptions.SwitchMode=%s" % expected)


async def test_the_two_day_night_writes_do_not_share_a_table():
    """Stated as a test because the whole risk is that they look alike. Writing
    one table's value into the other is accepted and silently does nothing."""
    profile, switch = _client(), _client()

    await profile.async_set_video_profile_mode(0, "night")
    await switch.async_set_night_switch_mode(0, "night")

    assert "VideoInMode" in _url(profile)
    assert "VideoInOptions" in _url(switch)
    assert _url(profile) != _url(switch)


@pytest.mark.parametrize("method", [
    "async_set_video_profile_mode", "async_set_night_switch_mode",
])
async def test_the_day_night_writes_are_verified(method):
    """Both pass verify_response, so a device that answers with an error body
    rather than an error status is still a failure."""
    client = _client()

    await getattr(client, method)(0, "day")

    assert client._sent.urls[0][1] is True


# --- the overlay writers ----------------------------------------------------

async def test_the_channel_title_url():
    client = _client()

    await client.async_set_service_set_channel_title(3, "Front", "Gate")

    assert _url(client) == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&ChannelTitle[3].Name=Front|Gate")


async def test_the_text_overlay_url_carries_its_group():
    """Four lines and a group. The group indexes a separate overlay, so putting
    the channel there writes over a different one."""
    client = _client()

    await client.async_set_service_set_text_overlay(1, 2, "a", "b", "c", "d")

    assert _url(client) == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&VideoWidget[1].CustomTitle[2].Text=a|b|c|d")


async def test_the_custom_overlay_writes_a_different_key():
    """`UserDefinedTitle`, not `CustomTitle`. Two services one word apart that
    write to two different overlays on the same camera."""
    client = _client()

    await client.async_set_service_set_custom_overlay(1, 2, "a", "b")

    assert _url(client) == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&VideoWidget[1].UserDefinedTitle[2].Text=a|b")


async def test_a_space_in_a_title_is_not_written_as_a_plus():
    """Measured on a DHI-NVR5464-16P-EI: `Channel+1` is stored literally as
    "Channel+1", and `Channel%201` is stored as "Channel 1". The device answers OK
    either way, so every name with a space in it was written back wrong."""
    client = _client()

    await client.async_set_service_set_channel_title(0, "Front Gate", "")

    assert "Front%20Gate" in _url(client)
    assert "+" not in _url(client)


@pytest.mark.parametrize("answer", ["OK", "ok", "   OK   ", "somethingOKelse"])
async def test_an_overlay_write_that_worked_does_not_raise(answer):
    """Both spellings are accepted because firmware disagrees about the casing,
    and checking for one would raise on every successful write from the other."""
    client = _client(answer)

    await client.async_set_service_set_channel_title(0, "a", "b")


@pytest.mark.parametrize("method, args", [
    ("async_set_service_set_channel_title", (0, "a", "b")),
    ("async_set_service_set_text_overlay", (0, 1, "a", "b", "c", "d")),
    ("async_set_service_set_custom_overlay", (0, 1, "a", "b")),
])
async def test_an_overlay_write_that_was_refused_raises(method, args):
    """The only writes in the client that read the answer. A service call that
    silently did nothing is worse than one that fails, because the user is looking
    at the old text wondering whether they typed it wrong."""
    client = _client("Error")

    with pytest.raises(Exception):
        await getattr(client, method)(*args)


# --- the two lighting writes with their own shapes --------------------------

@pytest.mark.parametrize("enabled, mode", [(True, "Manual"), (False, "Off")])
async def test_a_flood_light_is_switched_by_its_mode(enabled, mode):
    """There is no separate on/off key: Manual is on and Off is off, and the light
    index is always 1 on these."""
    client = _client()

    await client.async_set_lighting_v2_for_flood_lights(4, enabled, "1")

    assert _url(client) == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Lighting_V2[4][1][1].Mode=%s" % mode)


@pytest.mark.parametrize("mode, expected", [
    ("on", "ForceOn&Lighting_V2[0][0][1].State=On"),
    ("On", "ForceOn&Lighting_V2[0][0][1].State=On"),
    ("strobe", "ForceOn&Lighting_V2[0][0][1].State=Flicker"),
    ("flicker", "ForceOn&Lighting_V2[0][0][1].State=Flicker"),
    ("off", "Off"),
    ("anything else", "Off"),
])
async def test_an_amcrest_doorbell_light_takes_three_modes(mode, expected):
    """The on and strobe values carry a second parameter inside the Mode value, so
    one write sets both Mode and State. It reads like a quoting mistake and is
    what the device expects."""
    client = _client()

    await client.async_set_lighting_v2_for_amcrest_doorbells(mode)

    assert _url(client) == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Lighting_V2[0][0][1].Mode=%s" % expected)


async def test_the_doorbell_light_is_hard_wired_to_channel_zero():
    """A doorbell has one camera and one light. The channel is not a parameter,
    which is worth pinning so that adding one later is a deliberate change."""
    client = _client()

    await client.async_set_lighting_v2_for_amcrest_doorbells("on")

    assert "Lighting_V2[0][0][1]" in _url(client)
