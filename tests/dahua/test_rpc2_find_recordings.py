"""The mediaFileFind sequence behind the media source (#472).

A recorder hands back a stateful finder object, so listing recordings is the
same factory / action / destroy dance as openDoor, and the destroy must happen
even when the find itself fails. Driven against the real DahuaRpc2Client with a
session that answers each method from a script and records the call order.
"""

import json

import pytest

from custom_components.dahua.rpc2 import DahuaRpc2Client, Rpc2MethodRefused

R1 = {
    "Channel": 0,
    "StartTime": "2026-10-04 12:00:00",
    "EndTime": "2026-10-04 13:00:00",
    "FilePath": "/mnt/dvr/a.dav",
    "Type": "dav",
    "Flags": ["Timing"],
}
R2 = dict(R1, StartTime="2026-10-04 13:00:00", FilePath="/mnt/dvr/b.dav")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    async def text(self):
        return json.dumps(self._payload)


class _Session:
    """Answers each method from `script`; records (method, object, params)."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    async def post(self, url, json):  # noqa: A002 - matches aiohttp's name
        self.calls.append((json["method"], json.get("object"), json.get("params")))
        answer = self.script[json["method"]]
        if callable(answer):
            answer = answer()
        return _Resp(answer)

    @property
    def methods(self):
        return [method for method, _obj, _params in self.calls]


def _client(script):
    client = DahuaRpc2Client("u", "p", "10.0.0.1", 80, 554, _Session(script))
    client._session_id = "s1"  # already logged in, so the find starts cleanly
    return client


async def test_it_collects_records_and_tears_the_finder_down():
    calls = {"n": 0}

    def next_file():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": True, "params": {"found": 2, "infos": [R1, R2]}}
        return {"result": True, "params": {"found": 0, "infos": []}}

    client = _client(
        {
            "mediaFileFind.factory.create": {"result": 77},
            "mediaFileFind.findFile": {"result": True},
            "mediaFileFind.findNextFile": next_file,
            "mediaFileFind.close": {"result": True},
            "mediaFileFind.destroy": {"result": True},
        }
    )

    records = await client.async_find_recordings(
        0, "2026-10-04 00:00:00", "2026-10-04 23:59:59"
    )

    assert records == [R1, R2]
    session = client._session
    assert session.methods[0] == "mediaFileFind.factory.create"
    assert session.methods[1] == "mediaFileFind.findFile"
    assert session.methods[-1] == "mediaFileFind.destroy"
    assert "mediaFileFind.close" in session.methods
    # A short list ends the loop in one findNextFile, not a second empty read.
    assert session.methods.count("mediaFileFind.findNextFile") == 1


async def test_the_find_carries_the_channel_the_window_and_the_object():
    client = _client(
        {
            "mediaFileFind.factory.create": {"result": 42},
            "mediaFileFind.findFile": {"result": True},
            "mediaFileFind.findNextFile": {"result": True, "params": {"infos": []}},
            "mediaFileFind.close": {"result": True},
            "mediaFileFind.destroy": {"result": True},
        }
    )

    await client.async_find_recordings(3, "2026-10-04 00:00:00", "2026-10-04 23:59:59")

    find = next(
        params
        for method, _obj, params in client._session.calls
        if method == "mediaFileFind.findFile"
    )
    assert find["condition"]["Channel"] == 3
    assert find["condition"]["StartTime"] == "2026-10-04 00:00:00"
    assert find["condition"]["EndTime"] == "2026-10-04 23:59:59"
    assert find["condition"]["Types"] == ["dav"]
    # Every call after the factory is addressed to the finder object it returned.
    assert all(
        obj == 42
        for method, obj, _params in client._session.calls
        if method != "mediaFileFind.factory.create"
    )


async def test_result_false_from_find_next_ends_the_list_without_raising():
    # The device signals "no more files" with result=false, which is the end of
    # the list, not a refusal; it must not raise through verify_result.
    client = _client(
        {
            "mediaFileFind.factory.create": {"result": 5},
            "mediaFileFind.findFile": {"result": True},
            "mediaFileFind.findNextFile": {"result": False},
            "mediaFileFind.close": {"result": True},
            "mediaFileFind.destroy": {"result": True},
        }
    )

    records = await client.async_find_recordings(0, "s", "e")

    assert records == []
    assert "mediaFileFind.destroy" in client._session.methods


async def test_it_pages_past_a_single_read_up_to_the_cap():
    calls = {"n": 0}

    def paged():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": True, "params": {"infos": [dict(R1) for _ in range(100)]}}
        return {"result": True, "params": {"infos": [dict(R2) for _ in range(5)]}}

    client = _client(
        {
            "mediaFileFind.factory.create": {"result": 9},
            "mediaFileFind.findFile": {"result": True},
            "mediaFileFind.findNextFile": paged,
            "mediaFileFind.close": {"result": True},
            "mediaFileFind.destroy": {"result": True},
        }
    )

    records = await client.async_find_recordings(0, "s", "e", max_results=200)

    assert len(records) == 105
    assert client._session.methods.count("mediaFileFind.findNextFile") == 2


async def test_no_finder_object_raises_before_any_find():
    client = _client({"mediaFileFind.factory.create": {"result": False}})

    with pytest.raises(ConnectionError):
        await client.async_find_recordings(0, "s", "e")

    # Nothing was created, so nothing is destroyed: only the factory was called.
    assert client._session.methods == ["mediaFileFind.factory.create"]


async def test_a_refused_find_still_destroys_the_finder():
    client = _client(
        {
            "mediaFileFind.factory.create": {"result": 3},
            "mediaFileFind.findFile": {
                "result": False,
                "error": {"code": 1, "message": "bad"},
            },
            "mediaFileFind.close": {"result": True},
            "mediaFileFind.destroy": {"result": True},
        }
    )

    with pytest.raises(Rpc2MethodRefused):
        await client.async_find_recordings(0, "s", "e")

    assert "mediaFileFind.destroy" in client._session.methods
