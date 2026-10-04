"""Config reads, the motion toggle and reboot on a device with no /cgi-bin/ at all.

An SL300 answers 404 to every CGI path it has, so before this the motion-detection
switch showed off while the camera had detection enabled, its toggle wrote nothing,
and the reboot button did nothing -- each failing as though the request were wrong
rather than the transport.
"""

import aiohttp
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.rpc2 import Rpc2MethodRefused


class _Resp:
    """Just enough aiohttp response for _request to work against."""

    def __init__(self, status=200, body="OK"):
        self.status = status
        self._body = body
        self.headers = {}
        self.closed = False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status, message="Not Found"
            )

    async def text(self):
        return self._body

    def close(self):
        self.closed = True


def _fake_cgi(monkeypatch, status=404, body="OK"):
    """Make every CGI request answer with this status. Returns the call log."""
    calls = []

    class _FakeDigest:
        def __init__(self, *args, **kwargs):
            pass

        async def request(self, method, url, **kwargs):
            calls.append(url)
            return _Resp(status, body)

    monkeypatch.setattr(client_module, "DigestAuth", _FakeDigest)
    return calls


class _FakeRpc2:
    """A config store that records what was written back."""

    def __init__(self, tables):
        self.tables = tables
        self.reads = []
        self.writes = []
        self.methods = []

    async def get_config(self, params):
        name = params["name"]
        self.reads.append(name)
        # This firmware puts the table at the top level, not under params.
        return {"result": True, "table": self.tables.get(name)}

    async def set_configs(self, configs):
        self.writes.extend(configs)
        for name, table in configs:
            self.tables[name] = table
        return {"result": True}

    async def request(self, method, params=None, **kwargs):
        self.methods.append(method)
        return {"result": True, "params": None}


class _Holder:
    def __init__(self, client):
        self.client = client
        self.task = object()
        self.keepalive = None


def _client(monkeypatch, rpc2, address="cam"):
    client = DahuaClient("u", "p", address, 80, 554, object())
    holder = _Holder(rpc2)

    async def _shared(self):
        return holder

    monkeypatch.setattr(DahuaClient, "_shared_rpc2", _shared)
    return client


MOTION = [{"Enable": True, "Level": 3, "MotionDetectWindow": [{"Region": [1, 2]}]}]


async def test_a_config_read_that_404s_is_answered_over_rpc2(monkeypatch):
    """The read is not failed, it is asked again on the transport that exists."""
    cgi = _fake_cgi(monkeypatch, 404)
    rpc2 = _FakeRpc2({"MotionDetect": [{"Enable": True}]})
    client = _client(monkeypatch, rpc2)

    result = await client._request(
        "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"
    )

    assert rpc2.reads == ["MotionDetect"]
    assert result, "the RPC2 answer was not returned to the caller"
    assert len(cgi) == 1


async def test_the_host_is_remembered_so_the_second_read_skips_cgi(monkeypatch):
    """One 404 is the price of learning; paying it on every read is not."""
    cgi = _fake_cgi(monkeypatch, 404)
    rpc2 = _FakeRpc2(
        {"MotionDetect": [{"Enable": True}], "General": {"MachineName": "x"}}
    )
    client = _client(monkeypatch, rpc2)

    await client._request(
        "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"
    )
    assert len(cgi) == 1
    await client._request("/cgi-bin/configManager.cgi?action=getConfig&name=General")
    assert len(cgi) == 1, "the second read went to CGI again"
    assert rpc2.reads == ["MotionDetect", "General"]


async def test_a_404_on_something_that_is_not_a_config_read_still_raises(monkeypatch):
    """The fallback is for config reads, not a blanket 404 swallower."""
    _fake_cgi(monkeypatch, 404)
    client = _client(monkeypatch, _FakeRpc2({}))

    with pytest.raises(aiohttp.ClientResponseError):
        await client._request("/cgi-bin/snapshot.cgi?channel=1")


async def test_a_500_is_still_an_error_not_a_transport_hint(monkeypatch):
    """A device that is there and broken must not be read as a device without CGI."""
    _fake_cgi(monkeypatch, 500)
    rpc2 = _FakeRpc2({"MotionDetect": MOTION})
    client = _client(monkeypatch, rpc2)

    with pytest.raises(aiohttp.ClientResponseError):
        await client._request(
            "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"
        )
    assert rpc2.reads == []


async def test_motion_detection_toggle_falls_back_and_preserves_the_table(monkeypatch):
    """setConfig replaces the whole table, so everything else must survive."""
    _fake_cgi(monkeypatch, 404)
    rpc2 = _FakeRpc2({"MotionDetect": [dict(MOTION[0])]})
    client = _client(monkeypatch, rpc2)

    await client.enable_motion_detection(0, False)

    assert len(rpc2.writes) == 1
    name, table = rpc2.writes[0]
    assert name == "MotionDetect"
    assert table[0]["Enable"] is False
    # The keys the writer never heard of are still there.
    assert table[0]["Level"] == 3
    assert table[0]["MotionDetectWindow"] == [{"Region": [1, 2]}]


async def test_motion_detection_toggle_reports_a_channel_the_device_lacks(monkeypatch):
    """Better a clear error than an IndexError from inside a config write."""
    _fake_cgi(monkeypatch, 404)
    rpc2 = _FakeRpc2({"MotionDetect": [dict(MOTION[0])]})
    client = _client(monkeypatch, rpc2)

    with pytest.raises(ConnectionError, match="channel 3"):
        await client.enable_motion_detection(3, True)


async def test_a_table_that_is_an_object_not_a_list_is_written_too(monkeypatch):
    """MotionDetect is per channel here; other tables are a bare object."""
    _fake_cgi(monkeypatch, 404)
    rpc2 = _FakeRpc2({"General": {"MachineName": "Driveway", "LocalNo": 0}})
    client = _client(monkeypatch, rpc2)

    await client._rpc2_set_config_value("General", 0, "MachineName", "Gate")

    assert rpc2.writes[0][1] == {"MachineName": "Gate", "LocalNo": 0}


async def test_reboot_falls_back_to_magicbox_reboot(monkeypatch):
    """The button did nothing at all on a device with no CGI."""
    _fake_cgi(monkeypatch, 404)
    rpc2 = _FakeRpc2({})
    client = _client(monkeypatch, rpc2)

    result = await client.reboot()

    assert rpc2.methods == ["magicBox.reboot"]
    assert result == {"result": True}


async def test_reboot_does_not_swallow_a_real_failure(monkeypatch):
    """A 401 is the credentials, on every transport."""
    _fake_cgi(monkeypatch, 401)
    rpc2 = _FakeRpc2({})
    client = _client(monkeypatch, rpc2)

    with pytest.raises(aiohttp.ClientResponseError):
        await client.reboot()
    assert rpc2.methods == []


async def test_smart_motion_toggle_falls_back_too(monkeypatch):
    """The smart-motion switch is offered on these devices, so it needs it as well."""
    _fake_cgi(monkeypatch, 404)
    rpc2 = _FakeRpc2(
        {
            "SmartMotionDetect": [
                {
                    "Enable": True,
                    "Sensitivity": "Middle",
                    "ObjectTypes": {"Human": True, "Vehicle": False},
                },
            ]
        }
    )
    client = _client(monkeypatch, rpc2)

    await client.async_enabled_smart_motion_detection(0, False)

    name, table = rpc2.writes[0]
    assert name == "SmartMotionDetect"
    assert table[0]["Enable"] is False
    # ObjectTypes and Sensitivity are exactly what a rebuilt table would lose.
    assert table[0]["ObjectTypes"] == {"Human": True, "Vehicle": False}
    assert table[0]["Sensitivity"] == "Middle"


async def test_a_table_neither_transport_serves_reports_the_original_404(monkeypatch):
    """The fallback must be invisible when it cannot help.

    Raising the RPC2 refusal changed the exception a caller sees for an absent
    table from ClientResponseError to Rpc2MethodRefused. Callers that already
    tolerated a 404 do not catch that, so a read of RemoteDevice -- a table only a
    recorder has -- took an SL300's whole config entry down with it.
    """
    from custom_components.dahua.rpc2 import Rpc2MethodRefused

    _fake_cgi(monkeypatch, 404)

    class _Refusing(_FakeRpc2):
        async def get_config(self, params):
            raise Rpc2MethodRefused(
                "no such table", code=268959743, message="Unknown error"
            )

    client = _client(monkeypatch, _Refusing({}))

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        await client._request(
            "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice"
        )
    assert caught.value.status == 404


async def test_a_refused_table_is_not_asked_for_again(monkeypatch):
    """Once both transports have said no, stop paying for the round trip."""
    from custom_components.dahua.rpc2 import Rpc2MethodRefused

    _fake_cgi(monkeypatch, 404)
    asked = []

    class _Refusing(_FakeRpc2):
        async def get_config(self, params):
            asked.append(params["name"])
            raise Rpc2MethodRefused(
                "no such table", code=268959743, message="Unknown error"
            )

    client = _client(monkeypatch, _Refusing({}))
    url = "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice"

    for _ in range(2):
        with pytest.raises(aiohttp.ClientResponseError):
            await client._request(url)

    assert asked == ["RemoteDevice"], asked


# --- the other direction: RPC2 first, and what a failure there costs ---------
#
# Everything above is a device with no CGI, falling forward to RPC2. These are a device
# already reading config over RPC2 whose RPC2 read fails. It is a three way decision and
# the three outcomes are not interchangeable: two of them keep the transport and one
# throws it away for the life of the process.

MOTION_URL = "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"


def _rpc2_read_raises(monkeypatch, exception):
    """Make the RPC2 config read fail, leaving the decision under test isolated."""

    async def _boom(self, table):
        raise exception

    monkeypatch.setattr(DahuaClient, "_rpc2_get_config", _boom)


def _rpc2_client(monkeypatch, cgi_status=200, body="table.Enable=true"):
    calls = _fake_cgi(monkeypatch, status=cgi_status, body=body)
    client = DahuaClient("u", "p", "cam", 80, 554, object(), use_rpc2=True)
    return client, calls


async def test_a_table_the_device_declines_does_not_cost_the_transport(monkeypatch):
    """`Rpc2MethodRefused` means the transport reached the device and the device said
    no to this table. That is evidence RPC2 works here, so only the table is
    remembered."""
    client, calls = _rpc2_client(monkeypatch)
    _rpc2_read_raises(monkeypatch, Rpc2MethodRefused("declined"))

    await client._request(MOTION_URL)

    assert (client._rpc2_key(), "MotionDetect") in client_module._RPC2_TABLE_UNAVAILABLE
    assert client._rpc2_key() not in client_module._HOST_RPC2_UNAVAILABLE, (
        "a declined table wrote the whole host off, which is what put a working "
        "device back on a login per call"
    )
    assert calls, "the read was lost instead of falling through to CGI"


async def test_one_timeout_does_not_cost_the_transport(monkeypatch):
    """The fault rpc2_failure_is_permanent exists for. Nine reads timed out in one
    second on a recorder that had been serving RPC2 for two hours and went on being
    able to; every exception counting meant a login per call until a restart, silently,
    because the CGI fallback works."""
    client, calls = _rpc2_client(monkeypatch)
    _rpc2_read_raises(monkeypatch, TimeoutError())

    await client._request(MOTION_URL)

    assert client._rpc2_key() not in client_module._HOST_RPC2_UNAVAILABLE
    assert (client._rpc2_key(), "MotionDetect") not in (
        client_module._RPC2_TABLE_UNAVAILABLE
    ), "a timeout was recorded as the device refusing the table"
    assert calls, "the read was lost instead of falling through to CGI"


async def test_a_dropped_connection_does_not_cost_the_transport_either(monkeypatch):
    """The other half of TRANSIENT_RPC2_FAILURES."""
    client, calls = _rpc2_client(monkeypatch)
    _rpc2_read_raises(monkeypatch, aiohttp.ClientConnectionError("reset"))

    await client._request(MOTION_URL)

    assert client._rpc2_key() not in client_module._HOST_RPC2_UNAVAILABLE
    assert calls


async def test_a_device_that_cannot_serve_rpc2_is_written_off(monkeypatch):
    """The gate has to still close. A failure that is not transient and not a refusal
    means this device does not speak RPC2, and it should not pay for the attempt on
    every read from then on."""
    client, calls = _rpc2_client(monkeypatch)
    _rpc2_read_raises(monkeypatch, ValueError("not rpc2 at all"))

    await client._request(MOTION_URL)

    assert client._rpc2_key() in client_module._HOST_RPC2_UNAVAILABLE
    assert calls


async def test_a_refusal_is_checked_before_permanence(monkeypatch):
    """Rpc2MethodRefused is a ConnectionError, so it is not in
    TRANSIENT_RPC2_FAILURES and rpc2_failure_is_permanent says True about it. The
    refusal branch is only correct because it is tested first, which makes the order
    of those two checks load bearing rather than incidental."""
    from custom_components.dahua.client import rpc2_failure_is_permanent

    refusal = Rpc2MethodRefused("declined")
    assert rpc2_failure_is_permanent(refusal) is True, (
        "if this ever becomes False the ordering below stops mattering and this test "
        "should be rewritten rather than deleted"
    )

    client, _ = _rpc2_client(monkeypatch)
    _rpc2_read_raises(monkeypatch, refusal)

    await client._request(MOTION_URL)

    assert (
        client._rpc2_key() not in client_module._HOST_RPC2_UNAVAILABLE
    ), "the permanence check ran first and wrote the host off for a refusal"


async def test_only_the_declined_table_goes_back_to_cgi(monkeypatch):
    """The point of remembering the table rather than the host: everything else keeps
    using RPC2. Without this the first declined table cost the device its transport."""
    rpc2 = _FakeRpc2({"Lighting": [{"Mode": "Auto"}]})
    calls = _fake_cgi(monkeypatch, status=200, body="table.Enable=true")
    client = DahuaClient("u", "p", "cam", 80, 554, object(), use_rpc2=True)
    holder = _Holder(rpc2)

    async def _shared(self):
        return holder

    monkeypatch.setattr(DahuaClient, "_shared_rpc2", _shared)

    real = DahuaClient._rpc2_get_config

    async def _refuse_motion_only(self, table):
        if table == "MotionDetect":
            raise Rpc2MethodRefused("declined")
        return await real(self, table)

    monkeypatch.setattr(DahuaClient, "_rpc2_get_config", _refuse_motion_only)

    await client._request(MOTION_URL)
    cgi_after_refusal = len(calls)
    await client._request("/cgi-bin/configManager.cgi?action=getConfig&name=Lighting")

    assert "Lighting" in rpc2.reads, "the other table stopped using RPC2 too"
    assert (
        len(calls) == cgi_after_refusal
    ), "the Lighting read went to CGI as well, so the whole transport was lost"


async def test_a_verified_read_never_goes_over_rpc2(monkeypatch):
    """`verify_ok` is the credential check during setup. It has to reach the device's
    HTTP API, because that is the thing being verified."""
    # "OK" and nothing else: with verify_ok the body has to be exactly that, which is
    # the credential check itself and worth saying out loud here.
    client, calls = _rpc2_client(monkeypatch, body="OK")

    # Recorded rather than raised: _request catches everything around the RPC2 read
    # and falls through to CGI, so raising here would be swallowed and this test would
    # pass whether or not RPC2 was tried.
    attempts = []

    async def _record(self, table):
        attempts.append(table)
        raise Rpc2MethodRefused("should not have been asked")

    monkeypatch.setattr(DahuaClient, "_rpc2_get_config", _record)

    await client._request(MOTION_URL, verify_ok=True)

    assert attempts == [], "the credential check was attempted over RPC2"
    assert calls, "nothing was asked of the device at all"


async def test_a_verified_read_wants_ok_and_nothing_else(monkeypatch):
    """The other half of verify_ok: a device that answers something other than OK has
    not verified anything, so it must not read as success."""
    client, _ = _rpc2_client(monkeypatch, body="Error: bad credentials")

    with pytest.raises(Exception):
        await client._request(MOTION_URL, verify_ok=True)


async def test_the_ivs_rule_table_is_always_read_over_cgi(monkeypatch):
    """Per rule writes use CGI array indexes, so the read that resolves them has to be
    the CGI table even on a device that prefers RPC2. Reading it over RPC2 would hand
    back indexes that do not match what the write uses."""
    client, calls = _rpc2_client(monkeypatch)

    attempts = []

    async def _record(self, table):
        attempts.append(table)
        raise Rpc2MethodRefused("should not have been asked")

    monkeypatch.setattr(DahuaClient, "_rpc2_get_config", _record)

    await client._request(
        "/cgi-bin/configManager.cgi?action=getConfig&name=VideoAnalyseRule"
    )

    assert attempts == [], "VideoAnalyseRule was read over RPC2"
    assert calls
