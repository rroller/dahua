"""A recorder's placeholder MAC must not become a device connection.

#1078, on a DH-NVR4232-EI with an HWI-T221H on a channel. The recorder reports
`ff:ff:ff:ff:ff:ff` for a slot whose camera it cannot name, the channel's
device page offered that as its `connections` entry, and Home Assistant
refused the device:

    Invalid device info ... connections: {('mac', 'ff:ff:ff:ff:ff:ff')} ...
    are already registered for device ... of the same config entry

`connections` is how Home Assistant decides two devices are the same hardware,
so every channel the recorder could not name claimed to be one device. The
camera itself was working: 1.2 had no channel MAC at all, 1.3.0 added one, and
rolling back fixed it, which is what makes this a regression rather than a
device fault.
"""

import pytest

from custom_components.dahua.dahua_utils import usable_mac

# --- what counts as an address ----------------------------------------------


def test_a_real_mac_is_kept_as_the_device_reported_it():
    """Measured on a DHI-NVR5464-16P-EI: the recorder's slot table gives a
    camera's MAC in lower case with colons, and the page shows it as given."""
    assert usable_mac("30:dd:aa:f6:24:9e") == "30:dd:aa:f6:24:9e"


@pytest.mark.parametrize(
    "placeholder",
    [
        "ff:ff:ff:ff:ff:ff",
        "FF:FF:FF:FF:FF:FF",
        "ff-ff-ff-ff-ff-ff",
        "ffffffffffff",
        "00:00:00:00:00:00",
        "000000000000",
    ],
)
def test_a_placeholder_is_not_an_address(placeholder):
    """The broadcast address and the unspecified one, in the spellings a
    device might use. No camera can have either."""
    assert usable_mac(placeholder) is None


@pytest.mark.parametrize("absent", [None, "", "   "])
def test_nothing_is_nothing(absent):
    assert usable_mac(absent) is None


@pytest.mark.parametrize(
    "malformed", ["30:dd:aa:f6:24", "not-a-mac", "30:dd:aa:f6:24:9z"]
)
def test_something_that_is_not_a_mac_is_refused(malformed):
    """A malformed value in `connections` is worse than no value: that field
    is what merges two devices into one."""
    assert usable_mac(malformed) is None


def test_the_separators_a_device_might_use_are_accepted():
    assert usable_mac("30-DD-AA-F6-24-9E") == "30-DD-AA-F6-24-9E"


# --- and what the device page does with it -----------------------------------


def _coordinator(mac):
    from custom_components.dahua import DahuaDataUpdateCoordinator

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._remote_devices = {
        3: {"enabled": True, "mac": mac, "model": "HWI-T221H"}
    }
    coordinator._channel = 3
    return coordinator


def test_the_accessor_drops_a_placeholder():
    assert _coordinator("ff:ff:ff:ff:ff:ff").get_channel_mac() is None
    assert _coordinator("30:dd:aa:f6:24:9e").get_channel_mac() == "30:dd:aa:f6:24:9e"


def test_two_channels_the_recorder_cannot_name_do_not_collide():
    """The report itself. Two slots with the same placeholder produced two
    devices claiming one piece of hardware, and Home Assistant kept the first.
    """
    from custom_components.dahua import DahuaDataUpdateCoordinator

    table = {
        2: {"enabled": True, "mac": "ff:ff:ff:ff:ff:ff"},
        3: {"enabled": True, "mac": "ff:ff:ff:ff:ff:ff"},
    }
    macs = []
    for channel in (2, 3):
        coordinator = object.__new__(DahuaDataUpdateCoordinator)
        coordinator._remote_devices = table
        coordinator._channel = channel
        macs.append(coordinator.get_channel_mac())

    assert macs == [None, None], "two channels claiming the same hardware"
