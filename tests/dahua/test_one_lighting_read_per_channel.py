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

**The review of #1247** ran the client against two devices this broke: one
whose channel read times out while its direct read works, and one whose channel
read holds the day rows only while `Lighting[0][1]` has a night profile. The
direct read is the one every device was already proven on, so it stays the
authority.

**The second review** ran a recorder whose channel 0 carries a night row in
`Lighting[0]` while `Lighting[1]` returns day only and `Lighting[1][1]` has a
valid night profile. The fix for the first review believed a missing profile
once any channel on the device had shown it, and channel 1 read as having
none. So a channel read is no longer believed about a profile it does not
carry, on any evidence: the direct read is asked, per channel, every time. The
last two sections here are those.
"""

import asyncio

import aiohttp
import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import client as client_module
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


def _device(answers, port=80):
    """A real client over a device that answers each config name as told.

    `_request` is the stand-in, below the read cache, so what is counted is
    requests that would have reached the device. An exception is raised.
    """
    client = DahuaClient("u", "p", "recorder", port, 554, object())
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
    client = _device({"Lighting[1]": table, "Lighting[1][2]": NO_SUCH_PROFILE})

    assert await client.async_get_config_lighting(1, 0) == _profile(1, 0)
    assert await client.async_get_config_lighting(1, 2) == NO_SUCH_PROFILE


async def test_profile_one_is_not_answered_with_profile_ten():
    table = {**_profile(0, 1), **_profile(0, 10)}
    client = _device({"Lighting[0]": table})

    assert await client.async_get_config_lighting(0, 1) == _profile(0, 1)


async def test_a_channel_without_the_profile_is_asked_directly_about_it():
    """Channels 0 and 6 of the recorder: profile 0 only. The channel read not
    carrying profile 2 is not taken as the channel not having it."""
    client = _device(
        {"Lighting[0]": _channel(0, profiles=(0,)), "Lighting[0][2]": NO_SUCH_PROFILE}
    )

    assert await client.async_get_config_lighting(0, 2) == NO_SUCH_PROFILE
    assert client.sent == ["Lighting[0]", "Lighting[0][2]"]


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


async def test_a_channel_without_profiles_costs_the_two_requests_it_always_did():
    """The saving is for a channel that has the profile. One that does not is
    asked directly, which is what tells setup it has no profiles."""
    client = _device(
        {"Lighting[6]": _channel(6, profiles=(0,)), "Lighting[6][2]": NO_SUCH_PROFILE}
    )
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._channel = 6
    coordinator._profile_mode = "0"
    coordinator._supports_profile_mode = True
    coordinator._probe_refusals = {}

    assert await coordinator.async_detect_lighting_support() is True
    assert await coordinator._async_probe_profile_mode() is False

    assert client.sent == ["Lighting[6]", "Lighting[6][2]"]


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


# --- the channel read failing is not the poll failing (review of #1247) --------


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        asyncio.TimeoutError(),
        aiohttp.ClientConnectionError("dropped"),
        aiohttp.ServerDisconnectedError(),
    ],
)
async def test_a_channel_read_that_fails_is_followed_by_the_direct_read(failure):
    """The review's first device: `Lighting[0]` times out and `Lighting[0][1]`
    answers a valid night profile. The timeout used to escape into the poll's
    gather, which has no `return_exceptions`."""
    client = _device({"Lighting[0]": failure, "Lighting[0][1]": _profile(0, 1)})

    assert await client.async_get_config_lighting(0, 1) == _profile(0, 1)
    assert client.sent == ["Lighting[0]", "Lighting[0][1]"]


async def test_a_failed_channel_read_is_not_remembered_against_the_device():
    """One slow answer on a busy recorder must not cost it the saving."""
    answers = {"Lighting[0]": TimeoutError(), "Lighting[0][1]": _profile(0, 1)}
    client = _device(answers)
    await client.async_get_config_lighting(0, 1)
    assert client_module._LIGHTING_LEFT_OUT == set()

    answers["Lighting[2]"] = _channel(2)
    del client.sent[:]

    assert await client.async_get_config_lighting(2, 1) == _profile(2, 1)
    assert client.sent == ["Lighting[2]"]


async def test_both_reads_failing_fails_the_way_it_always_did():
    client = _device({"Lighting[0]": TimeoutError(), "Lighting[0][1]": TimeoutError()})

    with pytest.raises(TimeoutError):
        await client.async_get_config_lighting(0, 1)
    assert client.sent == ["Lighting[0]", "Lighting[0][1]"]


async def test_a_cancelled_channel_read_is_not_retried():
    """Cancellation is the caller leaving, not the device failing."""
    client = _device({"Lighting[0]": asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        await client.async_get_config_lighting(0, 1)
    assert client.sent == ["Lighting[0]"]


# --- a channel read is never believed about a profile it does not carry --------


async def test_day_rows_only_does_not_hide_a_night_profile():
    """The first review's second device: `Lighting[0]` returns the day rows
    and `Lighting[0][1]` a valid night profile. This returned {} and never
    asked."""
    client = _device(
        {"Lighting[0]": _channel(0, profiles=(0,)), "Lighting[0][1]": _profile(0, 1)}
    )

    assert await client.async_get_config_lighting(0, 1) == _profile(0, 1)
    assert client.sent == ["Lighting[0]", "Lighting[0][1]"]


async def test_a_night_row_on_one_channel_does_not_vouch_for_another():
    """The second review's recorder. Channel 0's read carries a night row.
    Channel 1's holds day only, and its direct read has a valid night profile.
    Channel 0 having shown the profile used to make channel 1's omission
    believed, and channel 1 read as having no night profile."""
    client = _device(
        {
            "Lighting[0]": _channel(0, profiles=(0, 1)),
            "Lighting[1]": _channel(1, profiles=(0,)),
            "Lighting[1][1]": _profile(1, 1),
        }
    )

    assert await client.async_get_config_lighting(0, 1) == _profile(0, 1)
    assert await client.async_get_config_lighting(1, 1) == _profile(1, 1)
    assert client.sent == ["Lighting[0]", "Lighting[1]", "Lighting[1][1]"]


async def test_nor_does_any_number_of_channels():
    """Seven channels carrying the profile say nothing about the eighth."""
    answers = {"Lighting[%d]" % ch: _channel(ch) for ch in range(7)}
    answers["Lighting[7]"] = _channel(7, profiles=(0,))
    answers["Lighting[7][2]"] = _profile(7, 2)
    client = _device(answers)
    for channel in range(7):
        await client.async_get_config_lighting(channel, 2)

    assert await client.async_get_config_lighting(7, 2) == _profile(7, 2)


async def test_a_profile_a_channel_read_leaves_out_is_read_directly_from_then_on():
    """Without asking the read that is known not to hold it. Counted below
    the cache, which is cleared so the second question is really asked."""
    client = _device(
        {"Lighting[0]": _channel(0, profiles=(0,)), "Lighting[0][1]": _profile(0, 1)}
    )
    await client.async_get_config_lighting(0, 1)
    client_module._HOST_CACHE.clear()
    del client.sent[:]

    assert await client.async_get_config_lighting(0, 1) == _profile(0, 1)
    assert client.sent == ["Lighting[0][1]"]


async def test_that_is_remembered_for_the_channel_it_was_seen_on_and_no_other():
    client = _device(
        {
            "Lighting[0]": _channel(0, profiles=(0,)),
            "Lighting[0][1]": _profile(0, 1),
            "Lighting[4]": _channel(4),
        }
    )
    await client.async_get_config_lighting(0, 1)
    del client.sent[:]

    assert await client.async_get_config_lighting(4, 1) == _profile(4, 1)
    assert client.sent == ["Lighting[4]"], "channel 4 is still asked by channel"


async def test_the_day_profile_still_comes_from_the_channel_read_on_that_channel():
    """What it left out says nothing about what it does carry."""
    client = _device(
        {"Lighting[0]": _channel(0, profiles=(0,)), "Lighting[0][1]": _profile(0, 1)}
    )
    await client.async_get_config_lighting(0, 1)
    del client.sent[:]

    assert await client.async_get_config_lighting(0, 0) == _profile(0, 0)
    assert client.sent == [], "from the channel read already made"


async def test_a_missing_profile_is_checked_on_every_channel_every_time():
    """Nothing is ever learnt that lets the direct read be skipped."""
    answers = {
        "Lighting[0]": _channel(0, profiles=(0,)),
        "Lighting[0][2]": NO_SUCH_PROFILE,
        "Lighting[6]": _channel(6, profiles=(0,)),
        "Lighting[6][2]": NO_SUCH_PROFILE,
    }
    client = _device(answers)

    assert await client.async_get_config_lighting(0, 2) == NO_SUCH_PROFILE
    assert await client.async_get_config_lighting(6, 2) == NO_SUCH_PROFILE
    assert client.sent == [
        "Lighting[0]",
        "Lighting[0][2]",
        "Lighting[6]",
        "Lighting[6][2]",
    ]
    assert client_module._LIGHTING_LEFT_OUT == set()

    # And again once the cache has expired: the camera may have gained the
    # profile since, and only the direct read would say so on a firmware
    # whose channel read leaves it out.
    client_module._HOST_CACHE.clear()
    answers["Lighting[0][2]"] = _profile(0, 2)
    del client.sent[:]

    assert await client.async_get_config_lighting(0, 2) == _profile(0, 2)
    assert client.sent == ["Lighting[0]", "Lighting[0][2]"]


async def test_a_direct_read_that_says_nothing_settles_nothing():
    client = _device(
        {"Lighting[0]": _channel(0, profiles=(0,)), "Lighting[0][2]": _refused(500)}
    )

    assert await client.async_get_config_lighting(0, 2) == {}
    assert client_module._LIGHTING_LEFT_OUT == set()


async def test_what_one_device_left_out_says_nothing_about_another():
    """Two boxes behind one address on two ports are two firmwares."""
    answers = {
        "Lighting[0]": _channel(0, profiles=(0,)),
        "Lighting[0][1]": _profile(0, 1),
    }
    one = _device(answers)
    await one.async_get_config_lighting(0, 1)
    other = _device({"Lighting[0]": _channel(0)}, port=81)

    assert await other.async_get_config_lighting(0, 1) == _profile(0, 1)
    assert other.sent == ["Lighting[0]"]


async def test_what_setup_costs_a_recorder_like_the_one_measured():
    """Setup's two questions across eight channels: 0 and 6 have profile 0
    only, the rest have four. Ten requests, where main makes sixteen."""
    answers = {}
    for channel in range(8):
        has_profiles = channel not in (0, 6)
        answers["Lighting[%d]" % channel] = _channel(
            channel, profiles=(0, 1, 2, 3) if has_profiles else (0,)
        )
        answers["Lighting[%d][2]" % channel] = (
            _profile(channel, 2) if has_profiles else NO_SUCH_PROFILE
        )
    client = _device(answers)

    for channel in range(8):
        await client.async_get_config_lighting(channel, "0")
        await client.async_get_config_lighting(channel, 2)

    assert client.sent == [
        "Lighting[0]",
        "Lighting[0][2]",
        "Lighting[1]",
        "Lighting[2]",
        "Lighting[3]",
        "Lighting[4]",
        "Lighting[5]",
        "Lighting[6]",
        "Lighting[6][2]",
        "Lighting[7]",
    ]
