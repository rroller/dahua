"""A Dahua CGI can decline a write with `200` and the body `Error`.

Measured on a DHI-NVR5464-16P-EI, with writes that set every field to the value it
already held, so nothing changed on the device:

    channel 3   Mode + MiddleLight[0].Light    HTTP 200   body 'Error'
    channel 6   Mode + MiddleLight[0].Light    HTTP 403   'Authority:check failure.'
    channel 3   Mode on its own                HTTP 403   'Authority:check failure.'
    channel 3   Mode + NearLight[0].Light      HTTP 403   'Authority:check failure.'

Channel 3's lighting row carries `NearLight` and `FarLight` and no middle bank, so
naming `MiddleLight` makes the device reject the request *before* it reaches the
authority check -- and it says so with a 200 and one word.

**Twenty-nine write methods read that as success.** `get()` has taken a `verify_ok`
flag all along, which raises unless the body is exactly "ok", and not one write
passed it.

`verify_ok` cannot simply be switched on for all of them: it demands "ok" and would
start failing every write to a device that answers an empty body or a body carrying
data, which is a far worse trade than the bug. So the check here is the narrow one
-- the single word that means failure is treated as failure, and everything else is
left alone. It can turn a silently broken write into a reported one and can never
turn a working write into a broken one.

This also settles something I got wrong twice. #937 reported "the device accepted
the change and did not make it"; I retracted it after retesting with `Mode` alone,
which is **not what the entity sends**. The entity sends both fields in one URL,
and that is the request that gets the 200.
"""

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    DahuaClient,
    DahuaWriteDeclined,
    WRITE_ERROR_BODY,
    write_was_declined,
)


class _Resp:
    """The shape test_cgi_absent_fallbacks.py already uses for this."""

    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.headers = {}
        self.closed = False

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError("not reached: these all answer 200")

    async def text(self):
        return self._body

    def close(self):
        self.closed = True


def _answering(monkeypatch, body, status=200):
    """Every CGI request answers with this status and body. Returns the call log."""
    calls = []

    class _FakeDigest:
        def __init__(self, *args, **kwargs):
            pass

        async def request(self, method, url, **kwargs):
            calls.append(url)
            return _Resp(status, body)

    monkeypatch.setattr(client_module, "DigestAuth", _FakeDigest)
    return calls


WRITE = "/cgi-bin/configManager.cgi?action=setConfig&Lighting[3][0].Mode=Manual"
READ = "/cgi-bin/configManager.cgi?action=getConfig&name=Lighting"


# --- what the word is ----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Error",
        "error",
        "ERROR",
        " Error ",
        "Error\r\n",
        "Error\nsomething the device added",
    ],
)
def test_the_device_declining_is_recognised(body):
    """Case and whitespace vary by firmware, and some add a second line."""
    assert write_was_declined(body) is True


@pytest.mark.parametrize(
    "body",
    [
        "OK",
        "ok",
        "ok\r\n",
        "",
        "   ",
        "\n",
        "Errors=0",
        "ErrorCode=0",
        "error_count=0",
        "table.Lighting[0][0].Mode=Auto",
        "An error occurred",
    ],
)
def test_everything_else_is_left_alone(body):
    """The whole point of being narrow. An empty body is not a refusal -- several
    write endpoints answer with nothing and the write lands -- and a body that
    merely contains the word is data, not a verdict."""
    assert write_was_declined(body) is False


def test_a_body_that_is_not_text_is_not_a_verdict():
    """parse_dahua_api_response is handed whatever came back, and a caller could
    reach this with bytes or None after a transport oddity. Guessing there would
    turn a transport problem into "the device declined", which is a different and
    wrong thing to tell a user."""
    for value in (None, b"Error", 0, [], {}):
        assert write_was_declined(value) is False


def test_the_word_is_named_once():
    """So the constant and the comparison cannot drift apart."""
    assert WRITE_ERROR_BODY == "error"
    assert write_was_declined(WRITE_ERROR_BODY.upper()) is True


# --- and what it is raised as --------------------------------------------------


def test_it_is_a_connection_error_so_existing_handlers_still_work():
    """Every write path already catches ConnectionError somewhere above it -- the
    entities translate one into "the device would not do that", which is exactly
    right for this. A new unrelated exception type would escape all of them and
    reach the user as an unhandled error."""
    assert issubclass(DahuaWriteDeclined, ConnectionError)


def test_it_is_its_own_type_so_a_caller_can_tell_the_difference():
    """It means the request arrived, was understood, and was declined. That is not
    a transport problem, and code that retries transport problems should not retry
    this."""
    assert DahuaWriteDeclined is not ConnectionError
    assert not issubclass(client_module.EventStreamClosed, DahuaWriteDeclined)


# --- and that it is actually asked for, which is the part worth proving --------
#
# The classifier above could be perfect and never called. The mutation that
# removes `reject_declined=True` from get() leaves every test above passing, which
# is exactly the "a scan I never ran" shape -- so these drive the real client.


async def test_a_write_answered_with_error_raises(monkeypatch):
    calls = _answering(monkeypatch, "Error")
    client = DahuaClient("u", "p", "recorder", 80, 554, object())

    with pytest.raises(DahuaWriteDeclined) as caught:
        await client.get(WRITE)

    assert calls, "the request was never made, so this proves nothing"
    assert "recorder" in str(caught.value)
    assert "Error" in str(caught.value)


async def test_a_write_answered_with_ok_does_not(monkeypatch):
    _answering(monkeypatch, "OK")
    client = DahuaClient("u", "p", "recorder", 80, 554, object())

    await client.get(WRITE)


async def test_a_write_answered_with_nothing_does_not(monkeypatch):
    """Several write endpoints answer with an empty body and the write lands."""
    _answering(monkeypatch, "")
    client = DahuaClient("u", "p", "recorder", 80, 554, object())

    await client.get(WRITE)


async def test_a_READ_answered_with_error_is_left_alone(monkeypatch):
    """Scoped to writes deliberately. A read that comes back odd is already handled
    by the fallbacks in _request, and failing reads on a body word would change
    behaviour for every device rather than fixing anything."""
    _answering(monkeypatch, "Error")
    client = DahuaClient("u", "p", "recorder", 80, 554, object())

    await client.get(READ)
