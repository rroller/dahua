"""An IVS switch on an NVR channel must write the field, not the whole table.

#818 built the NVR path as a read-modify-write of the entire
`RemoteVideoAnalyseRule` table for a channel. Measured on a DHI-NVR5464, that is
refused:

    {"result": false, "errCode": 287638033, "message": "Request length error!"}

The table for one channel is 9KB to 22KB depending on how many rules it holds,
because each row carries its own `EventHandler` and `TimeSection` and runs to
4.6KB by itself. The refusal applies in both directions and to a write that
changes nothing, so all seven switches on that recorder were inert: a toggle
raised, and the switch sprang back.

Addressing the field makes the request 91 bytes, and the same recorder accepts it.
Measured: disable accepted and reads back `False`, restore accepted and reads back
`True`, no other field altered.

The read is deliberately unchanged and these tests pin it. It resolves `rule_id`
to an index, that index is what now addresses the row, and the `Id` a rule reports
differs between the whole-table and the per-channel read shapes on this firmware
(3 of 5 channels disagreed when measured). So a cheaper read here would write to
the wrong rule, silently.
"""

import asyncio
import json

import pytest

from custom_components.dahua.rpc2 import DahuaRpc2Client

CHANNEL = 9

# Two Normal rules. Id 2 sits at index 1, which is the point: the id and the
# index differ, so a test that conflated them would pass either way.
TABLE = [
    {
        "Class": "Normal",
        "Id": 1,
        "Name": "Rule1",
        "Enable": True,
        "EventHandler": {"TimeSection": [["1 00:00:00-23:59:59"]]},
    },
    {
        "Class": "Normal",
        "Id": 2,
        "Name": "Rule2",
        "Enable": True,
        "EventHandler": {"TimeSection": [["1 00:00:00-23:59:59"]]},
    },
]


class _Response:
    def __init__(self, payload):
        self._payload = payload

    async def text(self):
        return json.dumps(self._payload)


class _Session:
    """Answers the read then the write, and keeps every body posted."""

    def __init__(self, table=None):
        self.posts = []
        self._table = TABLE if table is None else table

    async def post(self, url, json=None):  # noqa: A002 - matches aiohttp
        self.posts.append(json)
        if json.get("method") == "configManager.getConfig":
            return _Response(
                {"id": 1, "result": True, "params": {"table": self._table}}
            )
        return _Response({"id": 2, "result": True})


def _write(enabled, rule_id="2", table=None):
    """Drive the shipped method, returning the session so bodies can be read."""
    session = _Session(table)

    async def go():
        client = DahuaRpc2Client("u", "p", "192.0.2.10", 80, 554, session)
        client._session_id = "session"
        await client.async_set_remote_ivs_rule_by_id(CHANNEL, rule_id, enabled)

    asyncio.run(go())
    return session


def _sent(session, method):
    return [post for post in session.posts if post.get("method") == method]


# --- the change itself ------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_the_write_addresses_one_field(enabled):
    """The name carries the channel and the index, and the value is the bool."""
    params = _sent(_write(enabled), "configManager.setConfig")[0]["params"]

    assert params["name"] == "RemoteVideoAnalyseRule[9][1].Enable"
    assert params["table"] is enabled


def test_the_whole_table_is_not_posted():
    """The regression this exists for. A body carrying the rules is the refused
    shape, whatever else is right about it."""
    params = _sent(_write(False), "configManager.setConfig")[0]["params"]

    assert not isinstance(params["table"], list)
    assert "EventHandler" not in json.dumps(params)


def test_the_body_stays_small_enough_to_be_accepted():
    """91 bytes was measured as accepted and 9KB to 22KB as refused. A couple of
    hundred bytes is the order this has to stay in, and a future change that
    reintroduced any rule content would blow straight through it."""
    body = _sent(_write(False), "configManager.setConfig")[0]

    assert len(json.dumps(body["params"])) < 200


def test_the_index_is_resolved_from_the_id_and_they_are_not_the_same():
    """Id 2 is at index 1 in the fixture. Writing `[9][2]` would target a rule
    that does not exist, which is why the fixture makes them differ."""
    params = _sent(_write(False, rule_id="2"), "configManager.setConfig")[0]["params"]

    assert "[9][1].Enable" in params["name"]

    params = _sent(_write(False, rule_id="1"), "configManager.setConfig")[0]["params"]

    assert "[9][0].Enable" in params["name"]


# --- and the read it depends on, which must not drift -----------------------


def test_the_rule_is_resolved_from_a_fresh_per_channel_read():
    """Load bearing twice over: the index now addresses the write, and the `Id`
    a rule reports differs between read shapes on this firmware. The
    `onlyLocal: False` plus `channel` form is required, not merely convenient."""
    reads = _sent(_write(False), "configManager.getConfig")

    assert len(reads) == 1, "the write must resolve against exactly one fresh read"
    params = reads[0]["params"]
    assert params["name"] == "RemoteVideoAnalyseRule"
    assert params["onlyLocal"] is False
    assert params["channel"] == CHANNEL


def test_the_read_happens_before_the_write():
    """Resolving after writing would address the row by a stale index."""
    methods = [post.get("method") for post in _write(False).posts]

    assert methods == ["configManager.getConfig", "configManager.setConfig"]


# --- and the failure that must stay a failure -------------------------------


def test_an_unknown_rule_writes_nothing():
    """Better to raise than to address an index that resolved to None and write
    to whatever sits there."""
    session = _Session()

    async def go():
        client = DahuaRpc2Client("u", "p", "192.0.2.10", 80, 554, session)
        client._session_id = "session"
        await client.async_set_remote_ivs_rule_by_id(CHANNEL, "404", False)

    with pytest.raises(ValueError, match="missing or ambiguous"):
        asyncio.run(go())

    assert _sent(session, "configManager.setConfig") == []


def test_a_duplicated_id_writes_nothing():
    """Two rules claiming one Id cannot be told apart, and #818 was right to
    exclude them. Pinned here because the write now trusts the index alone."""
    duplicated = [dict(row, Id=2) for row in TABLE]
    session = _Session(duplicated)

    async def go():
        client = DahuaRpc2Client("u", "p", "192.0.2.10", 80, 554, session)
        client._session_id = "session"
        await client.async_set_remote_ivs_rule_by_id(CHANNEL, "2", False)

    with pytest.raises(ValueError, match="missing or ambiguous"):
        asyncio.run(go())

    assert _sent(session, "configManager.setConfig") == []
