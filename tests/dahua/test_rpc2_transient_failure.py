"""A read that did not come back is not a device that cannot answer.

Ruling RPC2 out is permanent for the life of the process, so it has to mean
"this device does not speak RPC2" rather than "this read timed out". Every
exception used to count, which meant one busy moment on a working recorder
switched it back to a login per call until Home Assistant was restarted --
silently, because the CGI fallback works.

Observed on a DHI-NVR5464-16P-EI running 0.9.96: nine reads timed out inside one
second, two hours after startup, on a host that had been serving RPC2 happily.
`rpc2_ruled_out_for_host` was true from then on.
"""

import asyncio

import aiohttp

from custom_components.dahua.client import rpc2_failure_is_permanent


# --- the failures that must not write a host off ------------------------------

def test_a_timeout_is_not_a_verdict():
    assert rpc2_failure_is_permanent(TimeoutError()) is False


def test_an_asyncio_timeout_is_not_a_verdict():
    """asyncio.timeout raises this; it is the builtin on modern Python."""
    assert rpc2_failure_is_permanent(asyncio.TimeoutError()) is False


def test_a_server_timeout_is_not_a_verdict():
    """aiohttp's read timeout; a TimeoutError subclass and equally transient."""
    assert rpc2_failure_is_permanent(aiohttp.ServerTimeoutError()) is False


def test_a_connection_failure_is_not_a_verdict():
    """A rebooting device refuses connections and then comes back."""
    assert rpc2_failure_is_permanent(aiohttp.ClientConnectionError()) is False


# --- and the ones that genuinely are ------------------------------------------

def test_a_device_that_answers_and_refuses_is_ruled_out():
    """An HTTP error means it replied; that is a statement about the device."""
    assert rpc2_failure_is_permanent(aiohttp.ClientResponseError(None, ())) is True


def test_a_malformed_answer_is_ruled_out():
    """RPC2 that returns something unparseable does not speak the protocol."""
    assert rpc2_failure_is_permanent(ValueError("no table in response")) is True


def test_an_unexpected_error_is_still_ruled_out():
    """Unknown failures keep the old behaviour rather than retrying forever."""
    assert rpc2_failure_is_permanent(KeyError("params")) is True
