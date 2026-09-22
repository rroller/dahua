"""Every write should say so at debug, because "no log line" is evidence.

#647 asked a question the integration could not answer. A camera's infrared
control did nothing when clicked, and the reporter enabled debug logging for
custom_components.dahua, clicked once, and found no request in the log at all.

They had set up the logging correctly. The write genuinely was not logged:

    async def async_set_lighting_v1_mode(...):
        url = ("/cgi-bin/configManager.cgi?action=setConfig"
               "&Lighting[{channel}][{profile}].Mode={mode}" ...)
        return await self.get(url)          # no log line anywhere

while its illuminator sibling `async_set_lighting_v2` logs "Turning light on".
So seven write methods announced themselves and the rest were silent, and
"nothing in the log" could not be read either way -- it meant "no request was
made" or "this method is one of the quiet ones", and there was no way to tell.

`get()` is the single point every write passes through, so that is where it
belongs rather than in thirty individual methods.
"""

import logging

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient


@pytest.fixture(autouse=True)
def _clean_read_cache():
    """The read path stores into module state keyed by device and user."""
    client_module._HOST_CACHE.clear()
    yield
    client_module._HOST_CACHE.clear()


def _client():
    return DahuaClient("admin", "pw", "10.0.0.5", 80, 554, None, False)


async def _run(client, url, caplog):
    sent = []

    async def _request(u, verify_ok=False, allow_rpc2=True):
        sent.append(u)
        return {}

    client._request = _request
    with caplog.at_level(logging.DEBUG, logger="custom_components.dahua"):
        await client.get(url)
    return sent


WRITE = ("/cgi-bin/configManager.cgi?action=setConfig"
         "&Lighting[0][0].Mode=Manual&Lighting[0][0].MiddleLight[0].Light=50")


async def test_a_write_is_logged(caplog):
    """The exact call #647 clicked and could not find."""
    client = _client()

    sent = await _run(client, WRITE, caplog)

    assert sent == [WRITE]
    assert any(WRITE in r.getMessage() for r in caplog.records), (
        "the write was sent and left no trace in the log")


async def test_the_log_names_the_device(caplog):
    """One log with several cameras in it needs to say which answered."""
    client = _client()

    await _run(client, WRITE, caplog)

    assert any("10.0.0.5" in r.getMessage() for r in caplog.records)


async def test_it_is_debug_not_warning(caplog):
    client = _client()

    await _run(client, WRITE, caplog)

    written = [r for r in caplog.records if "Writing to" in r.getMessage()]
    assert written, "no write log line at all"
    assert all(r.levelno == logging.DEBUG for r in written), (
        "a routine write must not be logged above debug")


@pytest.mark.parametrize("url", [
    "/cgi-bin/magicBox.cgi?action=getSystemInfo",
    "/cgi-bin/configManager.cgi?action=getConfig&name=Lighting",
    "/cgi-bin/coaxialControlIO.cgi?action=getStatus&channel=1",
])
async def test_reads_are_not_logged_as_writes(url, caplog):
    """Reads happen constantly; only writes are worth a line."""
    client = _client()
    client._request = _noop

    with caplog.at_level(logging.DEBUG, logger="custom_components.dahua"):
        await client.get(url)

    assert not [r for r in caplog.records if "Writing to" in r.getMessage()]


async def _noop(u, verify_ok=False, allow_rpc2=True):
    return {}
