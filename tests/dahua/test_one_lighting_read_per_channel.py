"""Setup asks one Lighting question per channel, not two (#1232).

@billjes's 19 channel recorder took 194 seconds to start on 3.1.0. His debug
log put about two minutes of that in two queues of per-channel requests:

    06:38:49 -> 06:39:53   Lighting[ch][0]   ~6 s each, two at a time
    06:40:06 -> 06:40:59   Lighting[ch][2]   ~8.5 s each, two at a time

The first asks whether the channel has an infrared light, the second whether
it has day and night profiles. Both are rows of one channel's table, and the
poll that follows reads the first again.

Measured on a DHI-NVR5464-16P-EI and a VTO2000A, read-only: `Lighting[ch]`
returns exactly the rows the two return separately. So the client reads the
channel once and answers each profile from it.

Not measured: whether @billjes's recorder answers the one read in the time of
one of the two. His is the recorder that is slow, and mine answers either in
0.05 s.
"""

import aiohttp
import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient

URL = "/cgi-bin/configManager.cgi?action=getConfig&name="


def _profile(channel, profile, bank="MiddleLight"):
    """One profile row. Five fields is what channel 1 of the recorder carries."""
    row = "table.Lighting[%d][%d]." % (channel, profile)
    return {
        row + "Correction": "50",
        row + bank + "[0].Angle": "50",
        row + bank + "[0].Light": "50",
        row + "Mode": "Auto",
        row + "Sensitive": "3",
    }


def _channel(channel, profiles=(0, 1, 2, 3)):
    table = {}
    for profile in profiles:
        table.update(_profile(channel, profile))
    return table


# What the recorder answers for the direct read of a profile the channel does
# not have: HTTP 200 and this one line, which the parser turns into one key.
NO_SUCH_PROFILE = {"Error: Error -1 getting param in name": "Lighting[0][2]"}


def _device(answers):
    """A real client over a device that answers each config name as told.

    `_request` is the stand-in, below the read cache, so what is counted is
    requests that would have reached the device. An exception is raised.
    """
    client = DahuaClient("u", "p", "recorder", 80, 554, object())
    client.sent = []

    async def _request(url, *args, **kwargs):
        client.sent.append(url.replace(URL, ""))
        answer = answers.get(url.replace(URL, ""), {})
        if isinstance(answer, BaseException):
            raise answer
        return dict(answer)

    client._request = _request
    return client


def _refused(status):
    return aiohttp.ClientResponseError(None, (), status=status, message="x")


# --- the rows are the same ones ----------------------------------------------


async def test_each_profile_comes_back_as_its_own_rows():
    client = _device({"Lighting[1]": _channel(1)})

    assert await client.async_get_config_lighting(1, 0) == _profile(1, 0)
    assert await client.async_get_config_lighting(1, 2) == _profile(1, 2)
    assert await client.async_get_config_lighting(1, "2") == _profile(1, 2)


async def test_another_channels_rows_are_not_this_channels():
    """Channel 1 must not be answered from channel 11, which shares its
    first digit."""
    table = {**_channel(1, profiles=(0,)), **_channel(11)}
    client = _device({"Lighting[1]": table})

    assert await client.async_get_config_lighting(1, 0) == _profile(1, 0)
    assert await client.async_get_config_lighting(1, 2) == {}


async def test_profile_one_is_not_answered_with_profile_ten():
    table = {**_profile(0, 1), **_profile(0, 10)}
    client = _device({"Lighting[0]": table})

    assert await client.async_get_config_lighting(0, 1) == _profile(0, 1)


async def test_a_channel_without_the_profile_has_no_rows_for_it():
    """Channels 0 and 6 of the recorder: profile 0 only."""
    client = _device(
        {"Lighting[0]": _channel(0, profiles=(0,)), "Lighting[0][2]": NO_SUCH_PROFILE}
    )

    assert await client.async_get_config_lighting(0, 2) == {}
    assert client.sent == ["Lighting[0]"], "the channel answered; nothing more to ask"


# --- and they cost one request -----------------------------------------------


async def test_three_questions_about_a_channel_are_one_request():
    """What setup does: the live profile, profile 2, then the poll's read."""
    client = _device({"Lighting[1]": _channel(1)})

    await client.async_get_config_lighting(1, "0")
    await client.async_get_config_lighting(1, 2)
    await client.async_get_config_lighting(1, "0")

    assert client.sent == ["Lighting[1]"]


async def test_setups_two_probes_and_the_poll_are_one_request():
    """The same, through the coordinator's own two probes."""
    client = _device({"Lighting[3]": _channel(3)})
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._channel = 3
    coordinator._profile_mode = "0"
    coordinator._supports_profile_mode = False
    coordinator._probe_refusals = {}

    assert await coordinator.async_detect_lighting_support() is True
    assert await coordinator._async_probe_profile_mode() is True
    assert await client.async_get_config_lighting(3, coordinator._profile_mode)

    assert client.sent == ["Lighting[3]"]


async def test_a_channel_without_profiles_is_still_one_request():
    client = _device({"Lighting[6]": _channel(6, profiles=(0,))})
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._channel = 6
    coordinator._profile_mode = "0"
    coordinator._supports_profile_mode = True
    coordinator._probe_refusals = {}

    assert await coordinator.async_detect_lighting_support() is True
    assert await coordinator._async_probe_profile_mode() is False

    assert client.sent == ["Lighting[6]"]


async def test_each_channel_is_its_own_request():
    client = _device({"Lighting[1]": _channel(1), "Lighting[2]": _channel(2)})

    await client.async_get_config_lighting(1, 0)
    await client.async_get_config_lighting(2, 0)

    assert client.sent == ["Lighting[1]", "Lighting[2]"]


# --- a device that says nothing for the channel is asked the old way -----------


@pytest.mark.parametrize("nothing", [{}, _refused(400), _refused(500)])
async def test_nothing_for_the_channel_falls_back_to_the_profile_read(nothing):
    """No firmware has been seen to do this. One that did would lose its
    infrared light without the fallback."""
    client = _device({"Lighting[0]": nothing, "Lighting[0][0]": _profile(0, 0)})

    assert await client.async_get_config_lighting(0, 0) == _profile(0, 0)
    assert client.sent == ["Lighting[0]", "Lighting[0][0]"]


async def test_an_answer_with_no_row_for_the_channel_falls_back_too():
    client = _device({"Lighting[0]": NO_SUCH_PROFILE, "Lighting[0][0]": _profile(0, 0)})

    assert await client.async_get_config_lighting(0, 0) == _profile(0, 0)


async def test_rows_for_channel_eleven_are_not_an_answer_about_channel_one():
    """The two share a prefix as text. An answer carrying only the other
    channel's rows has said nothing about this one, so it is asked the old
    way."""
    client = _device({"Lighting[1]": _channel(11), "Lighting[1][0]": _profile(1, 0)})

    assert await client.async_get_config_lighting(1, 0) == _profile(1, 0)
    assert client.sent == ["Lighting[1]", "Lighting[1][0]"]


async def test_the_fallback_returns_what_the_old_read_returned():
    """Whatever it was, the one junk key included: nothing changes for a
    device on this path."""
    client = _device({"Lighting[0][2]": NO_SUCH_PROFILE})

    assert await client.async_get_config_lighting(0, 2) == NO_SUCH_PROFILE


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_a_device_with_no_lighting_table_reads_as_empty(status):
    """One request more than before, on a device that has none of this."""
    client = _device(
        {"Lighting[0]": _refused(status), "Lighting[0][0]": _refused(status)}
    )

    assert await client.async_get_config_lighting(0, 0) == {}
    assert client.sent == ["Lighting[0]", "Lighting[0][0]"]
