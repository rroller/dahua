"""A device that does not answer must not get its channels renumbered.

#724, on a DH-XVR5104C-X. Channels duplicated, two cameras disappeared, and the
arrangement was different after every restart:

    canal 1 | trasters -2 rampa   (DUPLICATED)
    canal 2 | trasters -2 rampa

Each entry probed `snapshot.cgi?channel=0` for itself, and a failure meant
"this device numbers from one":

    PROBE_FAILED = (ClientError, TimeoutError)

Six entries on one recorder therefore sent six identical requests through a
semaphore two wide, with the timeout covering the wait for a slot as well as the
request. Whoever timed out kept `channel + 1` and pointed at the next channel's
video, while the neighbour that won pointed at its own. Hence one camera shown
twice, one missing, and a different result every restart.

A timeout is not the device saying no. Only an HTTP status is.
"""
import asyncio

from aiohttp import ClientConnectionError, ClientResponseError

import custom_components.dahua as dahua
from custom_components.dahua import async_device_is_zero_indexed


def _clean():
    dahua._HOST_CHANNEL_BASE.clear()
    dahua._HOST_CHANNEL_BASE_LOCKS.clear()


class _Client:
    """Counts probes, and answers however the test says."""

    def __init__(self, outcome, delay=0):
        self.outcome = outcome
        self.delay = delay
        self.probes = 0

    async def async_probe_snapshot(self, channel_number):
        self.probes += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.outcome == "ok":
            return None
        if self.outcome == "refused":
            raise ClientResponseError(request_info=None, history=(), status=400)
        if self.outcome == "timeout":
            raise TimeoutError()
        if self.outcome == "connection":
            raise ClientConnectionError("no route")
        raise AssertionError("unknown outcome %r" % self.outcome)


# --- what the device actually said ------------------------------------------

async def test_a_snapshot_at_zero_means_zero_indexed():
    _clean()

    assert await async_device_is_zero_indexed(_Client("ok"), "d1") is True


async def test_an_http_error_means_one_indexed():
    """The device answered, and the answer was no."""
    _clean()

    assert await async_device_is_zero_indexed(_Client("refused"), "d1") is False


# --- what it did not say ----------------------------------------------------

async def test_a_timeout_decides_nothing():
    """The #724 bug. This used to read as one-indexed and renumber the channel."""
    _clean()

    assert await async_device_is_zero_indexed(_Client("timeout"), "d1") is None


async def test_a_dropped_connection_decides_nothing_either():
    _clean()

    assert await async_device_is_zero_indexed(_Client("connection"), "d1") is None


async def test_a_timeout_does_not_poison_the_answer_for_everyone():
    """A cached guess would be inherited by every other channel on the device."""
    _clean()
    await async_device_is_zero_indexed(_Client("timeout"), "d1")

    later = _Client("ok")

    assert await async_device_is_zero_indexed(later, "d1") is True
    assert later.probes == 1, "the timeout was cached and blocked a real answer"


# --- asked once for the device, not once per entry --------------------------

async def test_six_entries_probe_once():
    _clean()
    client = _Client("ok")

    for _ in range(6):
        await async_device_is_zero_indexed(client, "d1")

    assert client.probes == 1, "each entry probed for itself, which is the race"


async def test_entries_racing_at_startup_still_probe_once():
    """Six coordinators set up concurrently, which is what actually happens."""
    _clean()
    client = _Client("ok", delay=0.01)

    results = await asyncio.gather(
        *[async_device_is_zero_indexed(client, "d1") for _ in range(6)])

    assert client.probes == 1
    assert results == [True] * 6, "the entries did not agree: %r" % (results,)


async def test_every_entry_gets_the_same_answer_when_it_is_no():
    _clean()
    client = _Client("refused", delay=0.01)

    results = await asyncio.gather(
        *[async_device_is_zero_indexed(client, "d1") for _ in range(6)])

    assert results == [False] * 6
    assert client.probes == 1


# --- two devices are two questions ------------------------------------------

async def test_two_devices_on_one_address_are_decided_separately():
    """One address can answer for two devices on different ports."""
    _clean()
    zero = _Client("ok")
    one = _Client("refused")

    assert await async_device_is_zero_indexed(zero, "10.0.0.5:80") is True
    assert await async_device_is_zero_indexed(one, "10.0.0.5:8000") is False
