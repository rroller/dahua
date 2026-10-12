"""A recorder is a device too, and its disks belong to it.

A recorder has no device of its own. `get_serial_number` appends the channel to
every channel but the lowest, so the lowest one's identifier is the bare serial,
which is the recorder's, and everything describing the box is filed against it:
four disk sensors on the DHI-NVR5464-16P-EI this was measured on, plus the
processor and the memory. That page is named after whatever camera is in slot 0.

#1053 stopped the page being *described* as the camera, because a recorder's
disks under a camera's model and serial is worse than a camera going
undescribed. This is the other half: give the box a device, move the handful of
entities that describe it, and let the lowest channel go back to being a camera.

Additive on purpose. Nothing that exists today changes its identity:

* the new device is `{serial}_host`, not the bare serial, so the device somebody
  has named after a camera is neither renamed nor emptied;
* the entities move by being re-added against a different device, which Home
  Assistant applies through `_async_update_entity(device_id=...)` inside
  `async_get_or_create`, so every `entity_id` survives and no automation breaks;
* a camera is left alone. It is one device already, and a parent above it would
  hold two entities over a child holding thirty.

The last migration to touch this registry destroyed 232 entities (the #827 merge
card), which is why none of this removes or renames anything.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.entity import DahuaBaseEntity, DahuaHostEntity

SERIAL = "BC0A198PAJ779DF"

RECORDER = {
    "get_serial_number": SERIAL,
    "get_device_serial_number": SERIAL,
    "host_device_identifier": SERIAL + "_host",
    "uses_separate_host_device": True,
    "get_device_name": "CCTV - CH1 Front Street",
    "get_model": "DHI-NVR5464-16P-EI",
    "get_configuration_url": "https://10.0.0.5",
    "get_firmware_version": "4.005.0000000.3.R",
    "get_channel_model": "H32_VSIPP-6DIRMD-I3",
    "get_channel_serial_number": "4L07AD6PAG5F0F2",
    "get_channel_firmware_version": "2.622.0000000.31.R",
    "get_channel_mac": "30:dd:aa:f6:24:9e",
    "configured_area_name": None,
}


class _Coordinator:
    """Everything the two device_info implementations read, spelled out.

    Spelled out rather than answered by `__getattr__` because
    test_a_double_provides_what_the_base_classes_read.py reads these files with
    ast and cannot see through a dynamic attribute, and neither can a reader.
    """

    def __init__(self, **overrides):
        self.values = dict(RECORDER, **overrides)

    def get_serial_number(self):
        return self.values["get_serial_number"]

    def get_device_serial_number(self):
        return self.values["get_device_serial_number"]

    def host_device_identifier(self):
        return self.values["host_device_identifier"]

    def stands_in_for_the_host(self):
        """Computed the way the real one is, rather than stated separately."""
        return (
            self.get_serial_number() == self.get_device_serial_number()
            and not self.uses_separate_host_device()
        )

    def uses_separate_host_device(self):
        return self.values["uses_separate_host_device"]

    def get_device_name(self):
        return self.values["get_device_name"]

    def get_model(self):
        return self.values["get_model"]

    def get_configuration_url(self):
        return self.values["get_configuration_url"]

    def get_firmware_version(self):
        return self.values["get_firmware_version"]

    def get_channel_model(self):
        return self.values["get_channel_model"]

    def get_channel_serial_number(self):
        return self.values["get_channel_serial_number"]

    def get_channel_firmware_version(self):
        return self.values["get_channel_firmware_version"]

    def get_channel_mac(self):
        return self.values["get_channel_mac"]

    def configured_area_name(self):
        return self.values["configured_area_name"]


class _Entry:
    def __init__(self, title="Gerty New"):
        self.title = title


def _host_info(title="Gerty New", **overrides):
    """What a disk or load sensor puts on its device page."""
    entity = object.__new__(DahuaHostEntity)
    entity._coordinator = _Coordinator(**overrides)
    entity.config_entry = _Entry(title)
    return entity.device_info


def _channel_info(**overrides):
    """What everything else on that channel puts on its device page."""
    entity = object.__new__(DahuaBaseEntity)
    entity._coordinator = _Coordinator(**overrides)
    entity.config_entry = _Entry()
    return entity.device_info


# --- the box gets a page of its own ------------------------------------------


def test_the_disks_land_on_a_device_of_their_own():
    assert _host_info()["identifiers"] == {(DOMAIN, SERIAL + "_host")}


def test_that_page_is_not_the_one_the_lowest_channel_uses():
    """The whole point. If these matched, the disks would be back on a page
    named after a camera and nothing would have been gained."""
    assert _host_info()["identifiers"] != _channel_info()["identifiers"]


def test_the_box_describes_itself():
    info = _host_info()

    assert info["model"] == "DHI-NVR5464-16P-EI"
    assert info["serial_number"] == SERIAL
    assert info["sw_version"] == "4.005.0000000.3.R"


def test_the_page_is_named_after_the_entry():
    """This is the device somebody is about to watch appear in their UI. The
    entry title is what they called this recorder when they added it; the model
    is the fallback for an entry with no title of its own."""
    assert _host_info(title="Gerty New")["name"] == "Gerty New"
    assert _host_info(title="")["name"] == "DHI-NVR5464-16P-EI"


def test_the_box_does_not_claim_a_cameras_mac():
    """The only MAC read here comes out of the recorder's slot table and belongs
    to a camera. `connections` is how Home Assistant decides two devices are the
    same hardware."""
    assert "connections" not in _host_info()


def test_the_box_is_not_put_in_a_cameras_area():
    """The area is a per channel option, and it means where that camera points."""
    assert "suggested_area" not in _host_info(configured_area_name="Server Room")


# --- and the lowest channel goes back to being a camera ----------------------


def test_the_lowest_channel_describes_its_camera_again():
    """#1053 made this page keep the recorder's identity, because the recorder's
    disks were sitting on it. They are not any more."""
    info = _channel_info()

    assert info["model"] == "H32_VSIPP-6DIRMD-I3"
    assert info["serial_number"] == "4L07AD6PAG5F0F2"
    assert info["sw_version"] == "2.622.0000000.31.R"
    assert info["connections"] == {("mac", "30:dd:aa:f6:24:9e")}


def test_a_recorder_with_no_device_of_its_own_keeps_the_old_arrangement():
    """A recorder that never answered the class question gets no host device, so
    its disks are still on the lowest channel and #1053 still applies there:
    that page has to keep saying what the recorder is."""
    info = _channel_info(uses_separate_host_device=False)

    assert info["model"] == "DHI-NVR5464-16P-EI"
    assert info["serial_number"] == SERIAL
    assert "connections" not in info


# --- a camera is already its own device --------------------------------------


def test_a_camera_with_an_sd_card_keeps_its_disk_sensor_where_it_is():
    """`_disk_reporter` returns a camera's coordinator when that camera has an SD
    card. There is no box to separate from the channel, so a parent device
    holding two entities above a child holding thirty is not an improvement."""
    camera = {
        "uses_separate_host_device": False,
        "get_channel_model": None,
        "get_channel_serial_number": None,
        "get_channel_firmware_version": None,
        "get_channel_mac": None,
        "get_model": "IPC-HDW5831R-ZE",
    }
    info = _host_info(**camera)

    assert info["identifiers"] == {(DOMAIN, SERIAL)}
    assert info == _channel_info(**camera), "it must be the ordinary page, not a copy"


# --- the entry where no channel holds the bare serial ------------------------


def test_an_entry_whose_lowest_channel_is_not_zero_still_gets_a_host_device():
    """Nothing here depends on a channel 0 existing.

    An entry configured with channels 2 to 5 has no device carrying the bare
    serial at all, so before this there was nowhere for the disks to go that was
    not a camera's page, and #1053's check never fired for it either.
    """
    slot_two = {"get_serial_number": SERIAL + "_2"}

    assert _host_info(**slot_two)["identifiers"] == {(DOMAIN, SERIAL + "_host")}
    # And that channel is a camera, as every channel but the lowest already was.
    assert _channel_info(**slot_two)["serial_number"] == "4L07AD6PAG5F0F2"


# --- the two rules, read off the real coordinator ----------------------------


def test_the_host_identifier_can_never_be_a_channels():
    """Both spellings come from the real functions, not from a copy of the
    format string. If `get_serial_number` ever learned a suffix that could spell
    `_host`, the recorder's page and a channel's page would silently become one
    device and the disks would be back where they started.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._serial_number = SERIAL

    host = DahuaDataUpdateCoordinator.host_device_identifier(coordinator)

    channels = set()
    for channel in range(64):
        coordinator._channel = channel
        channels.add(DahuaDataUpdateCoordinator.get_serial_number(coordinator))

    assert len(channels) == 64, "the channel identifiers are not all distinct"
    assert host not in channels


@pytest.mark.parametrize(
    "device_class,expected",
    [
        ("NVR", True),
        ("DVR", True),
        ("XVR", True),
        ("HCVR", True),
        # The doorbell measured here. A VTO is one device, and giving it a
        # parent holding two entities above a child holding thirty-four would
        # be worse than the arrangement this replaces.
        ("VTO", False),
        ("IPC", False),
        # Never answered, so nothing is assumed.
        ("", False),
    ],
)
def test_only_a_recorder_gets_a_device_of_its_own(device_class, expected):
    """Decided by the class the device reported, never by its model name, which
    is the bug in #570, #676 and #690."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._device_class = device_class

    assert DahuaDataUpdateCoordinator.uses_separate_host_device(coordinator) is expected
