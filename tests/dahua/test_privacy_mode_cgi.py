"""LeLensMask over plain CGI, with RPC2 as the fallback.

#682 implemented privacy mode over RPC2 only. On #379 an IP4M-1041W answers
`Authority:check failure` to the RPC2 read -- which reads like a permissions
problem, and the reporter gave the account full admin rights trying to fix it --
while this works:

    configManager.cgi?action=setConfig&LeLensMask[0].Enable=true

So the camera has the table; only the transport was wrong. Plain CGI is also
what the Amcrest integration uses for this feature, and it costs no login of its
own, where the RPC2 path logs in and out around every call.

RPC2 stays as the fallback, because it is the route the feature was built and
verified on and a camera that answers only there must keep working.

The CGI answer counts only when it actually carries the key. async_get_config
swallows a ClientResponseError and returns {}, and a device can answer 200 with
an empty body for a table it does not have -- a recorder on #669 does exactly
that for VideoAnalyseRule. Neither is "privacy mode is off".
"""

import pytest

from custom_components.dahua.client import DahuaClient

ENABLED = {"table.LeLensMask[0].Enable": "true",
           "table.LeLensMask[0].TimeSection[0][0]": "0 00:00:00-24:00:00"}
DISABLED = {"table.LeLensMask[0].Enable": "false"}


class _Client(DahuaClient):
    """The real methods, with the two transports replaced by recorders."""

    def __init__(self, cgi, rpc2_value=None):
        self._cgi = cgi
        self._rpc2_value = rpc2_value
        self.urls = []
        self.rpc2_calls = []

    async def async_get_config(self, name):
        self.cgi_reads = getattr(self, "cgi_reads", [])
        self.cgi_reads.append(name)
        return dict(self._cgi) if self._cgi is not None else {}

    async def get(self, url, verify_ok=False):
        self.urls.append(url)
        return {}

    async def _async_privacy_mode_rpc2(self, action, description):
        self.rpc2_calls.append(description)
        if self._rpc2_value is None:
            raise AssertionError("RPC2 was used when CGI had the answer")
        return self._rpc2_value


# --- reading -----------------------------------------------------------------

async def test_cgi_is_read_when_the_camera_has_the_table():
    assert await _Client(ENABLED).async_get_privacy_mode() is True
    assert await _Client(DISABLED).async_get_privacy_mode() is False


async def test_rpc2_is_not_touched_when_cgi_answers():
    client = _Client(ENABLED)

    await client.async_get_privacy_mode()

    assert client.rpc2_calls == []


async def test_an_empty_cgi_reply_falls_back_to_rpc2():
    """#669's recorder answers 200 with no body for a table it does not have."""
    client = _Client({}, rpc2_value=True)

    assert await client.async_get_privacy_mode() is True
    assert client.rpc2_calls == ["privacy mode read"]


async def test_a_reply_without_the_key_falls_back_to_rpc2():
    """Some other table came back. That is not privacy mode being off."""
    client = _Client({"table.VideoInMode[0].Config[0]": "0"}, rpc2_value=False)

    assert await client.async_get_privacy_mode() is False
    assert client.rpc2_calls == ["privacy mode read"]


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), (" true ", True),
    ("false", False), ("", False), ("0", False),
])
async def test_the_enabled_value_is_read_the_way_the_device_writes_it(value, expected):
    client = _Client({"table.LeLensMask[0].Enable": value})

    assert await client.async_get_privacy_mode() is expected


# --- writing -----------------------------------------------------------------

async def test_the_write_goes_over_cgi_when_cgi_can_read_it():
    client = _Client(ENABLED)

    await client.async_set_privacy_mode(True)

    assert client.urls == [
        "/cgi-bin/configManager.cgi?action=setConfig&LeLensMask[0].Enable=true"]
    assert client.rpc2_calls == []


async def test_turning_it_off_writes_false():
    client = _Client(ENABLED)

    await client.async_set_privacy_mode(False)

    assert client.urls[-1].endswith("LeLensMask[0].Enable=false")


async def test_the_write_names_only_enable():
    """setConfig merges, so the camera keeps its own TimeSection schedule."""
    client = _Client(ENABLED)

    await client.async_set_privacy_mode(True)

    assert "TimeSection" not in client.urls[-1]


async def test_the_write_reaches_the_row_the_read_found():
    """A device need not put its lens mask on row 0.

    Reading any row while always writing row 0 would be a control that reports
    one thing and changes another -- the shape of #679, #683 and #689. Neither
    of my devices carries this table, so the row cannot be assumed.
    """
    client = _Client({"table.LeLensMask[2].Enable": "true"})

    assert await client.async_get_privacy_mode() is True

    await client.async_set_privacy_mode(False)

    assert client.urls == [
        "/cgi-bin/configManager.cgi?action=setConfig&LeLensMask[2].Enable=false"]


async def test_the_lowest_row_is_used_when_a_device_reports_several():
    """Not whichever key the dict happens to yield first."""
    client = _Client({"table.LeLensMask[3].Enable": "false",
                      "table.LeLensMask[1].Enable": "true"})

    assert await client.async_get_privacy_mode() is True

    await client.async_set_privacy_mode(True)

    assert "LeLensMask[1].Enable=true" in client.urls[-1]


async def test_a_table_without_an_enable_row_is_not_an_answer():
    """The device has the table but not the field, so nothing can be written."""
    client = _Client({"table.LeLensMask[0].TimeSection[0][0]": "0 00:00:00-24:00:00"},
                     rpc2_value=True)

    assert await client.async_get_privacy_mode() is True
    assert client.rpc2_calls == ["privacy mode read"]


async def test_the_write_falls_back_to_rpc2_when_cgi_cannot_read_it():
    client = _Client({}, rpc2_value=None)
    client._rpc2_value = "written"

    await client.async_set_privacy_mode(True)

    assert client.urls == [], "no CGI write on a camera CGI cannot read"
    assert client.rpc2_calls == ["privacy mode write"]


async def test_the_state_is_read_back_from_wherever_it_was_written():
    """The read and the write must never pick different transports."""
    cgi = _Client(ENABLED)
    await cgi.async_set_privacy_mode(True)
    await cgi.async_get_privacy_mode()

    assert cgi.rpc2_calls == []

    rpc2 = _Client({}, rpc2_value=True)
    await rpc2.async_set_privacy_mode(True)
    await rpc2.async_get_privacy_mode()

    assert rpc2.urls == []
    assert rpc2.rpc2_calls == ["privacy mode write", "privacy mode read"]
