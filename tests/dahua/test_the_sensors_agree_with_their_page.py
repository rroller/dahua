"""A diagnostic sensor must not contradict the page it sits on.

Reported in #1043 on an Amcrest NV5232E-16P: a channel's device page showed the
camera's firmware `2.460.AC01.0.R` and its serial, while the Firmware version
and Serial number sensors on that same page showed the recorder's
`4.000.00AC000.0.R` and `AMR044032F4F06B33E`. Two answers to one question, a few
centimetres apart.

The page was right. #1044 made a recorder channel's page describe the camera in
that slot, and #1054 gave the box a page of its own to be described on, so there
is no longer any reason for a camera's page to report the recorder's identity.
These sensors had simply not been moved with it.

The rule lives on the coordinator as `stands_in_for_the_host`, in one place, so
the page and the sensors cannot drift apart again. They already had: the
expression was written out twice.
"""

import pytest

from custom_components.dahua.sensor import (
    DahuaFirmwareVersionSensor,
    DahuaSerialNumberSensor,
)

HOST_SERIAL = "AMR044032F4F06B33E"
HOST_FIRMWARE = "4.000.00AC000.0.R"
CAMERA_SERIAL = "BD01D6APAG74E89"
CAMERA_FIRMWARE = "2.460.AC01.0.R"


class _Coordinator:
    """What the two sensors read, spelled out.

    Not `__getattr__`: test_a_double_provides_what_the_base_classes_read.py
    reads these files with ast and cannot see through a dynamic attribute.
    """

    def __init__(
        self,
        stands_in=False,
        channel_serial=CAMERA_SERIAL,
        channel_firmware=CAMERA_FIRMWARE,
        build_date="2024-11-28",
    ):
        self._stands_in = stands_in
        self._channel_serial = channel_serial
        self._channel_firmware = channel_firmware
        self._build_date = build_date

    def stands_in_for_the_host(self):
        return self._stands_in

    def get_serial_number(self):
        return HOST_SERIAL if self._stands_in else HOST_SERIAL + "_15"

    def get_device_serial_number(self):
        return HOST_SERIAL

    def get_firmware_version(self):
        return HOST_FIRMWARE

    def get_channel_serial_number(self):
        return self._channel_serial

    def get_channel_firmware_version(self):
        return self._channel_firmware

    def get_build_date(self):
        return self._build_date

    # What the base class's extra_state_attributes reads, so the real one can
    # run rather than being patched out from under the test.
    data = {"id": "7"}


def _sensor(cls, **kwargs):
    entity = object.__new__(cls)
    coordinator = _Coordinator(**kwargs)
    # Both spellings: the base class reads `self.coordinator`, this
    # integration's own code reads `self._coordinator`.
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


# --- a camera's page reports the camera --------------------------------------


def test_the_firmware_sensor_reports_the_camera_on_a_channel_page():
    assert _sensor(DahuaFirmwareVersionSensor).native_value == CAMERA_FIRMWARE


def test_the_serial_sensor_reports_the_camera_on_a_channel_page():
    assert _sensor(DahuaSerialNumberSensor).native_value == CAMERA_SERIAL


# --- the box's page reports the box ------------------------------------------


def test_the_sensors_report_the_box_on_the_page_that_is_the_box():
    """A camera, or a recorder with no device of its own, whose lowest channel
    carries the bare serial. Everything on such a page describes the box,
    because the box has nowhere else to be described."""
    assert (
        _sensor(DahuaFirmwareVersionSensor, stands_in=True).native_value
        == HOST_FIRMWARE
    )
    assert _sensor(DahuaSerialNumberSensor, stands_in=True).native_value == HOST_SERIAL


def test_a_slot_the_recorder_said_nothing_about_falls_back_to_the_box():
    """Not every slot is described. Reporting nothing would be worse than
    reporting the box, which is at least true of the hardware involved."""
    nothing_known = {"channel_serial": None, "channel_firmware": None}

    assert (
        _sensor(DahuaFirmwareVersionSensor, **nothing_known).native_value
        == HOST_FIRMWARE
    )
    assert _sensor(DahuaSerialNumberSensor, **nothing_known).native_value == HOST_SERIAL


# --- and the build date goes with the firmware it belongs to -----------------


def test_the_build_date_is_dropped_when_the_firmware_is_the_cameras():
    """`get_build_date` is the box's. The camera's version comes out of the
    recorder's slot table with its date stripped (#1044), so pairing them would
    put the recorder's build date beside a camera's version: the same
    disagreement this file exists for, one level quieter."""
    assert (
        "build_date" not in _sensor(DahuaFirmwareVersionSensor).extra_state_attributes
    )


def test_the_build_date_stays_when_the_firmware_is_the_boxs():
    attrs = _sensor(DahuaFirmwareVersionSensor, stands_in=True).extra_state_attributes

    assert attrs["build_date"] == "2024-11-28"


def test_no_build_date_is_invented_when_the_device_reports_none():
    attrs = _sensor(
        DahuaFirmwareVersionSensor, stands_in=True, build_date=None
    ).extra_state_attributes

    assert "build_date" not in attrs


# --- what must not move ------------------------------------------------------


@pytest.mark.parametrize(
    "cls,suffix",
    [
        (DahuaFirmwareVersionSensor, "_firmware_version"),
        (DahuaSerialNumberSensor, "_serial_number"),
    ],
)
def test_the_entity_keys_do_not_move(cls, suffix):
    """Both keys are built from `get_serial_number`, the channel-suffixed
    bookkeeping key, and none of this touches it. A changed unique_id orphans
    the entity and takes its history and its automations with it, which is the
    one failure here with no recovery."""
    assert _sensor(cls).unique_id == HOST_SERIAL + "_15" + suffix
