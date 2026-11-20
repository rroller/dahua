"""A recorder's disks, when the read that finds them did not answer (#1213).

The disks are read once at setup and then refreshed at most hourly, and the
hourly refresh is gated on disks having been found:

    if self._storage_disks and time.time() - self._storage_last_refresh >= 3600:

So a recorder that was busy for the one read at setup was never asked again.
`_async_refresh_storage` caught everything into a debug line and recorded
nothing either, so the question was not re-asked, and the diagnostics dump did
not say it had gone unanswered. No disk sensors for the life of the entry, and
nothing anywhere saying why.

What a refusal was is the distinction #1213 turned up, and it decides whether
asking again is worth anything:

- a 400 or a 403 is the recorder saying it does not serve that read, which is a
  fact about the model, so it is recorded and not chased;
- a timeout is the recorder saying nothing, which is a fact about that moment,
  so it is chased for as long as the retry budget lasts.

The disk payloads here are the DHI-NVR5464-16P-EI's own shapes.
"""

import aiohttp
import pytest

from custom_components.dahua.coordinator import (
    RETRYABLE_CAPABILITIES,
    DahuaDataUpdateCoordinator,
)

ADDRESS = "10.0.0.7"

DISK_INFO = {
    "list.info[0].Name": "/dev/sda",
    "list.info[0].State": "Success",
    "list.info[0].HealthDataFlag": "0",
    "list.info[0].Detail[0].IsError": "false",
    "list.info[0].Detail[0].TotalBytes": "1495797858304.000000",
    "list.info[0].Detail[0].UsedBytes": "1495797858304.000000",
    "list.info[0].Detail[0].Path": "/dev/sda0",
}


def _refused(status=400):
    """What the device raises when it answers and declines.

    A real `aiohttp.ClientResponseError`, not a stand-in carrying a `status`
    attribute: in #1218 a stand-in let a mutation through, because the code
    under test cared about the type and the test did not.
    """
    return aiohttp.ClientResponseError(None, (), status=status, message="Bad Request")


class _StorageClient:
    def __init__(self, info=None, info_raises=None):
        self._info = DISK_INFO if info is None else info
        self._info_raises = info_raises
        self.info_reads = 0

    def answers_from_now_on(self, info=None):
        """The device coming back, which is what the retry exists for."""
        self._info_raises = None
        if info is not None:
            self._info = info

    async def async_get_storage_device_info(self):
        self.info_reads += 1
        if self._info_raises is not None:
            raise self._info_raises
        return dict(self._info)

    async def async_get_storage_device_details(self):
        # The optional RPC2 half, which is off by default. Raising is the
        # ordinary case, not an edge one.
        raise RuntimeError("RPC2 is disabled")


def _coordinator(client, device_class="NVR"):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._address = ADDRESS
    coordinator._channel = 0
    coordinator._device_class = device_class
    coordinator._storage_disks = []
    coordinator._storage_last_refresh = 0.0
    coordinator._probe_refusals = {}
    return coordinator


# --- what the probe records --------------------------------------------------


async def test_a_silent_read_is_recorded_as_unanswered():
    coordinator = _coordinator(_StorageClient(info_raises=TimeoutError()))

    await coordinator._async_refresh_storage()

    assert "storage" in coordinator.probes_that_did_not_answer()


async def test_a_refused_read_is_recorded_as_answered():
    """The recorder said it does not serve this. Kept, and not chased.

    It is the half that generalises: a model that answers 400 here will answer
    400 on the next poll and on the next entry, and spending three retries
    finding that out is what the status is there to prevent.
    """
    coordinator = _coordinator(_StorageClient(info_raises=_refused()))

    await coordinator._async_refresh_storage()

    assert coordinator.probes_that_did_not_answer() == set()
    assert coordinator._probe_refusals["storage"] == {
        "answered": True,
        "status": 400,
        "error": "ClientResponseError",
    }


async def test_a_recorder_with_no_disk_in_it_is_not_pending():
    """An empty answer is an answer. A bay with nothing in it reads like this,
    and a question nobody is waiting for must not hold anything open."""
    client = _StorageClient(info={})
    coordinator = _coordinator(client)

    await coordinator._async_refresh_storage()

    assert client.info_reads == 1
    assert coordinator.get_storage_disks() == []
    assert coordinator.probes_that_did_not_answer() == set()


async def test_the_hourly_refresh_does_not_file_a_pending_question():
    """⚠ The method is the hourly refresh as well as the probe.

    A recorder whose disks are already on screen and whose hourly read times
    out has not left a question unanswered: the answer is an entity. Filing it
    as pending would spend the retry budget, report the probe as unanswered in
    the dump, and hold the leftover-rows gate shut (#1114, #1125) over a read
    that has already succeeded.
    """
    client = _StorageClient()
    coordinator = _coordinator(client)
    await coordinator._async_refresh_storage()
    assert coordinator.get_storage_disks()

    client._info_raises = TimeoutError()
    await coordinator._async_refresh_storage()

    assert coordinator.probes_that_did_not_answer() == set()
    assert coordinator.get_storage_disks(), "the known disks are kept"


@pytest.mark.parametrize("device_class", ["IPC", "VTO", ""])
async def test_nothing_that_is_not_a_recorder_is_read_or_recorded(device_class):
    """⚠ Including the empty string, which is a device whose class probe has
    not answered yet.

    `is_recorder_host` is false for a camera and false for a box that has not
    said what it is, and those must not be told apart here: recording a
    pending question for the second would record one for every camera whose
    class read timed out, chasing each of them for three polls over disks it
    does not have and taking nothing away if it answered. A class that arrives
    late is `_async_retry_device_class`'s scope, and what it cannot rebuild is
    in its own log line.
    """
    client = _StorageClient()
    coordinator = _coordinator(client, device_class=device_class)

    await coordinator._async_refresh_storage()

    assert client.info_reads == 0
    assert coordinator.probes_that_did_not_answer() == set()
    assert coordinator._probe_refusals == {}


# --- and what the retry does with it -----------------------------------------


def test_storage_is_in_the_retry_registry():
    """Nothing outside it is ever re-asked, so this is the enrolment itself."""
    assert "storage" in RETRYABLE_CAPABILITIES


async def test_the_retry_finds_the_disks_the_setup_read_missed():
    client = _StorageClient(info_raises=TimeoutError())
    coordinator = _coordinator(client)
    await coordinator._async_refresh_storage()
    assert coordinator.get_storage_disks() == []

    client.answers_from_now_on()
    found = await coordinator._async_retry_storage()

    assert found is True, "True is what tells the platform to build the sensors"
    assert [d["name"] for d in coordinator.get_storage_disks()] == ["/dev/sda"]
    assert coordinator.probes_that_did_not_answer() == set()


async def test_the_retry_reports_nothing_while_the_device_is_still_silent():
    client = _StorageClient(info_raises=TimeoutError())
    coordinator = _coordinator(client)

    assert await coordinator._async_retry_storage() is False
    assert "storage" in coordinator.probes_that_did_not_answer()


async def test_the_retry_leaves_a_recorder_that_already_has_disks_alone():
    """Otherwise it is a second read of the same table in the same poll, on a
    device that logs every one, for a set of entities that already exist."""
    client = _StorageClient()
    coordinator = _coordinator(client)
    await coordinator._async_refresh_storage()
    reads = client.info_reads

    assert await coordinator._async_retry_storage() is False
    assert client.info_reads == reads


async def test_the_retry_applies_the_gate_inside_the_probe():
    """One judgment for setup and for the retry, which is @rroller's third
    finding on #1201. A camera reached the retry here because the registry is
    keyed on a name, not on what kind of device recorded it."""
    client = _StorageClient()
    coordinator = _coordinator(client, device_class="IPC")

    assert await coordinator._async_retry_storage() is False
    assert client.info_reads == 0
