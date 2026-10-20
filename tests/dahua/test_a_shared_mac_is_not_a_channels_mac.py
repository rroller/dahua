"""A MAC that several slots report is not any one channel's MAC.

#1104, reported by benloy-debug on a recorder carrying several coax channels
plus IP channels. A coax channel has no camera with a MAC of its own, so the
recorder fills the slot in with its own, which is a perfectly valid address
and so survived the placeholder check added for #1078. `connections` is how
Home Assistant decides two devices are the same hardware and it allows one
device per MAC inside a config entry, so every channel after the first was
refused:

    Invalid device info ... connections: {('mac', '...')} ... are already
    registered for device ... of the same config entry

About 130 errors per startup, and the channel devices missing. The cameras
themselves worked; only their device pages could not be created.

The sibling of test_a_placeholder_mac_is_not_a_mac.py: same failure, same
field, but a real address, which is why the value alone cannot decide it.
"""

import pytest

from custom_components.dahua.dahua_utils import mac_digits, usable_mac

# A made-up address. Deliberately not the reporter's redacted prefix.
SHARED = "9a:14:63:11:22:33"
OWN = "30:dd:aa:f6:24:9e"


def _coordinator(table, channel):
    from custom_components.dahua import DahuaDataUpdateCoordinator

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._remote_devices = table  # noqa: SLF001
    coordinator._channel = channel  # noqa: SLF001
    return coordinator


# --- comparing two addresses ------------------------------------------------


def test_digits_are_the_address_without_its_spelling():
    assert mac_digits("9A:14:63:11:22:33") == "9a14631122 33".replace(" ", "")


@pytest.mark.parametrize(
    "spelling",
    ["9a:14:63:11:22:33", "9A-14-63-11-22-33", "9a14.6311.2233", " 9A:14:63:11:22:33 "],
)
def test_every_spelling_of_one_address_compares_equal(spelling):
    """The reason the count is taken on digits. Home Assistant normalises a
    connection before it stores it, so these are one address in the registry
    and four unequal strings in Python. Counting what `usable_mac` returns,
    which keeps the device's own spelling, would miss the collision that Home
    Assistant then refuses the device over."""
    assert mac_digits(spelling) == mac_digits(SHARED)


@pytest.mark.parametrize("absent", [None, "", "not a mac", "9a:14:63:11:22"])
def test_nothing_usable_has_no_digits(absent):
    assert mac_digits(absent) is None


# --- what the device page does with it --------------------------------------


def test_the_report_itself_two_coax_channels_sharing_the_recorders_mac():
    """Both are dropped, not just the second.

    Which slot Home Assistant happened to register first is not evidence
    about whose camera the address belongs to, and a wrong MAC in
    `connections` is worse than none, because that is the field that merges
    devices together.
    """
    table = {
        1: {"enabled": True, "mac": SHARED, "model": "Coax"},
        2: {"enabled": True, "mac": SHARED, "model": "Coax"},
    }

    assert _coordinator(table, 1).get_channel_mac() is None
    assert _coordinator(table, 2).get_channel_mac() is None


def test_the_ip_channel_on_the_same_recorder_keeps_its_own():
    """The other half of the report: channel 12 had a MAC of its own and was
    the one channel that worked. It must still work, exactly as reported."""
    table = {
        1: {"enabled": True, "mac": SHARED},
        2: {"enabled": True, "mac": SHARED},
        11: {"enabled": True, "mac": OWN},
    }

    assert _coordinator(table, 11).get_channel_mac() == OWN


def test_a_collision_spelled_two_ways_is_still_a_collision():
    """A recorder has no obligation to be consistent between slots, and Home
    Assistant would merge these two whatever it did."""
    table = {
        1: {"enabled": True, "mac": "9A-14-63-11-22-33"},
        2: {"enabled": True, "mac": "9a:14:63:11:22:33"},
    }

    assert _coordinator(table, 1).get_channel_mac() is None
    assert _coordinator(table, 2).get_channel_mac() is None


def test_one_camera_on_one_recorder_is_untouched():
    """The negative control. The count rule must not take the MAC away from
    the ordinary case it was never about."""
    table = {3: {"enabled": True, "mac": OWN}}

    assert _coordinator(table, 3).get_channel_mac() == OWN


def test_a_slot_with_no_mac_does_not_count_as_a_match():
    """Three slots the recorder said nothing about must not make each other
    look shared. `usable_mac` answers None for all of them, and None is not
    an address."""
    table = {
        1: {"enabled": True},
        2: {"enabled": True, "mac": ""},
        3: {"enabled": True, "mac": OWN},
    }

    assert _coordinator(table, 3).get_channel_mac() == OWN


def test_a_placeholder_shared_with_a_real_address_is_still_dropped():
    """#1078's rule still stands on its own. A broadcast MAC goes whether or
    not anything else reports it, because one channel with it is already
    wrong."""
    table = {
        1: {"enabled": True, "mac": "ff:ff:ff:ff:ff:ff"},
        2: {"enabled": True, "mac": OWN},
    }

    assert _coordinator(table, 1).get_channel_mac() is None
    assert _coordinator(table, 2).get_channel_mac() == OWN
    assert usable_mac("ff:ff:ff:ff:ff:ff") is None


# --- and what reaches Home Assistant ----------------------------------------


def test_the_device_page_offers_no_connections_for_a_shared_slot():
    """The assertion closest to the bug. Everything above is about the
    accessor; this is the dict Home Assistant was refusing.
    """
    from custom_components.dahua.entity import DahuaBaseEntity

    table = {
        1: {"enabled": True, "mac": SHARED},
        2: {"enabled": True, "mac": SHARED},
    }
    coordinator = _coordinator(table, 1)
    coordinator.stands_in_for_the_host = lambda: False
    coordinator.get_serial_number = lambda: "4E051BAPAZ8D724_1"
    coordinator.get_device_name = lambda: "NVR Ch2"
    coordinator.get_channel_model = lambda: "Coax"
    coordinator.get_model = lambda: "NVR"
    coordinator.get_configuration_url = lambda: None
    coordinator.get_channel_firmware_version = lambda: None
    coordinator.get_firmware_version = lambda: "4.0"
    coordinator.get_channel_serial_number = lambda: None
    coordinator.get_device_serial_number = lambda: "4E051BAPAZ8D724"
    coordinator.configured_area_name = lambda: None

    entity = object.__new__(DahuaBaseEntity)
    entity._coordinator = coordinator  # noqa: SLF001

    info = entity.device_info

    assert "connections" not in info
    assert info["identifiers"] == {("dahua", "4E051BAPAZ8D724_1")}
