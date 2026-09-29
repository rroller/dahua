"""A device refusing one optional capability should not take the camera offline.

The poll gathers a dozen reads and the coordinator's result depends on the gather, so any
exception from any one of them used to fail the whole entry for that cycle: every entity
stale, a host failure recorded against the count **all** of a recorder's channels share,
and the poll interval backed off.

Measured on the DHI-NVR5464 here. `coaxialControlIO.cgi?action=getStatus` answers `400` on
seven different channels at various times, and once ran for nine hours at about 109 an
hour:

    channel=1  23    channel=4   6    channel=8    3
    channel=2   2    channel=5  12    channel=12 949    channel=15  1

Each one produced `Failed to sync device state ... 400 Bad Request` at WARNING and an
`Error fetching dahua data` from the coordinator, for a recorder that was reachable and
answering everything else.

A `400` is the device understanding the request and refusing it, which is the same shape as
`LightingScheme` answering 400 on recorders. That is not a reason to report the camera as
broken.

**What this deliberately does not do is stop asking.** Those refusals recovered on their
own, so a capability somebody may rely on must not be switched off for the life of the
process by one bad answer. What changes is the cost of a refusal: a stale reading instead
of a failed poll, and one warning per channel instead of one every two minutes.
"""

from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

import custom_components.dahua as dahua
from custom_components.dahua import CAPABILITY_REFUSED

ADDRESS = "192.168.0.213"
DEVICE = "192.168.0.213:80"


@pytest.fixture(autouse=True)
def _clear_reported():
    """Reported-once state is module level, so it outlives a test."""
    dahua._CAPABILITY_REFUSALS_REPORTED.clear()
    yield
    dahua._CAPABILITY_REFUSALS_REPORTED.clear()


def _refusal(status):
    return ClientResponseError(None, None, status=status, message="Bad Request")


def _coordinator(raises=None, returns=None):
    """Just enough coordinator for the wrapper, which is all it touches."""
    calls = []

    async def async_get_coaxial_control_io_status(channel):
        calls.append(channel)
        if raises is not None:
            raise raises
        return returns

    coordinator = SimpleNamespace(
        _address=ADDRESS,
        client=SimpleNamespace(
            device_key=DEVICE,
            async_get_coaxial_control_io_status=async_get_coaxial_control_io_status),
        _calls=calls)
    coordinator._async_coaxial_status = (
        dahua.DahuaDataUpdateCoordinator._async_coaxial_status.__get__(coordinator))
    return coordinator


# --- the refusal must not propagate -----------------------------------------

@pytest.mark.parametrize("status", CAPABILITY_REFUSED)
async def test_a_refusal_yields_no_data_instead_of_raising(status):
    """None is what the gather's `if result is not None` already skips, so the reading
    holds its last value and nothing else in the poll is disturbed."""
    coordinator = _coordinator(raises=_refusal(status))

    assert await coordinator._async_coaxial_status(12) is None


async def test_the_400_this_recorder_actually_sends_is_covered():
    """Named on its own because 400 is the one measured here, and the pair already used
    for the CGI endpoints is (404, 501), which would have missed it."""
    assert 400 in CAPABILITY_REFUSED


async def test_a_reading_is_passed_through_when_the_device_answers():
    coordinator = _coordinator(returns={"table.CoaxialControlIO.WhiteLight": "On"})

    result = await coordinator._async_coaxial_status(12)

    assert result == {"table.CoaxialControlIO.WhiteLight": "On"}


async def test_the_channel_asked_for_is_the_channel_given():
    """On a recorder this is the channel number, not the index, and getting it wrong is
    how one camera reads another's siren."""
    coordinator = _coordinator(returns={})

    await coordinator._async_coaxial_status(12)

    assert coordinator._calls == [12]


# --- but a real failure still has to be one --------------------------------

@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_anything_that_is_not_a_refusal_still_raises(status):
    """A 401 has to reach the reauth path and a 500 is a device in trouble. Swallowing
    those would hide a camera that genuinely stopped working behind a stale reading."""
    coordinator = _coordinator(raises=_refusal(status))

    with pytest.raises(ClientResponseError):
        await coordinator._async_coaxial_status(12)


async def test_a_connection_failure_still_raises():
    """Not reachable is not the same as not supported, and only the second one is safe
    to absorb."""
    coordinator = _coordinator(raises=TimeoutError("gone"))

    with pytest.raises(TimeoutError):
        await coordinator._async_coaxial_status(12)


# --- and it says so once, not every poll ------------------------------------

async def test_the_refusal_is_recorded_so_it_is_only_reported_once():
    coordinator = _coordinator(raises=_refusal(400))

    await coordinator._async_coaxial_status(12)
    await coordinator._async_coaxial_status(12)

    assert dahua._CAPABILITY_REFUSALS_REPORTED == {(DEVICE, 12)}


async def test_each_channel_is_reported_in_its_own_right():
    """Seven channels of one recorder have done this, and being told about one of them
    would leave the other six invisible."""
    coordinator = _coordinator(raises=_refusal(400))

    await coordinator._async_coaxial_status(5)
    await coordinator._async_coaxial_status(12)

    assert dahua._CAPABILITY_REFUSALS_REPORTED == {(DEVICE, 5), (DEVICE, 12)}


async def test_two_devices_are_not_confused():
    """`device_key` is address:port, because one address can answer for two devices on
    different ports."""
    first = _coordinator(raises=_refusal(400))
    second = _coordinator(raises=_refusal(400))
    second.client.device_key = "192.168.0.232:80"

    await first._async_coaxial_status(1)
    await second._async_coaxial_status(1)

    assert dahua._CAPABILITY_REFUSALS_REPORTED == {
        (DEVICE, 1), ("192.168.0.232:80", 1)}


async def test_reporting_once_does_not_mean_asking_once():
    """The refusals here recovered on their own, so the capability is not disabled. Only
    the logging is quietened."""
    coordinator = _coordinator(raises=_refusal(400))

    for _ in range(4):
        await coordinator._async_coaxial_status(12)

    assert coordinator._calls == [12, 12, 12, 12], (
        "it stopped asking, which would switch off a capability that may recover")


# --- the poll has to use the wrapper ---------------------------------------

def test_the_poll_reads_the_status_through_the_wrapper():
    """A wrapper that works and a poll that still calls the client directly is the
    failure this repo keeps shipping, and driving the real poll needs the whole of Home
    Assistant. So this reads the source instead.

    It checks the `coros.append(...)` calls specifically rather than the whole function.
    `_async_update_data` also holds the one-time capability probe, which calls the client
    directly on purpose: its job is to decide whether coaxial control works at all, and
    its failure is already handled where it sits. Only what goes into the gather has to be
    wrapped, because only the gather's result is what the entry depends on."""
    import ast

    from .integration_source import definition

    update = definition("_async_update_data")

    gathered = "\n".join(
        ast.unparse(node) for node in ast.walk(update)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("coros.append"))

    assert "self._async_coaxial_status" in gathered, (
        "the poll does not gather through the wrapper, so a refusal still fails the entry")
    assert "async_get_coaxial_control_io_status(" not in gathered, (
        "the gather still calls the client directly, so the wrapper is bypassed")


# --- and the reported-once state is forgotten with the host -----------------

def test_the_reported_state_is_dropped_when_the_last_entry_for_a_host_goes(monkeypatch):
    """Otherwise a different device later given the same address inherits the silence and
    its first refusal is never reported."""
    dahua._CAPABILITY_REFUSALS_REPORTED.update({(DEVICE, 5), ("10.0.0.9:80", 1)})
    monkeypatch.setattr(
        dahua, "ir",
        SimpleNamespace(async_delete_issue=lambda *args, **kwargs: None))

    dahua._async_forget_host(SimpleNamespace(), ADDRESS)

    assert dahua._CAPABILITY_REFUSALS_REPORTED == {("10.0.0.9:80", 1)}
