"""What the client asks a device about itself, and what it does when there is no answer.

Four methods, none executed by the suite, all of the same shape: ask, and cope. What
makes them worth pinning is that every one of their fallbacks is silent by design, so a
wrong fallback is not an error anywhere. It is a camera with an entity too many, or a
device whose identity quietly changes.

The one with consequences beyond itself is `async_get_machine_name`. When `magicBox.cgi`
refuses, the client invents an identity by hashing the address, the RTSP port and the
credentials, and flags that it did so. That hash is what the config entry's unique id
ends up being, which means **changing the password changes the device's identity** and
Home Assistant sees a different device. That is not a hypothetical: it is why
`is_synthesised_identity` and the re-identification migration exist at all.
"""

import pytest
from hashlib import md5

import aiohttp

from custom_components.dahua.client import DEFAULT_EXTRA_STREAMS, DahuaClient


def _error(status):
    return aiohttp.ClientResponseError(request_info=None, history=(), status=status)


def _client(answer=None, password="p"):
    """A client whose one HTTP call answers, or raises, as the test chooses."""
    client = object.__new__(DahuaClient)
    client._address = "10.0.0.5"
    client._rtsp_port = 554
    client._username = "u"
    client._password = password
    client.identity_derived_from_credentials = False
    client.asked = []

    async def get(url, verify_response=False):
        client.asked.append(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer if answer is not None else {}

    client.get = get
    return client


# --- how many sub-streams a device has --------------------------------------


async def test_the_stream_count_is_what_the_device_reports():
    client = _client({"table.MaxExtraStream": "2"})

    assert await client.get_max_extra_streams() == 2


async def test_a_doorbell_reporting_one_sub_stream_is_believed():
    """Measured: a VTO2000A answers 1 where an NVR answers 2. Over-guessing is not
    harmless, it is a camera entity that 404s on every attempt for as long as the
    entry exists (#237)."""
    client = _client({"table.MaxExtraStream": "1"})

    assert await client.get_max_extra_streams() == 1


async def test_a_device_without_the_endpoint_falls_back():
    """`getProductDefinition` is absent on plenty of firmware. The caller is inside
    the one-time init block, whose handler turns any exception into UpdateFailed,
    so raising here would stop the device ever finishing setup."""
    client = _client(_error(400))

    assert await client.get_max_extra_streams() == DEFAULT_EXTRA_STREAMS


async def test_an_answer_that_is_not_a_number_falls_back():
    client = _client({"table.MaxExtraStream": "lots"})

    assert await client.get_max_extra_streams() == DEFAULT_EXTRA_STREAMS


async def test_an_absent_key_falls_back():
    """A 200 with nothing useful in it, which is what an endpoint that exists but
    does not know this name answers."""
    client = _client({})

    assert await client.get_max_extra_streams() == DEFAULT_EXTRA_STREAMS


# --- what each stream is called ---------------------------------------------


def test_the_main_stream_is_called_main():
    assert DahuaClient.to_stream_name(0) == "Main"


def test_the_first_sub_stream_is_called_sub_not_sub_1():
    """Backwards compatibility, and the reason this is not a format string. When
    only one sub-stream was supported it was called "Sub", and entity ids were
    built from that. Renaming it to "Sub_1" would orphan every one of them."""
    assert DahuaClient.to_stream_name(1) == "Sub"


@pytest.mark.parametrize("index, name", [(2, "Sub_2"), (3, "Sub_3")])
def test_the_later_sub_streams_are_numbered(index, name):
    assert DahuaClient.to_stream_name(index) == name


# --- who the device says it is ----------------------------------------------


async def test_the_machine_name_is_passed_through():
    client = _client({"table.General.MachineName": "Cam4"})

    assert await client.async_get_machine_name() == {
        "table.General.MachineName": "Cam4"
    }
    assert client.identity_derived_from_credentials is False


async def test_a_device_that_will_not_say_gets_an_invented_identity():
    """Cameras whose `magicBox.cgi` refuses still have to be addressable, so the
    client hashes what it does know. The flag is what tells everything downstream
    that the id is not a real serial."""
    client = _client(_error(400))

    answer = await client.async_get_machine_name()

    assert client.identity_derived_from_credentials is True
    assert list(answer) == ["table.General.MachineName"]
    assert answer["table.General.MachineName"] != "Cam4"


async def test_the_invented_identity_is_the_documented_hash():
    """Asserted against the recipe rather than a literal, so that changing the
    inputs is a deliberate act: the value is a device's unique id, and changing
    how it is built re-identifies every camera that uses one."""
    client = _client(_error(400))

    answer = await client.async_get_machine_name()

    expected = md5("10.0.0.5_554_u_p".encode("UTF-8")).hexdigest()
    assert answer["table.General.MachineName"] == expected


async def test_the_invented_identity_is_stable_for_the_same_device():
    """Asked twice, the same answer. An id that moved between polls would make
    Home Assistant create a new device on every restart."""
    first = await _client(_error(400)).async_get_machine_name()
    second = await _client(_error(400)).async_get_machine_name()

    assert first == second


async def test_changing_the_password_changes_the_identity():
    """The consequence worth writing down. The credentials are in the hash, so a
    password change makes Home Assistant see a different device, which is exactly
    why `is_synthesised_identity` and the re-identification migration exist."""
    before = await _client(_error(400)).async_get_machine_name()
    after = await _client(_error(400), password="new").async_get_machine_name()

    assert before != after


# --- reading a lighting table that may not exist ----------------------------
#
# `async_get_config_lighting` reads as though it distinguishes a 400 (no such
# table) from any other status, which it re-raises. It does not, and cannot:
# `async_get_config` catches **every** ClientResponseError and returns {}, so
# nothing ever reaches the outer handler. Those lines are not merely uncovered,
# they are unreachable, which is why coverage could never have shown otherwise.
#
# Tested as it behaves rather than as it reads. See the PR for the consequence:
# a 401 on this read is swallowed as "this camera has no lights".


async def test_the_lighting_table_is_read_for_the_channel_and_profile():
    client = _client({"table.Lighting[0][1].Mode": "Auto"})

    await client.async_get_config_lighting(0, 1)

    assert client.asked == [
        "/cgi-bin/configManager.cgi?action=getConfig&name=Lighting[0][1]"
    ]


async def test_a_device_without_a_lighting_table_reads_as_empty():
    """400 means this device does not have the table. Empty rather than an
    exception, because a camera with no illuminator is not a broken camera and
    the poll must not fail on it."""
    client = _client(_error(400))

    assert await client.async_get_config_lighting(0, 0) == {}


@pytest.mark.parametrize("status", [401, 500, 503])
async def test_every_other_refusal_reads_as_empty_too(status):
    """Not what the code reads like. `async_get_config_lighting` has an
    `if e.status == 400 ... else raise`, and `async_get_config` has already
    turned every refusal into {} before it can run.

    Pinned as it behaves so the current answer is written down, and so that
    making the distinction real is a visible change to this file rather than a
    silent one. A 401 here is a credential refusal reported as a camera with no
    lights."""
    client = _client(_error(status))

    assert await client.async_get_config_lighting(0, 0) == {}
