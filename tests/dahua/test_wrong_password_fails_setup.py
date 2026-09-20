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
from custom_components.dahua.config_flow import describe_setup_failure


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
    client = _client(_status(401))

    with pytest.raises(ClientResponseError):
        await client.async_get_system_info()


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
