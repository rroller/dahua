"""A device that explains a refusal must not have the explanation thrown away.

`request` read the reason from `resp_json["error"]["code"]` and `["message"]`. Some
refusals put it at the top level with no `error` object at all, and those arrived as
a bare "returned result=false" with nothing to act on.

Measured on a DHI-NVR5464 while testing #818's NVR write, which posts a whole
`RemoteVideoAnalyseRule` channel table, 9KB to 22KB depending on the rule count:

    {"result": false, "errCode": 287638033, "message": "Request length error!"}

That refusal reported as:

    Rpc2MethodRefused: Dahua RPC2 method configManager.setConfig returned result=false

The reason only came out by printing the raw response by hand, which cost a round
trip on the wrong hypothesis: with no code to read, the refusal looked like the table
being read-only, or like the value False being rejected, when the device had already
said it was the length of the request.

Session errors on the same firmware use the nested shape, checked with a forged
session id, so both are live and neither can be dropped:

    {"error": {"code": 287637505, "message": "Invalid session in request data!"}}

That matters beyond the message, because `client.py` switches on `exc.code` to decide
whether a login needs replacing.
"""

import asyncio
import json

import pytest

from custom_components.dahua.rpc2 import (
    DahuaRpc2Client,
    Rpc2MethodRefused,
    refusal_reason,
)

TOP_LEVEL = {
    "id": 9,
    "result": False,
    "session": "abc",
    "errCode": 287638033,
    "message": "Request length error!",
}

NESTED = {
    "id": 9,
    "result": False,
    "session": "abc",
    "error": {"code": 287637505, "message": "Invalid session in request data!"},
}


# --- the shape that was being dropped ---------------------------------------


def test_a_top_level_reason_is_read():
    """The measured #818 refusal. Both halves, because the code is what
    `client.py` switches on and the message is what a person reads."""
    code, message = refusal_reason(TOP_LEVEL)

    assert code == 287638033
    assert message == "Request length error!"


# --- the shape that already worked, which must not change -------------------


def test_a_nested_reason_is_still_read():
    code, message = refusal_reason(NESTED)

    assert code == 287637505
    assert message == "Invalid session in request data!"


def test_nested_wins_when_a_device_sends_both():
    """Preferring nested keeps every device that already worked unchanged, rather
    than making the answer depend on key order."""
    both = dict(NESTED)
    both["errCode"] = 1
    both["message"] = "top level"

    assert refusal_reason(both) == (287637505, "Invalid session in request data!")


# --- and a refusal that really says nothing --------------------------------


def test_a_refusal_with_no_reason_at_all_reports_none():
    """Not every device explains itself, and inventing a code would be worse than
    admitting there is none."""
    assert refusal_reason({"result": False}) == (None, None)


@pytest.mark.parametrize("error", [None, "a string", 7, []])
def test_a_non_dict_error_falls_back_to_the_top_level(error):
    """`error` is only trusted when it is actually an object. A device that sends
    `error: null` alongside a top level errCode is the measured case."""
    response = dict(TOP_LEVEL)
    response["error"] = error

    assert refusal_reason(response) == (287638033, "Request length error!")


# --- what the exception then carries ----------------------------------------


class _Response:
    def __init__(self, payload):
        self._payload = payload

    async def text(self):
        return json.dumps(self._payload)


class _Session:
    """Just enough aiohttp for `request`, which awaits `post` then `text`."""

    def __init__(self, payload):
        self._payload = payload
        self.posts = []

    async def post(self, url, json=None):  # noqa: A002 - matches aiohttp
        self.posts.append((url, json))
        return _Response(self._payload)


def _refuse(response):
    """Drive the real `request`, so this tests the shipped code and not a copy.

    Synchronous on purpose. Nothing here needs a Home Assistant loop or a
    fixture, and `asyncio.run` keeps the file runnable by a plain pytest that
    has no asyncio plugin, which is where it was proved.
    """

    async def go():
        client = DahuaRpc2Client("u", "p", "192.0.2.10", 80, 554, _Session(response))
        with pytest.raises(Rpc2MethodRefused) as caught:
            await client.request(method="configManager.setConfig", params={"name": "x"})
        return caught.value

    return asyncio.run(go())


def test_the_exception_carries_the_top_level_code_for_client_py_to_switch_on():
    """`client.py` compares `exc.code` against RPC2_SESSION_EXPIRED_CODE in three
    places. A code left at None there is a decision made on missing information."""
    error = _refuse(TOP_LEVEL)

    assert error.code == 287638033
    assert error.message == "Request length error!"


def test_the_message_a_person_reads_names_the_actual_problem():
    """The point of the change: the string alone should be enough to know the
    request was too long, without going back to the device."""
    text = str(_refuse(TOP_LEVEL))

    assert "Request length error!" in text
    assert "287638033" in text


def test_a_reason_free_refusal_is_not_dressed_up():
    """No trailing empty parenthesis, and nothing implied that was not said."""
    text = str(_refuse({"result": False}))

    assert text.endswith("returned result=false")
    assert "(" not in text
