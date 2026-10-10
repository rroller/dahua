"""The siren and white light read the same two fields on both transports.

`CoaxialControlIOStatus` is the RPC2 side: `rpc2.get_coaxial_control_io_status`
builds one from the device's reply, and `client.async_get_coaxial_control_io_status_rpc2`
re-serialises it into the flat `status.Speaker` / `status.WhiteLight` keys the
coordinator reads. The CGI side skips all that and puts the device's own strings
into the poll's data.

So the same field is read by two rules depending on transport, and they
disagreed. Every CGI reader folds case:

    coordinator.is_siren_on()         get_status_value("Speaker").lower() == "on"
    coordinator.is_security_light_on()  get_status_value("WhiteLight").lower() == "on"

and this compared `== "On"` exactly. A firmware answering `on` or `ON` therefore
read as off over RPC2 and as on over CGI, for the same output on the same
device. Nothing had a test, which is why the two could drift.

The second half is the missing field. A `KeyError` here escapes
`_async_coaxial_status`, which catches refusals rather than shape errors, so it
would reach the poll's `gather` -- no `return_exceptions` -- and fail the whole
refresh over a device that reported one output and not the other. The CGI path
reads a missing key as off, so this does too.

This module imports nothing but the standard library, which is why it is worth
testing on its own.
"""

import pytest

from custom_components.dahua.models import CoaxialControlIOStatus


def _status(**fields):
    return {"params": {"status": fields}}


# --- the casing, which is the disagreement -----------------------------------


@pytest.mark.parametrize("value", ["On", "on", "ON", " On ", "oN"])
def test_every_spelling_of_on_is_on(value):
    status = CoaxialControlIOStatus(api_response=_status(Speaker=value))

    assert status.speaker is True


@pytest.mark.parametrize("value", ["Off", "off", "OFF", "", " "])
def test_every_spelling_of_off_is_off(value):
    status = CoaxialControlIOStatus(api_response=_status(Speaker=value))

    assert status.speaker is False


def test_the_two_outputs_are_read_separately():
    """They are different hardware: a camera can sound its siren without
    lighting anything."""
    status = CoaxialControlIOStatus(
        api_response=_status(Speaker="On", WhiteLight="Off")
    )

    assert status.speaker is True
    assert status.white_light is False


def test_a_value_that_is_not_a_word_is_not_on():
    """The device sends strings. Anything else is not it saying "on"."""
    assert CoaxialControlIOStatus(api_response=_status(Speaker=None)).speaker is False
    assert CoaxialControlIOStatus(api_response=_status(Speaker=1)).speaker is False


# --- and the shapes that must not fail a poll --------------------------------


def test_one_output_reported_and_not_the_other():
    status = CoaxialControlIOStatus(api_response=_status(Speaker="On"))

    assert status.speaker is True
    assert status.white_light is False


@pytest.mark.parametrize(
    "response", [{"params": {}}, {"params": None}, {}, {"params": {"status": None}}]
)
def test_a_reply_with_no_status_reads_as_off_rather_than_raising(response):
    """A KeyError here escapes _async_coaxial_status, which catches refusals and
    not shape errors, and the poll's gather has no return_exceptions -- so it
    would take every entity on the channel down for a cycle."""
    status = CoaxialControlIOStatus(api_response=response)

    assert (status.speaker, status.white_light) == (False, False)


def test_no_response_at_all_leaves_the_defaults():
    """The dataclass is also constructed without a response, and those two
    defaults are what the fields mean before anything has been read."""
    status = CoaxialControlIOStatus()

    assert (status.speaker, status.white_light) == (False, False)


def test_it_is_still_hashable():
    """`unsafe_hash=True` is on the dataclass, so something relies on putting
    these in a set or using one as a key. Keep that true."""
    assert len({CoaxialControlIOStatus(), CoaxialControlIOStatus()}) == 1
