"""A wrong password must fail setup, not add a camera that cannot work.

`_test_credentials` makes two calls, and both used to swallow every
`ClientResponseError` and return an id built from the credentials:

    except aiohttp.ClientResponseError as e:
        self.identity_derived_from_credentials = True
        not_hashed_id = "{0}_{1}_{2}_{3}".format(...username, ...password)
        return {"name": md5(not_hashed_id).hexdigest()}

So `"name" in data` was true whatever the device said, and a camera that
answered 401 to both was *added*. It then failed on every poll, and because the
id is derived from the password, correcting the password produced a different id
-- a second device rather than a repaired one.

The fallback itself is not the bug: it is what supports cameras with no
magicBox.cgi at all. The bug is applying it to a device that understood the
request and refused the login.
"""

from hashlib import md5

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua.client import DahuaClient, _is_login_refused
from custom_components.dahua.config_flow import (describe_setup_failure,
                                                 fallback_device_name)


def _status(status):
    return ClientResponseError(None, None, status=status, message="x")


def _client(exception):
    """A client whose device answers every read with `exception`."""
    client = DahuaClient("admin", "pw", "10.0.0.5", 80, 554, None, False)

    async def get(url, verify_ok=False):
        raise exception

    client.get = get
    return client


# --- the fallback is for a missing endpoint, not a refused login -------------

@pytest.mark.parametrize("status", [404, 501, 400])
async def test_a_device_without_magicbox_still_gets_an_id(status):
    """The reason the fallback exists. It must keep working."""
    client = _client(_status(status))

    name = await client.get_machine_name()
    info = await client.async_get_system_info()

    assert name["name"]
    assert info["serialNumber"]
    assert client.identity_derived_from_credentials is True


async def test_a_restricted_account_still_gets_an_id():
    """403 is "logged in but not allowed this", which is not a bad password."""
    client = _client(_status(403))

    assert (await client.get_machine_name())["name"]


# --- a refused login must not be turned into an identity --------------------

async def test_a_refused_login_is_not_turned_into_an_id():
    client = _client(_status(401))

    with pytest.raises(ClientResponseError) as caught:
        await client.get_machine_name()
    assert caught.value.status == 401


async def test_the_serial_call_refuses_too():
    """Only when the caller asks, which is the config flow and not the poll.

    This method is shared with the coordinator's one-time init, where a 401 is
    not proof of a wrong password -- channels of one NVR share a digest
    challenge and a raced nonce is refused identically. Raising there made
    every channel start a reauth flow at startup (#714), so the strictness is
    now the caller's to request.
    """
    client = _client(_status(401))

    with pytest.raises(ClientResponseError):
        await client.async_get_system_info(strict_auth=True)


async def test_the_serial_call_does_not_refuse_the_poll():
    """The coordinator's call, and what 0.10.7 did (#714)."""
    client = _client(_status(401))

    assert (await client.async_get_system_info())["serialNumber"]


async def test_the_id_is_never_built_from_a_refused_password():
    """The specific harm: an id derived from a password the camera rejected."""
    client = _client(_status(401))
    would_have_been = md5("10.0.0.5_554_admin_pw".encode("UTF-8")).hexdigest()

    try:
        result = await client.get_machine_name()
    except ClientResponseError:
        return  # refused, which is the point

    assert result.get("name") != would_have_been, (
        "added a camera with an id built from the password the device rejected")
    pytest.fail("a refused login was turned into %r" % result)


# --- and the whole way out to what the user is told -------------------------

def test_the_user_is_told_the_camera_rejected_the_login():
    """This is what makes the `auth` key reachable at all."""
    assert describe_setup_failure(_status(401)) == "auth"


def test_the_split_is_on_401_alone():
    assert _is_login_refused(_status(401)) is True
    for other in (403, 404, 501, 400, 500):
        assert _is_login_refused(_status(other)) is False


# --- and it must not be offered to the user as its name ----------------------
#
# The fallback id is fine as an id. It was also used as the *name*, and the flow
# prefilled it as the default of the last step, so pressing Submit produced a
# device called 4f3a9c8e... No platform sets _attr_has_entity_name, so that hash
# then went into every entity_id permanently.


def test_a_device_that_would_not_name_itself_gets_a_readable_name():
    assert fallback_device_name("192.168.1.108", 0) == "Dahua camera at 192.168.1.108"


def test_the_name_is_not_a_hash():
    """The specific thing that shipped: md5 of address_rtspport_user_password."""
    hashed = md5(b"192.168.1.108_554_admin_pw").hexdigest()

    assert fallback_device_name("192.168.1.108", 0) != hashed


def test_a_recorders_channels_do_not_all_get_the_same_name():
    """Several channels of one NVR can fall back together, and eleven devices
    called the same thing is no better than eleven hashes."""
    names = {fallback_device_name("10.0.0.5", ch) for ch in (0, 1, 2, 3)}

    assert len(names) == 4


def test_the_channel_is_numbered_the_way_the_recorder_shows_it():
    """1 based, matching the recorder's own UI, not the 0 based index."""
    assert fallback_device_name("10.0.0.5", 3) == "Dahua camera at 10.0.0.5 channel 4"


def test_a_single_camera_is_not_called_channel_1():
    """Channel 0 is a standalone camera as often as it is a recorder's first
    channel, and "channel 1" on a single camera is noise."""
    assert "channel" not in fallback_device_name("10.0.0.5", 0)


def test_a_channel_that_is_not_a_number_does_not_raise():
    """entry.data has carried strings here before now."""
    assert fallback_device_name("10.0.0.5", "not a number") == "Dahua camera at 10.0.0.5"
    assert fallback_device_name("10.0.0.5", None) == "Dahua camera at 10.0.0.5"


async def test_the_flow_offers_the_readable_name_and_keeps_the_hashed_id(monkeypatch):
    """The helper is only worth anything if _test_credentials uses it.

    The id keeps the hash here, and the reason has changed since this was written. It
    used to be "never change it, a new one would orphan every entry that has the hashed
    form". #805 measured a way out of that: `DHDiscover` on UDP 37810 needs no
    credentials and reports the device's real serial, so an identity better than a hash
    of the password is available for exactly the devices that land here.

    So the rule now is narrower: the hash is kept **when the network cannot do better**,
    which is this test, because the probe below answers nothing. The other half, taking
    the serial when there is one, is `test_identity_from_the_network.py`.
    """
    from custom_components.dahua import config_flow

    hashed = md5(b"10.0.0.5_554_admin_pw").hexdigest()

    async def _no_probe(address):
        """Silent, which is a camera behind a router. Stubbed rather than left to try a
        real UDP socket: the identity is md5 shaped, so the flow now probes, and the
        test harness fails any test that opens one."""
        return {}

    monkeypatch.setattr(config_flow, "async_probe_identity", _no_probe)

    class _Device:
        """No magicBox.cgi, so both reads fall back."""

        def __init__(self, *args, **kwargs):
            self.identity_derived_from_credentials = False

        async def get_machine_name(self):
            self.identity_derived_from_credentials = True
            return {"name": hashed}

        async def async_get_system_info(self, strict_auth=False):
            return {"serialNumber": hashed}

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(config_flow, "DahuaClient", _Device)
    monkeypatch.setattr(config_flow, "ClientSession", _Session)
    monkeypatch.setattr(config_flow, "TCPConnector", lambda **kwargs: None)

    handler = config_flow.DahuaFlowHandler()
    data, error = await handler._test_credentials(
        "admin", "pw", "10.0.0.5", "80", "554", 3)

    assert error is None
    assert data["name"] == "Dahua camera at 10.0.0.5 channel 4"
    assert data["serialNumber"] == hashed, (
        "the unique_id must keep the hashed form; changing it orphans existing entries")


async def test_a_device_that_names_itself_is_left_alone(monkeypatch):
    """The replacement must only apply to the fallback."""
    from custom_components.dahua import config_flow

    class _Device:
        def __init__(self, *args, **kwargs):
            self.identity_derived_from_credentials = False

        async def get_machine_name(self):
            return {"name": "FrontDoorCam"}

        async def async_get_system_info(self, strict_auth=False):
            return {"serialNumber": "4X7C5A1ZAG21L3F"}

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(config_flow, "DahuaClient", _Device)
    monkeypatch.setattr(config_flow, "ClientSession", _Session)
    monkeypatch.setattr(config_flow, "TCPConnector", lambda **kwargs: None)

    handler = config_flow.DahuaFlowHandler()
    data, error = await handler._test_credentials(
        "admin", "pw", "10.0.0.5", "80", "554", 3)

    assert data["name"] == "FrontDoorCam"
