"""The URLs the write methods build.

Almost every remaining uncovered line in client.py is an `async_set_*` method: it turns
arguments into a CGI URL and hands it to `self.get`. Nothing drove them, which is how the
infrared write came to be the only lighting URL with no test and how the profile it wrote
to went wrong unnoticed (#605).

These are worth pinning because of how they fail. A wrong URL is accepted by the device,
answers 200, and does nothing -- or does something else. There is no exception to notice
and no state to compare against, because the value read back comes from a different key.

Two of them are not simple formatting:

  * event notifications write the *opposite* of the switch, because the config key is
    `DisableEventNotify`
  * both disarming writes fall back to an un-indexed key when the channel-indexed one is
    refused, for cameras that do not have it
"""

from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient


def _client():
    client = object.__new__(DahuaClient)
    client.get = AsyncMock(return_value={})
    return client


def _urls(client):
    return [call.args[0] for call in client.get.await_args_list]


def _refusing_once(status=400):
    """A device that refuses the first write and accepts the second."""
    calls = []

    async def get(url, *args, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=status, message="Bad Request")
        return {}

    return get, calls


# --- the disarming linkage ----------------------------------------------------

async def test_disarming_linkage_writes_the_channel_indexed_key():
    client = _client()

    await DahuaClient.async_set_disarming_linkage(client, 3, True)

    assert "DisableLinkage[3].Enable=true" in _urls(client)[0]


async def test_disabling_the_linkage_writes_false():
    client = _client()

    await DahuaClient.async_set_disarming_linkage(client, 0, False)

    assert "DisableLinkage[0].Enable=false" in _urls(client)[0]


async def test_a_camera_without_the_indexed_key_gets_the_plain_one():
    """Measured on a recorder: it reports DisableLinkage un-indexed while the integration
    writes it per channel. A DH-P3D-3F-PV-P refuses the indexed form outright, so the
    write is retried without the index rather than reported as a failure."""
    client = _client()
    client.get, calls = _refusing_once()

    await DahuaClient.async_set_disarming_linkage(client, 2, True)

    assert len(calls) == 2
    assert "DisableLinkage[2].Enable=true" in calls[0]
    assert "DisableLinkage.Enable=true" in calls[1]
    assert "[" not in calls[1].split("DisableLinkage")[1][:4]


# --- event notifications, which mean the opposite -----------------------------

async def test_enabling_notifications_disables_the_disable():
    """The key is DisableEventNotify, so turning notifications *on* writes false. Getting
    this backwards silently does the opposite of what the switch says, and the state reads
    back consistently wrong so nothing looks broken."""
    client = _client()

    await DahuaClient.async_set_event_notifications(client, 0, True)

    assert "DisableEventNotify[0].Enable=false" in _urls(client)[0]


async def test_disabling_notifications_enables_the_disable():
    client = _client()

    await DahuaClient.async_set_event_notifications(client, 0, False)

    assert "DisableEventNotify[0].Enable=true" in _urls(client)[0]


async def test_notifications_fall_back_to_the_plain_key_too():
    client = _client()
    client.get, calls = _refusing_once()

    await DahuaClient.async_set_event_notifications(client, 1, True)

    assert len(calls) == 2
    assert "DisableEventNotify[1].Enable=false" in calls[0]
    assert "DisableEventNotify.Enable=false" in calls[1]


async def test_the_fallback_keeps_the_inversion():
    """The retry rebuilds the URL, so it is a second chance to get the inversion wrong."""
    client = _client()
    client.get, calls = _refusing_once()

    await DahuaClient.async_set_event_notifications(client, 0, False)

    assert "DisableEventNotify.Enable=true" in calls[1]


# --- the coaxial control -------------------------------------------------------

async def test_the_coaxial_control_turns_something_on_with_io_one():
    client = _client()

    await DahuaClient.async_set_coaxial_control_state(client, 1, 2, True)

    url = _urls(client)[0]
    assert "channel=1" in url
    assert "info[0].Type=2" in url
    assert "info[0].IO=1" in url


async def test_the_coaxial_control_turns_it_off_with_io_two_not_zero():
    """The comment above this code says "on = 1, off = 0" and the code writes 2. The code
    is what the device sees, so 2 is what this pins; the comment is stale."""
    client = _client()

    await DahuaClient.async_set_coaxial_control_state(client, 0, 1, False)

    assert "info[0].IO=2" in _urls(client)[0]


async def test_the_light_and_the_siren_are_different_types():
    """Type 1 is the white light and Type 2 is the siren, and they are the same endpoint
    otherwise -- so a swapped type sounds the siren when somebody asked for a light."""
    light, siren = _client(), _client()

    await DahuaClient.async_set_coaxial_control_state(light, 0, 1, True)
    await DahuaClient.async_set_coaxial_control_state(siren, 0, 2, True)

    assert "info[0].Type=1" in _urls(light)[0]
    assert "info[0].Type=2" in _urls(siren)[0]


# --- the record mode ----------------------------------------------------------

@pytest.mark.parametrize("given,written", [
    ("auto", "0"), ("Auto", "0"),
    ("manual", "1"), ("Manual", "1"), ("on", "1"), ("On", "1"),
    ("off", "2"), ("Off", "2"),
])
async def test_the_record_mode_names_map_to_the_numbers_the_device_wants(given, written):
    """Three numbers behind six names, and "on" is a synonym for manual because that is
    what a person means by it."""
    client = _client()

    await DahuaClient.async_set_record_mode(client, 0, given)

    assert "RecordMode[0].Mode=%s" % written in _urls(client)[0]


async def test_the_record_mode_is_written_against_the_channel_asked_for():
    client = _client()

    await DahuaClient.async_set_record_mode(client, 4, "off")

    assert "RecordMode[4].Mode=2" in _urls(client)[0]


# --- all the IVS rules at once -------------------------------------------------

async def test_only_the_rules_the_camera_has_are_written():
    """The table is read first, and a rule the camera does not hold is not written. Writing
    a rule index that does not exist is #713's 400, from a control that was never going to
    work."""
    client = _client()

    async def _rules():
        return {
            "table.VideoAnalyseRule[0][0].Enable": "true",
            "table.VideoAnalyseRule[0][2].Enable": "false",
        }

    client.async_get_ivs_rules = _rules

    await DahuaClient.async_set_all_ivs_rules(client, 0, True)

    url = _urls(client)[0]
    assert "VideoAnalyseRule[0][0].Enable=true" in url
    assert "VideoAnalyseRule[0][2].Enable=true" in url
    assert "VideoAnalyseRule[0][1]" not in url


async def test_every_rule_goes_in_one_request():
    """One setConfig with all of them rather than one per rule: each is a separate login in
    the device's log, and this is the control that toggles all of them."""
    client = _client()

    async def _rules():
        return {"table.VideoAnalyseRule[0][%d].Enable" % i: "true" for i in range(4)}

    client.async_get_ivs_rules = _rules

    await DahuaClient.async_set_all_ivs_rules(client, 0, False)

    assert len(_urls(client)) == 1
    assert _urls(client)[0].count("VideoAnalyseRule") == 4


async def test_a_camera_with_no_rules_is_not_written_to_at_all():
    """An empty setConfig would be a request that changes nothing, and on some firmware an
    error."""
    client = _client()

    async def _rules():
        return {}

    client.async_get_ivs_rules = _rules

    await DahuaClient.async_set_all_ivs_rules(client, 0, True)

    assert _urls(client) == []


async def test_rules_on_another_channel_are_left_alone():
    """The table holds every channel of a recorder, so filtering on the channel is what
    stops this toggling all of them."""
    client = _client()

    async def _rules():
        return {
            "table.VideoAnalyseRule[0][0].Enable": "true",
            "table.VideoAnalyseRule[1][0].Enable": "true",
        }

    client.async_get_ivs_rules = _rules

    await DahuaClient.async_set_all_ivs_rules(client, 1, False)

    url = _urls(client)[0]
    assert "VideoAnalyseRule[1][0].Enable=false" in url
    assert "VideoAnalyseRule[0][0]" not in url


async def test_a_rule_another_channel_has_is_not_written_to_this_one():
    """The channel has to be in the key that is looked up, not only in the key that is
    written. Here channel 0 holds rule 0 and channel 1 holds nothing, so a lookup that
    ignored the channel would find channel 0's rule and write it against channel 1 -- a
    rule index that camera does not have, which is #713's 400 from a control that was
    never going to work.

    The version of this test above cannot see that, because both channels held the rule
    and the mutated lookup produced the same URL. This is the case that separates them.
    """
    client = _client()

    async def _rules():
        return {"table.VideoAnalyseRule[0][0].Enable": "true"}

    client.async_get_ivs_rules = _rules

    await DahuaClient.async_set_all_ivs_rules(client, 1, False)

    assert _urls(client) == [], (
        "wrote a rule channel 1 does not have: %s" % _urls(client))
