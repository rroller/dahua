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
    return [
        r.getMessage()
        for r in caplog.records
        if "unidentified camera" in r.getMessage()
    ]


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
        "getDeviceType",
        "getSoftwareVersion",
        "getVendor",
    ]


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


# --- the fallbacks that decide the IDENTITY ----------------------------------
#
# ⭐ @Masterz69 on #1128 attached a dump reading, three lines apart:
#
#     serial_is_derived_from_credentials: true
#     identity_fallbacks: {}
#
# Both true as written, and together they say nothing. The three calls above
# record because they change a LABEL: a wrong vendor or model is cosmetic. The
# three below change the device's IDENTITY, because a serial that falls back
# becomes an md5 of address, RTSP port, username and password, and then the
# same camera returns as a new device the next time any of those changes
# (#805). Those three recorded nothing at all, so the field was empty in
# exactly the case somebody would consult it for.
#
# This is the shape the observability memory warns about: a dump field that
# answers a narrower question than the one it is being read for.


def _derived(client):
    return getattr(client, "identity_derived_from_credentials", False)


def _recorded(client):
    return dict(getattr(client, "_identity_fallbacks", {}) or {})


async def test_a_derived_serial_says_which_call_fell_back():
    client = _refusing(_client(), 400)

    info = await client.async_get_system_info()

    assert info["serialNumber"], "the identity itself is unchanged"
    assert _derived(client) is True
    # The call this test is about, asserted by name rather than by comparing
    # the whole dict: a device refusing everything also refuses `getSerialNo`,
    # and that record is a second true thing rather than this one being wrong.
    assert _recorded(client)["getSystemInfo (serial)"] == 400


async def test_a_derived_name_says_so_too():
    client = _refusing(_client(), 400)

    await client.get_machine_name()

    assert _derived(client) is True
    assert "getMachineName" in _recorded(client)


async def test_the_configured_machine_name_no_longer_swallows_the_status():
    """This one caught the exception without naming it, so not even the log
    said what the device had answered."""
    client = _refusing(_client(), 403)

    await client.async_get_machine_name()

    assert _derived(client) is True
    assert _recorded(client) == {"General.MachineName": 403}


# 404 and 501 are `CONFIG_CGI_ABSENT`, which goes looking for a monitor's
# identity over RPC2 before deciding anything, so they are a different path
# and not this one's business.
@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_whatever_the_status_the_reason_is_recorded(status):
    """The status is the useful half, and it is the whole point of recording
    the serial: a 401 is a credentials problem and a 400 is firmware that does
    not serve the action, and they produce the same invented identity."""
    client = _refusing(_client(), status)

    await client.async_get_system_info()

    assert _recorded(client)["getSystemInfo (serial)"] == status


async def test_an_invented_identity_is_never_silent():
    """⭐ The claim the #1128 dump disproved, asserted directly rather than
    one call at a time. Anything that sets the derived flag must also leave a
    reason behind it, or the dump says "invented" and cannot say why."""
    for call in (
        lambda c: c.async_get_system_info(),
        lambda c: c.get_machine_name(),
        lambda c: c.async_get_machine_name(),
    ):
        client = _refusing(_client(), 400)

        await call(client)

        assert _derived(client) is True
        assert _recorded(client), "derived an identity and recorded no reason"


# --- ask the other way before inventing one (#1128) --------------------------
#
# @Masterz69's DH-C4K-P answers `magicBox.cgi?action=getSystemInfo` with HTTP
# 400 and `magicBox.cgi?action=getSerialNo` with `sn=...`. So the device knows
# its serial perfectly well and was being given an invented one because only
# the refusing call was ever asked.
#
# A reported serial matters more than a label: the invented one is an md5 of
# address, RTSP port, username and password, so it moves the first time any of
# those changes and every entity is rebuilt under a new id (#805).
#
# The recorder and the doorbell here both answer `getSystemInfo`, so neither
# reaches this path. It is an extra question for the devices that need it.


def _answering_only_serial(client, serial="3E0164EPAN00095", status=400):
    """The measured DH-C4K-P: getSystemInfo refuses, getSerialNo answers."""
    asked = []

    async def get(url, verify_ok=False):
        asked.append(url)
        if "getSerialNo" in url:
            return {"sn": serial}
        raise _http(status)

    client.get = get
    return client, asked


async def test_a_reported_serial_is_used_instead_of_an_invented_one():
    client, _asked = _answering_only_serial(_client())

    info = await client.async_get_system_info()

    assert info["serialNumber"] == "3E0164EPAN00095"
    assert _derived(client) is False, "invented an identity the device had"


async def test_the_refusal_that_sent_us_there_is_still_recorded():
    """The dump must still say why the usual call was no good."""
    client, _asked = _answering_only_serial(_client())

    await client.async_get_system_info()

    assert _recorded(client)["getSystemInfo (serial)"] == 400


async def test_a_device_that_answers_normally_is_never_asked():
    """One extra round trip, only where the identity would be invented."""
    asked = []

    async def get(url, verify_ok=False):
        asked.append(url)
        return {"serialNumber": "REALSERIAL1234", "deviceType": "IPC"}

    client = _client()
    client.get = get

    await client.async_get_system_info()

    assert not [url for url in asked if "getSerialNo" in url]


async def test_a_device_refusing_both_still_gets_an_identity():
    """Setup has to survive. The hash is poor, and poor beats none."""
    client = _refusing(_client(), 400)

    info = await client.async_get_system_info()

    assert info["serialNumber"]
    assert _derived(client) is True


async def test_refusing_both_records_both():
    """So a dump says the second question was asked and also turned down."""
    client = _refusing(_client(), 400)

    await client.async_get_system_info()

    assert _recorded(client)["getSystemInfo (serial)"] == 400
    assert _recorded(client)["getSerialNo"] == 400


async def test_an_empty_serial_is_not_a_serial():
    """Taking it would give every device answering this way one identity."""
    client, _asked = _answering_only_serial(_client(), serial="   ")

    info = await client.async_get_system_info()

    assert _derived(client) is True
    assert info["serialNumber"] != ""


async def test_a_getserialno_that_raises_does_not_take_setup_down():
    """It runs inside the branch whose job is to keep the entry alive."""

    async def get(url, verify_ok=False):
        if "getSerialNo" in url:
            raise RuntimeError("something nobody predicted")
        raise _http(400)

    client = _client()
    client.get = get

    info = await client.async_get_system_info()

    assert info["serialNumber"]
    assert _derived(client) is True
