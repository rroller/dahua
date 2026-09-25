"""A camera reported as `Generic RTSP` should say why it is not itself.

`Generic RTSP` is not a device type, and firmware `1.0` is not a firmware.
They are what this client returns when `magicBox.cgi` answers an identity
question with an HTTP error. The user sees a device page naming a camera they
do not own, most entities unavailable, and nothing anywhere saying what
happened. Three open reports sit on exactly that, on firmware from 2014 and
2015: #583, and the same unanswered question under #728 and #767.

The status is the half that matters. A 400 is the device saying it does not
serve that action, which is a fact about the firmware and generalises. A 401
is it refusing the credentials, which is a completely different problem
arriving at an identical device page.

What must not change: the fallback values themselves. Entities and unique ids
are built on them, so an old camera that has been running as `Generic RTSP`
for years must keep exactly the identity it has. This adds an explanation, not
a behaviour.
"""
import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient


def _client():
    return DahuaClient("u", "p", "10.0.0.94", 80, 554, None)


def _http(status):
    return aiohttp.ClientResponseError(None, None, status=status)


def _refusing(client, status=400):
    """Make every read fail the way a refusing CGI interface does."""
    async def get(url, verify_ok=False):
        raise _http(status)
    client.get = get
    return client


def _messages(caplog):
    return [r.getMessage() for r in caplog.records
            if "unidentified camera" in r.getMessage()]


# --- the identity still falls back exactly as it did ------------------------

async def test_the_model_still_falls_back():
    assert await _refusing(_client()).get_device_type() == {"type": "Generic RTSP"}


async def test_the_firmware_still_falls_back():
    assert await _refusing(_client()).get_software_version() == {"version": "1.0"}


async def test_the_vendor_still_falls_back():
    assert await _refusing(_client()).get_vendor() == {"vendor": "Generic RTSP"}


# --- but now it says so -----------------------------------------------------

async def test_the_status_the_device_gave_is_recorded():
    client = _refusing(_client(), 400)

    await client.get_device_type()

    assert client._identity_fallbacks == {"getDeviceType": 400}


async def test_a_refused_password_is_told_apart_from_an_unserved_action():
    """Both produce the same device page, and they are not the same problem."""
    client = _refusing(_client(), 401)

    await client.get_device_type()

    assert client._identity_fallbacks["getDeviceType"] == 401


async def test_each_question_is_recorded_separately():
    client = _refusing(_client())

    await client.get_device_type()
    await client.get_software_version()
    await client.get_vendor()

    assert sorted(client._identity_fallbacks) == [
        "getDeviceType", "getSoftwareVersion", "getVendor"]


async def test_it_is_logged(caplog):
    client = _refusing(_client())

    await client.get_device_type()

    said = _messages(caplog)
    assert len(said) == 1
    assert "10.0.0.94" in said[0]
    assert "getDeviceType" in said[0]
    assert "400" in said[0]


async def test_it_is_logged_once_and_not_once_per_poll():
    """These are asked on every setup, and a warning per poll would be worse
    than the silence it replaces."""
    client = _refusing(_client())

    for _ in range(5):
        await client.get_device_type()

    assert list(client._identity_fallbacks) == ["getDeviceType"]


async def test_logged_once_per_question_not_once_per_client(caplog):
    client = _refusing(_client())

    for _ in range(3):
        await client.get_device_type()
        await client.get_software_version()

    assert len(_messages(caplog)) == 2


# --- and never becomes the failure itself -----------------------------------

async def test_a_client_without_the_attribute_starts_one():
    """This runs inside an except branch whose purpose is to keep setup
    alive, so it must never be what raises."""
    client = _refusing(_client())
    assert not hasattr(client, "_identity_fallbacks")

    await client.get_device_type()

    assert client._identity_fallbacks


async def test_an_exception_with_no_status_is_still_recorded():
    client = _client()

    client._note_identity_fallback("getDeviceType", ValueError("no status"))

    assert client._identity_fallbacks == {"getDeviceType": None}


async def test_a_timeout_still_propagates():
    """Only an HTTP status means the device answered. A timeout is the device
    not answering, and inventing an identity from that is how an entry that
    was merely slow ends up renamed."""
    client = _client()

    async def get(url, verify_ok=False):
        raise TimeoutError()
    client.get = get

    with pytest.raises(TimeoutError):
        await client.get_device_type()
