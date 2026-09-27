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
                request_info=None, history=(), status=self.status, message="Not Found")

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
        "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect")

    assert rpc2.reads == ["MotionDetect"]
    assert result, "the RPC2 answer was not returned to the caller"
    assert len(cgi) == 1


async def test_the_host_is_remembered_so_the_second_read_skips_cgi(monkeypatch):
    """One 404 is the price of learning; paying it on every read is not."""
    cgi = _fake_cgi(monkeypatch, 404)
    rpc2 = _FakeRpc2({"MotionDetect": [{"Enable": True}], "General": {"MachineName": "x"}})
    client = _client(monkeypatch, rpc2)

    await client._request("/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect")
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
            "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect")
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
    rpc2 = _FakeRpc2({"SmartMotionDetect": [
        {"Enable": True, "Sensitivity": "Middle",
         "ObjectTypes": {"Human": True, "Vehicle": False}},
    ]})
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
            raise Rpc2MethodRefused("no such table", code=268959743, message="Unknown error")

    client = _client(monkeypatch, _Refusing({}))

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        await client._request(
            "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice")
    assert caught.value.status == 404


async def test_a_refused_table_is_not_asked_for_again(monkeypatch):
    """Once both transports have said no, stop paying for the round trip."""
    from custom_components.dahua.rpc2 import Rpc2MethodRefused

    _fake_cgi(monkeypatch, 404)
    asked = []

    class _Refusing(_FakeRpc2):
        async def get_config(self, params):
            asked.append(params["name"])
            raise Rpc2MethodRefused("no such table", code=268959743, message="Unknown error")

    client = _client(monkeypatch, _Refusing({}))
    url = "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice"

    for _ in range(2):
        with pytest.raises(aiohttp.ClientResponseError):
            await client._request(url)

    assert asked == ["RemoteDevice"], asked
