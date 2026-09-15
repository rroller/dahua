"""The firmware sensor should still know the firmware on the second poll.

`_async_update_data` builds its `data` dict from scratch every cycle, and only
the one-time initialization branch ever puts a `version` in it. Anything reading
the version back out of `data` therefore answered once, about two minutes after
Home Assistant started, and answered nothing for the rest of the process.

The device registry kept its copy -- it is written while that first refresh is
still the current data -- so the device page went on showing a firmware while
the sensor added to make the same value templatable read `unknown`.
"""

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator(firmware):
    """A coordinator that has initialized and then polled at least once.

    `data` holds what a steady-state poll leaves behind: the per-poll reads and
    nothing else. That is the state the getters have to work in.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._firmware_version = firmware
    coordinator.data = {"table.MotionDetect[0].Enable": "true"}
    return coordinator


def test_the_firmware_survives_a_poll_that_did_not_read_it():
    coordinator = _coordinator("2.800.0000016.0.R,build:2020-06-05")

    assert coordinator.get_firmware_version() == "2.800.0000016.0.R,build:2020-06-05"


def test_the_build_date_survives_it_too():
    coordinator = _coordinator("2.800.0000016.0.R,build:2020-06-05")

    assert coordinator.get_build_date() == "2020-06-05"


def test_a_version_without_a_build_date_still_reports_the_version():
    """Older firmware reports no build date. That is not a missing firmware."""
    coordinator = _coordinator("3.120.0000.0.R")

    assert coordinator.get_firmware_version() == "3.120.0000.0.R"
    assert coordinator.get_build_date() == ""


def test_a_device_that_never_reported_one_reports_nothing_rather_than_raising():
    coordinator = _coordinator("")

    assert coordinator.get_firmware_version() == ""
    assert coordinator.get_build_date() == ""
