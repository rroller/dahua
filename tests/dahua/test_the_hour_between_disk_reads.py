"""The hour between disk reads is counted from the disk read (#745, #1046).

#745 made the recorder's disks a once-at-setup read refreshed at most hourly,
because each one is a login the device writes to its own log. The pacing is a
timestamp and a gate in the poll:

    if self._storage_disks and time.time() - self._storage_last_refresh >= 3600:

#1046 then added a second, optional read to the same method -- RPC2's
`StorageDeviceManager.getDeviceInfos`, which says which disk is in which bay --
and gave it an early `return` of its own. The timestamp was already the last
statement in the method, so it ended up below that return.

RPC2 is off unless someone turns it on (`CONF_USE_RPC2` defaults to False in
config_flow.py), and `_async_rpc2_value` opens with an unconditional
`raise RuntimeError("RPC2 is disabled")`. So on a default recorder the optional
read always raises, the method always returns before its timestamp, the stamp
stays at 0.0 for the life of the entry, and `now - 0.0 >= 3600` is true on every
single refresh. One read an hour became one read a poll, and the poll is 30s by
default: 2,880 logged reads a day in place of 24.

Whether the hardware detail arrived says nothing about when the disks were last
read, so the stamp belongs to the read it paces.
"""

import ast
import time

from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

from .integration_source import definition

ADDRESS = "10.0.0.7"

# One disk from a DHI-NVR5464, trimmed to a single partition. The flat keys and
# the string-formatted byte counts are the device's own shapes.
DISK_INFO = {
    "list.info[0].Name": "/dev/sda",
    "list.info[0].State": "Success",
    "list.info[0].HealthDataFlag": "0",
    "list.info[0].Detail[0].IsError": "false",
    "list.info[0].Detail[0].TotalBytes": "1495797858304.000000",
    "list.info[0].Detail[0].UsedBytes": "1495797858304.000000",
    "list.info[0].Detail[0].Path": "/dev/sda0",
}

# What getDeviceInfos answers for that disk, keyed the way parse_storage_disks
# names it so the two can be joined.
DISK_DETAIL = {
    "/dev/sda": {
        "model": "ST6000VX00232EF110",
        "serial": "ZAD3KYFP",
        "firmware": "SC60",
    }
}

# The exact exception a device with RPC2 switched off raises, from
# `_async_rpc2_value`. Not a stand-in: an `except Exception` catching a
# different type is how a lenient read went untested in #1218.
RPC2_DISABLED = RuntimeError("RPC2 is disabled")


class _StorageClient:
    """Counts the reads, because the whole point of the gate is their number."""

    def __init__(self, info=None, info_raises=None, details_raises=None):
        self._info = DISK_INFO if info is None else info
        self._info_raises = info_raises
        self._details_raises = details_raises
        self.info_reads = 0
        self.detail_reads = 0

    async def async_get_storage_device_info(self):
        self.info_reads += 1
        if self._info_raises is not None:
            raise self._info_raises
        return dict(self._info)

    async def async_get_storage_device_details(self):
        self.detail_reads += 1
        if self._details_raises is not None:
            raise self._details_raises
        return {name: dict(d) for name, d in DISK_DETAIL.items()}


def _coordinator(client, device_class="NVR"):
    """Built the way this suite builds them, with only what the read touches.

    The class defaults supply `_storage_disks` and `_storage_last_refresh`,
    which is what those defaults are on the class for. The device class is set
    because the recorder gate is inside the read: a box that never said what
    it is reads no disks at all.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._address = ADDRESS
    coordinator._channel = 0
    coordinator._device_class = device_class
    coordinator._storage_disks = []
    coordinator._storage_last_refresh = 0.0
    return coordinator


# --- the stamp ---------------------------------------------------------------


async def test_the_stamp_is_set_when_the_hardware_detail_cannot_be_read():
    """The default case: RPC2 off, so the optional read raises every time.

    This is the whole bug. With the stamp below that read's `return`, a
    recorder that nobody switched RPC2 on for re-read its disks on every poll
    for the life of the entry.
    """
    coordinator = _coordinator(_StorageClient(details_raises=RPC2_DISABLED))

    await coordinator._async_refresh_storage()

    assert [d["name"] for d in coordinator.get_storage_disks()] == ["/dev/sda"]
    assert coordinator._storage_last_refresh > 0.0


async def test_the_stamp_is_set_when_the_hardware_detail_is_served():
    """And the case that already worked keeps working, detail and all."""
    coordinator = _coordinator(_StorageClient())

    await coordinator._async_refresh_storage()

    assert coordinator._storage_last_refresh > 0.0
    assert coordinator.get_storage_disks()[0]["serial"] == "ZAD3KYFP"


async def test_a_failed_disk_read_leaves_the_stamp_alone():
    """The opposite direction, and it must stay that way.

    Nothing was read, so nothing is paced: the next poll has to ask again. A
    fix that stamped the method on entry rather than on the answer would pass
    the test above and silence the retry this one is about.
    """
    coordinator = _coordinator(_StorageClient(info_raises=TimeoutError()))

    await coordinator._async_refresh_storage()

    assert coordinator.get_storage_disks() == []
    assert coordinator._storage_last_refresh == 0.0


async def test_the_second_read_inside_the_hour_is_the_one_being_prevented():
    """Two refreshes back to back still cost two reads, because this method has
    no gate of its own -- the poll has it, and the test below reads it.

    Here to keep the claim honest: what the stamp buys is the poll declining to
    call this, not this declining to read.
    """
    client = _StorageClient(details_raises=RPC2_DISABLED)
    coordinator = _coordinator(client)

    await coordinator._async_refresh_storage()
    await coordinator._async_refresh_storage()

    assert client.info_reads == 2


# --- and the gate that reads it ----------------------------------------------


def test_the_poll_paces_the_disk_read_by_that_stamp():
    """Derived from the poll's own source rather than restated here.

    The two tests have to be joined by something: "the stamp is set" is only
    worth having because the poll is what reads it, and a stamp set against a
    gate that had been rewritten to read something else would be a green test
    for nothing. Driving the real poll needs the whole of Home Assistant, so
    this reads the gate, which is the pattern
    test_a_refused_capability_does_not_fail_the_poll.py already uses.
    """
    update = definition("_async_update_data")

    gates = [
        ast.unparse(node.test)
        for node in ast.walk(update)
        if isinstance(node, ast.If)
        and "_storage_last_refresh" in ast.unparse(node.test)
    ]

    assert len(gates) == 1, gates
    gate = gates[0]
    # An hour, measured from the stamp the read sets.
    assert "self._storage_last_refresh" in gate
    assert "3600" in gate
    # And still only on something that found disks, so no camera pays for it.
    assert "self._storage_disks" in gate
