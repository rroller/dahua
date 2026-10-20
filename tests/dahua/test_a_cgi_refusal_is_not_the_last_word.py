"""A CGI refusal of the coaxial status is one transport's answer, not the device's.

#555. Measured on a DHI-NVR5464-16P-EI, both transports asked back to back in
the same minute, CGI asked twice per channel so a flaky answer would show:

    ch | CGI 1st | CGI 2nd | RPC2 CoaxialControlIO.getStatus
    1-4|  200    |  200    | OK   Speaker=Off WhiteLight=Off
    6  |  400    |  400    | error
    7  |  400    |  400    | OK   Speaker=Off WhiteLight=Off   <- the one that matters

Channel 7 refuses over CGI twice and answers over RPC2 at the same moment, so a
`400` means *that transport* refused rather than the capability being absent.
The wrapper treated it as the end of the question and held the last value for
ever, which on a flood light stops the lamp's own entity following the lamp.

`uses_rpc2_deterrence()` cannot cover this: it returns False for every recorder
channel by construction, and the recorder is where the split was measured.

The asymmetry in how often each transport is retried is the other half, and it
is deliberate rather than an oversight. CGI keeps being asked every poll because
those refusals were measured recovering on their own. RPC2 is asked once,
because "no such method" and "Authority:check failure" are both measured to mean
the firmware does not serve the call at all, and seven channels here refuse, so
retrying would buy a known answer at the price of a request per channel per poll.

Reintroduced against the real source, with the tests that failed:

| reintroduced | caught by |
| --- | --- |
| no second opinion after a CGI refusal | `answers_over_rpc2`, `the_reading_is_returned` |
| the channel not pinned afterwards | `pinned_to_rpc2`, `cgi_is_not_asked_again` |
| RPC2 retried on every poll | `asked_once_not_every_poll` |
| a stale login swallowed as "no such method" | `a_stale_login_still_escapes` |
| the fallback raising on an unrelated error | `a_broken_fallback_does_not_fail_the_poll` |
"""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua import coordinator as dahua
from custom_components.dahua.rpc2 import Rpc2MethodRefused

ADDRESS = "192.168.0.213"
DEVICE = "192.168.0.213:80"
CHANNEL = 7

# What the recorder answers over RPC2 for the channel CGI refuses, in the flat
# shape the coordinator reads.
RPC2_READING = {"status.Speaker": "Off", "status.WhiteLight": "On"}


@pytest.fixture(autouse=True)
def _forget_reported_refusals():
    dahua._CAPABILITY_REFUSALS_REPORTED.clear()
    yield
    dahua._CAPABILITY_REFUSALS_REPORTED.clear()


def _refusal(status):
    return ClientResponseError(None, None, status=status)


def _coordinator(rpc2_answer=RPC2_READING, cgi_status=400):
    """A coordinator whose CGI refuses and whose RPC2 answers, as the recorder does."""
    cgi_calls = []
    rpc2_calls = []

    async def async_get_coaxial_control_io_status(channel):
        cgi_calls.append(channel)
        raise _refusal(cgi_status)

    async def async_get_coaxial_control_io_status_rpc2(channel=0):
        rpc2_calls.append(channel)
        if isinstance(rpc2_answer, Exception):
            raise rpc2_answer
        return dict(rpc2_answer)

    coordinator = SimpleNamespace(
        _address=ADDRESS,
        uses_rpc2_deterrence=lambda: False,
        client=SimpleNamespace(
            device_key=DEVICE,
            async_get_coaxial_control_io_status=async_get_coaxial_control_io_status,
            async_get_coaxial_control_io_status_rpc2=(
                async_get_coaxial_control_io_status_rpc2
            ),
        ),
        cgi_calls=cgi_calls,
        rpc2_calls=rpc2_calls,
    )
    for name in ("_async_coaxial_status", "_async_coaxial_status_second_opinion"):
        setattr(
            coordinator,
            name,
            getattr(dahua.DahuaDataUpdateCoordinator, name).__get__(coordinator),
        )
    coordinator._previous_coaxial_status = (
        dahua.DahuaDataUpdateCoordinator._previous_coaxial_status.__get__(coordinator)
    )
    return coordinator


# --- the second opinion ------------------------------------------------------


async def test_a_channel_that_refuses_over_cgi_is_asked_over_rpc2():
    coordinator = _coordinator()

    await coordinator._async_coaxial_status(CHANNEL)

    assert coordinator.rpc2_calls == [CHANNEL], "the other transport was never tried"


async def test_the_reading_is_returned_rather_than_the_stale_one():
    """The point of the whole change: the device knew, and we were discarding it."""
    coordinator = _coordinator()
    coordinator.data = {"status.Speaker": "On", "status.WhiteLight": "Off"}

    result = await coordinator._async_coaxial_status(CHANNEL)

    assert result == RPC2_READING


async def test_the_channel_asked_over_rpc2_is_the_one_that_refused():
    """Getting this wrong is how one camera reads another camera's siren."""
    coordinator = _coordinator()

    await coordinator._async_coaxial_status(11)

    assert coordinator.rpc2_calls == [11]


# --- and then it stays there -------------------------------------------------


async def test_the_channel_is_pinned_to_rpc2_afterwards():
    coordinator = _coordinator()

    await coordinator._async_coaxial_status(CHANNEL)

    assert coordinator._coaxial_status_over_rpc2 is True


async def test_cgi_is_not_asked_again_once_rpc2_has_answered():
    """Otherwise every poll pays a refusal before getting to the transport that
    works, which is two requests a channel for a question already settled."""
    coordinator = _coordinator()

    for _ in range(3):
        await coordinator._async_coaxial_status(CHANNEL)

    assert coordinator.cgi_calls == [
        CHANNEL
    ], "it kept asking the transport that said no"
    assert coordinator.rpc2_calls == [CHANNEL] * 3


# --- when RPC2 refuses too ---------------------------------------------------


async def test_a_refusal_on_both_transports_holds_the_last_value():
    coordinator = _coordinator(
        rpc2_answer=Rpc2MethodRefused("no", code=268894210, message="Method not found!")
    )
    coordinator.data = {"status.Speaker": "On"}

    result = await coordinator._async_coaxial_status(CHANNEL)

    assert result == {"status.Speaker": "On"}


async def test_rpc2_is_asked_once_and_not_on_every_poll():
    """ "No such method" does not change while the process runs, and seven channels
    of this recorder refuse, so asking again each poll buys a known answer."""
    coordinator = _coordinator(
        rpc2_answer=Rpc2MethodRefused("no", code=268894210, message="Method not found!")
    )

    for _ in range(4):
        await coordinator._async_coaxial_status(CHANNEL)

    assert coordinator.rpc2_calls == [CHANNEL]
    assert (
        coordinator.cgi_calls == [CHANNEL] * 4
    ), "CGI must keep being asked; it recovers"


async def test_a_stale_login_still_escapes_instead_of_counting_as_no_method():
    """Treating an expired session as "this device has no siren" would drop a
    capability the device has, and silently."""
    stale = Rpc2MethodRefused(
        "stale", code=287637505, message="Invalid session in request data!"
    )
    coordinator = _coordinator(rpc2_answer=stale)

    with pytest.raises(Rpc2MethodRefused):
        await coordinator._async_coaxial_status(CHANNEL)


async def test_a_stale_login_does_not_use_up_the_one_attempt():
    """It was the session that failed, not the capability, so the question has
    not actually been answered yet."""
    stale = Rpc2MethodRefused(
        "stale", code=287637505, message="Invalid session in request data!"
    )
    coordinator = _coordinator(rpc2_answer=stale)

    with pytest.raises(Rpc2MethodRefused):
        await coordinator._async_coaxial_status(CHANNEL)

    assert coordinator._coaxial_status_rpc2_asked is False


async def test_a_broken_fallback_does_not_fail_the_poll():
    """The caller is already handling a refusal when it reaches here, so the
    second opinion must never be the thing that takes the entry offline."""
    coordinator = _coordinator(rpc2_answer=RuntimeError("no RPC2 on this device"))
    coordinator.data = {"status.WhiteLight": "On"}

    result = await coordinator._async_coaxial_status(CHANNEL)

    assert result == {"status.WhiteLight": "On"}


# --- the refusals that are not this ------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_a_real_failure_is_not_given_a_second_opinion(status):
    """A 401 has to reach reauth and a 500 is a device in trouble. Neither is a
    transport refusing a call it does not implement."""
    coordinator = _coordinator(cgi_status=status)

    with pytest.raises(ClientResponseError):
        await coordinator._async_coaxial_status(CHANNEL)

    assert coordinator.rpc2_calls == []


@pytest.mark.parametrize("failure", [TimeoutError("offline"), asyncio.CancelledError()])
async def test_a_transient_or_cancelled_fallback_can_recover_next_poll(failure):
    coordinator = _coordinator()
    calls = []

    async def read(channel):
        calls.append(channel)
        if len(calls) == 1:
            raise failure
        return RPC2_READING

    coordinator.client.async_get_coaxial_control_io_status_rpc2 = read
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await coordinator._async_coaxial_status(CHANNEL)
    else:
        assert await coordinator._async_coaxial_status(CHANNEL) is None

    assert await coordinator._async_coaxial_status(CHANNEL) == RPC2_READING
    assert calls == [CHANNEL, CHANNEL]
