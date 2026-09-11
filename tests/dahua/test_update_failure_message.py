"""A failed poll must say what failed.

The coordinator used to raise `UpdateFailed()` bare, so Home Assistant logged
"Error fetching dahua data:" followed by nothing, and the integration's own
warning pointed at the README rather than naming the fault. Diagnosing an
intermittent device then means turning on debug logging and waiting for it to
happen again, for what is usually a one-word answer.

The exceptions raised here mostly carry no message of their own, which is the
whole reason the naive `str(exception)` is not enough.
"""

import asyncio

import aiohttp
import pytest

from custom_components.dahua import describe_update_failure


# --- the empty ones, which are the common case --------------------------------

def test_a_timeout_is_named_even_though_it_carries_no_message():
    """`str(asyncio.TimeoutError())` is "" -- the case that produced a blank log."""
    assert describe_update_failure(asyncio.TimeoutError()) == "TimeoutError"


def test_a_bare_client_error_is_named():
    assert describe_update_failure(aiohttp.ClientError()) == "ClientError"


@pytest.mark.parametrize("exception", [
    asyncio.TimeoutError(),
    aiohttp.ClientError(),
    aiohttp.ServerDisconnectedError(),
    ValueError(),
])
def test_no_exception_ever_describes_as_empty(exception):
    """Whatever arrives, the log line must not end in a blank."""
    assert describe_update_failure(exception).strip() != ""


# --- and when there is a message, keep it -------------------------------------

def test_a_message_is_preferred_over_the_class_name():
    """The class name is the fallback, not the answer."""
    assert describe_update_failure(ValueError("boom")) == "boom"


def test_surrounding_whitespace_is_not_reported_as_a_message():
    """A message of only whitespace is as useless as none, so fall back."""
    assert describe_update_failure(ValueError("   ")) == "ValueError"
