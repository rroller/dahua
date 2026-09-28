"""A table the device will not serve must not cost the whole transport.

RPC2 exists so a poll costs one login instead of one per call (#636, #639).
`_request` routes config reads over it and, on failure, decides whether to
write the host off for the life of the process.

A device that answers `result=false` for one table was landing in that
"permanent" branch -- so a recorder that serves RPC2 perfectly well, but has no
LightingScheme table, lost RPC2 for *everything* and went back to a login per
call. Measured on a DHI-NVR5464-16P-EI running 0.10.9:

    ConnectionError: Dahua RPC2 method configManager.getConfig returned
    result=false (code=268959743, message=Unknown error! error code was not
    set in service!)
    -> "RPC2 config reads are not working for 192.168.0.213, using CGI instead"

and then, for the rest of the day, repeated 401s on ordinary polls -- the very
login storm RPC2 was introduced to stop (#577).

The device answering is evidence the transport works. It is the strongest
evidence available, and it was being read as the opposite.
"""

import pytest

from custom_components.dahua.client import (
    TRANSIENT_RPC2_FAILURES,
    rpc2_failure_is_permanent,
)
from custom_components.dahua.rpc2 import Rpc2MethodRefused

MEASURED = ("Dahua RPC2 method configManager.getConfig returned result=false "
            "(code=268959743, message=Unknown error! error code was not set in service!)")


def test_a_refusal_is_still_a_connection_error():
    """Subclassed so any handler written against the old type still catches it."""
    assert issubclass(Rpc2MethodRefused, ConnectionError)


def test_the_measured_refusal_is_recognisable():
    err = Rpc2MethodRefused(MEASURED)

    assert isinstance(err, Rpc2MethodRefused)
    assert "result=false" in str(err)


def test_a_refusal_is_not_a_transport_failure():
    """The distinction the fix turns on: the device answered."""
    assert not isinstance(Rpc2MethodRefused(MEASURED), TRANSIENT_RPC2_FAILURES)


@pytest.mark.parametrize("exception", [
    TimeoutError(),
    __import__("aiohttp").ClientConnectionError("closed"),
])
def test_transport_failures_are_still_not_permanent(exception):
    """Unchanged: a timeout must not write the host off either (#639)."""
    assert rpc2_failure_is_permanent(exception) is False


def test_an_unrecognised_failure_is_still_permanent():
    """A device that genuinely cannot speak RPC2 must still be written off."""
    assert rpc2_failure_is_permanent(ValueError("no RPC2 here")) is True
