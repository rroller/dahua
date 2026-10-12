"""Which disk is in which bay: model, serial, firmware, bus, power mode.

The disk sensors were built on `storageDevice.cgi?action=getDeviceAllInfo`,
which answers state and capacity and nothing about the hardware. So a recorder
with four bays reported four disks and no way to tell which one to pull.

RPC2's `StorageDeviceManager.getDeviceInfos` answers the rest, and is served on
a DHI-NVR5464-16P-EI while every health-shaped method on the same service is
refused (268632080):

    getDeviceHealthInfos        REFUSED 268632080
    getSSDRemainServiceLife     REFUSED 268632080
    getSSDDataBaseDistribution  REFUSED 268632080
    getDeviceInfos              OK, 3897 characters

So the SMART ceiling recorded against #745 stands -- no temperature, no health
percentage, no remaining life. This is identity, not health, and it is the part
that was reachable all along and unused.

Measured, for /dev/sda on that recorder:

    Module "ST6000VX00232EF110"  SerialNo "ZAD3KYFP"  Firmware "SC60"
    BUS "SATA"  Media "DISK"  PowerMode "Active"  Capacity 6001175126016

Note the capacity disagrees with the CGI's summed partitions -- 6001175126016
against 5923181428736 for the same disk -- because one is the disk and the other
is what is formatted on it. Both are reported, under names that say which.
"""

import pytest

from custom_components.dahua.client import parse_storage_device_details

MEASURED = {
    "device": [
        {
            "BUS": "SATA",
            "Capacity": 6001175126016.0,
            "Firmware": "SC60",
            "Media": "DISK",
            "Module": "ST6000VX00232EF110",
            "Name": "/dev/sda",
            "PowerMode": "Active",
            "SerialNo": "ZAD3KYFP",
            "State": "Running",
        },
        {
            "BUS": "SATA",
            "Capacity": 16000900661248.0,
            "Firmware": "SC61",
            "Media": "DISK",
            "Module": "WDC_WD161KRYZ",
            "Name": "/dev/sdd",
            "PowerMode": "Active",
            "SerialNo": "2BJ8TAYZ",
            "State": "Running",
        },
    ]
}


# --- what it reads -----------------------------------------------------------


def test_each_disk_is_identified():
    found = parse_storage_device_details(MEASURED)

    assert found["/dev/sda"]["model"] == "ST6000VX00232EF110"
    assert found["/dev/sda"]["serial"] == "ZAD3KYFP"
    assert found["/dev/sda"]["firmware"] == "SC60"
    assert found["/dev/sda"]["bus"] == "SATA"
    assert found["/dev/sda"]["power_mode"] == "Active"
    assert found["/dev/sda"]["capacity_bytes"] == 6001175126016


def test_it_is_keyed_the_way_the_cgi_read_names_a_disk():
    """The two reads are joined on this. `/dev/sda`, not `sda` or an index: the
    caller looks each disk up by the name parse_storage_disks gave it."""
    found = parse_storage_device_details(MEASURED)

    assert sorted(found) == ["/dev/sda", "/dev/sdd"]


def test_every_bay_keeps_its_own_disk():
    found = parse_storage_device_details(MEASURED)

    assert found["/dev/sdd"]["serial"] == "2BJ8TAYZ"
    assert found["/dev/sdd"]["capacity_bytes"] == 16000900661248


# --- and what it refuses to do -----------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        None,
        "Error",
        {},
        {"device": None},
        {"device": "not a list"},
        {"device": [None, "nonsense"]},
    ],
    ids=["none", "string", "empty", "null device", "device not a list", "junk entries"],
)
def test_anything_unexpected_is_nothing_rather_than_a_failure(params):
    """This supplements a diagnostic sensor. A recorder answering in a shape
    nobody has seen must cost the attribute, never the sensor."""
    assert parse_storage_device_details(params) == {}


def test_a_disk_with_no_name_is_skipped():
    """Nothing could be joined to it, and a None key would collide with the
    next nameless one."""
    found = parse_storage_device_details({"device": [{"Module": "X", "Name": ""}]})

    assert found == {}


def test_a_field_the_recorder_left_empty_reads_as_absent():
    """None rather than "", because the sensor adds an attribute only where
    there is a value and a blank one would show as an empty row."""
    found = parse_storage_device_details(
        {"device": [{"Name": "/dev/sda", "Module": "", "SerialNo": "   "}]}
    )

    assert found["/dev/sda"]["model"] is None
    assert found["/dev/sda"]["serial"] is None


# --- what the sensor then shows ---------------------------------------------


def _sensor(disk):
    from custom_components.dahua.binary_sensor import DahuaDiskProblemBinarySensor

    sensor = object.__new__(DahuaDiskProblemBinarySensor)
    sensor._disk_name = disk["name"]
    sensor._coordinator = type(
        "_C",
        (),
        {
            "get_storage_disks": lambda self: [disk],
            "data": {"id": "SERIAL1"},
        },
    )()
    sensor.coordinator = sensor._coordinator
    return sensor


def _disk(**extra):
    base = {
        "name": "/dev/sda",
        "state": "Success",
        "health_flag": 0,
        "total_bytes": 5923181428736,
        "used_bytes": 5923181428736,
        "has_error": False,
        "healthy": True,
    }
    base.update(extra)
    return base


def test_the_sensor_says_which_disk_it_is():
    attributes = _sensor(
        _disk(
            model="ST6000VX00232EF110",
            serial="ZAD3KYFP",
            firmware="SC60",
            bus="SATA",
            power_mode="Active",
            capacity_bytes=6001175126016,
        )
    ).extra_state_attributes

    assert attributes["model"] == "ST6000VX00232EF110"
    assert attributes["disk_serial"] == "ZAD3KYFP"
    assert attributes["disk_firmware"] == "SC60"
    assert attributes["bus"] == "SATA"
    assert attributes["power_mode"] == "Active"


def test_the_disk_and_what_is_formatted_on_it_are_both_reported():
    """They differ by a few percent and mean different things. Naming only one
    of them would make a user reading the other one wrong."""
    attributes = _sensor(_disk(capacity_bytes=6001175126016)).extra_state_attributes

    assert attributes["capacity_gb"] == 6001.2
    assert attributes["total_gb"] == 5923.2


def test_a_recorder_that_cannot_say_grows_no_empty_attributes():
    """RPC2 off, or a device without the service. The sensor keeps everything it
    had and says nothing it does not know."""
    attributes = _sensor(_disk()).extra_state_attributes

    for absent in (
        "model",
        "disk_serial",
        "disk_firmware",
        "bus",
        "power_mode",
        "capacity_gb",
    ):
        assert absent not in attributes, absent
    # And still reports what the CGI read gave it.
    assert attributes["state"] == "Success"
    assert attributes["total_gb"] == 5923.2
