"""A recorder channel's device page should describe the camera, not the recorder.

Every channel of a recorder reports the recorder's model, serial and firmware,
because that is what `magicBox.cgi getSystemInfo` and `getSoftwareVersion`
answer -- they are questions about the box being asked. So a device page for
"FRONT VERANDAH" said DHI-NVR5464-16P-EI, the recorder's serial, and the
recorder's firmware.

The recorder knows better, and it is the only thing that can say: a PoE camera
sits on the recorder's own network (10.2.2.x on the install this was measured
on) and nothing else can reach it to ask. Measured there, per slot:

    DeviceType  H32_VSIPP-6DIRMD-I3        <- the camera's model
    SerialNo    BD01D6APAG74E89            <- the camera's serial
    Mac         30:dd:aa:f6:24:9e
    Version     3.140.101B000.0.R,2024-11-28
    Vendor      Private

while the recorder itself reports 4.005.0000000.3.R. Another channel on the same
recorder carried a 2018 camera firmware, which is the sort of thing worth being
able to see.

Two deliberate restraints:

* `UserName` and `Password` are in the same slot, in clear, and are not read.
* the firmware's build date is dropped. Every other Dahua device here reports a
  bare version, and half the devices showing a date is worse than none of them.
"""

import pytest

from custom_components.dahua.dahua_utils import parse_remote_devices

UUID = "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_%d.%s"
BRACKET = "table.RemoteDevice[%d].%s"

MEASURED = {
    "DeviceType": "H32_VSIPP-6DIRMD-I3",
    "SerialNo": "BD01D6APAG74E89",
    "Mac": "30:dd:aa:f6:24:9e",
    "Version": "3.140.101B000.0.R,2024-11-28",
    "Vendor": "Private",
    "Enable": "true",
    "ProtocolType": "Private",
}


def _table(shape, channel=11, fields=None):
    return {
        shape % (channel, field): value for field, value in (fields or MEASURED).items()
    }


# --- what the table gives up -------------------------------------------------


@pytest.mark.parametrize("shape", [UUID, BRACKET], ids=["uuid-keyed", "bracketed"])
def test_the_cameras_own_identity_is_kept(shape):
    """Both key spellings, because firmwares disagree and this table has bitten
    the integration once already for reading only one of them."""
    slot = parse_remote_devices(_table(shape))[11]

    assert slot["model"] == "H32_VSIPP-6DIRMD-I3"
    assert slot["serial"] == "BD01D6APAG74E89"
    assert slot["mac"] == "30:dd:aa:f6:24:9e"
    assert slot["firmware"] == "3.140.101B000.0.R,2024-11-28"
    assert slot["vendor"] == "Private"


@pytest.mark.parametrize("shape", [UUID, BRACKET], ids=["uuid-keyed", "bracketed"])
def test_what_it_already_read_still_works(shape):
    """The two fields this parser existed for. A new field must not cost them."""
    slot = parse_remote_devices(_table(shape))[11]

    assert slot["enabled"] is True
    assert slot["protocol"] == "private"


def test_the_login_in_the_slot_is_not_read():
    """The recorder stores each camera's own username and password here, in
    clear. Nothing in this integration needs them, so nothing reads them."""
    table = _table(UUID)
    table[UUID % (11, "UserName")] = "admin"
    table[UUID % (11, "Password")] = "hunter2"

    slot = parse_remote_devices(table)[11]

    assert "hunter2" not in repr(slot)
    assert "admin" not in repr(slot)


def test_a_slot_that_says_nothing_still_has_every_key():
    """A reader does `slot.get("serial")` and must not meet a KeyError on a
    recorder that reports fewer fields than this one does."""
    slot = parse_remote_devices({UUID % (3, "Enable"): "true"})[3]

    for key in ("model", "serial", "mac", "firmware", "vendor"):
        assert slot[key] == "", key


def test_each_slot_keeps_its_own_camera():
    table = {}
    table.update(_table(UUID, channel=0, fields={"SerialNo": "FIRST"}))
    table.update(_table(UUID, channel=9, fields={"SerialNo": "SECOND"}))

    found = parse_remote_devices(table)

    assert found[0]["serial"] == "FIRST"
    assert found[9]["serial"] == "SECOND"


# --- what the coordinator makes of it ---------------------------------------


def _coordinator(slots):
    from custom_components.dahua import DahuaDataUpdateCoordinator

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._remote_devices = slots
    coordinator._channel = 11
    return coordinator


def test_the_accessors_report_the_camera():
    coordinator = _coordinator(parse_remote_devices(_table(UUID)))

    assert coordinator.get_channel_serial_number() == "BD01D6APAG74E89"
    assert coordinator.get_channel_mac() == "30:dd:aa:f6:24:9e"


def test_the_firmware_build_date_is_dropped():
    """ "3.140.101B000.0.R,2024-11-28" is how the recorder reports it. Every
    other device here reports a bare version, and the devices screen comparing
    one against the other is the reason this is trimmed."""
    coordinator = _coordinator(parse_remote_devices(_table(UUID)))

    assert coordinator.get_channel_firmware_version() == "3.140.101B000.0.R"


def test_a_channel_the_recorder_says_nothing_about_reports_none():
    """Which is every standalone camera, and any slot that answered emptily. The
    caller falls back to what the device says about itself, so None has to mean
    None rather than an empty string."""
    coordinator = _coordinator({})

    assert coordinator.get_channel_serial_number() is None
    assert coordinator.get_channel_mac() is None
    assert coordinator.get_channel_firmware_version() is None


def test_an_empty_slot_reports_none_too():
    coordinator = _coordinator(parse_remote_devices({UUID % (11, "Enable"): "true"}))

    assert coordinator.get_channel_serial_number() is None
    assert coordinator.get_channel_firmware_version() is None


# --- and what the device page shows -----------------------------------------


# Every method spelled out rather than answered by __getattr__, because
# test_a_double_provides_what_the_base_classes_read.py reads these files with
# ast and cannot see through dynamic attributes. It flagged this double, and it
# was right to: a reader cannot see what it answers either.
RECORDER_CHANNEL = {
    "get_serial_number": "NVRSERIAL_11",
    "get_device_serial_number": "NVRSERIAL",
    "get_device_name": "FRONT VERANDAH",
    "get_model": "DHI-NVR5464-16P-EI",
    "get_configuration_url": "https://10.0.0.5",
    "get_firmware_version": "4.005.0000000.3.R",
    "get_channel_model": "H32_VSIPP-6DIRMD-I3",
    "get_channel_serial_number": "BD01D6APAG74E89",
    "get_channel_firmware_version": "3.140.101B000.0.R",
    "get_channel_mac": "30:dd:aa:f6:24:9e",
    "configured_area_name": None,
}


class _Coordinator:
    """A coordinator for a recorder channel, answering what device_info reads."""

    def __init__(self, **overrides):
        self.values = dict(RECORDER_CHANNEL, **overrides)

    def get_serial_number(self):
        return self.values["get_serial_number"]

    def get_device_serial_number(self):
        return self.values["get_device_serial_number"]

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


def _info(**overrides):
    from custom_components.dahua.entity import DahuaBaseEntity

    entity = object.__new__(DahuaBaseEntity)
    entity._coordinator = _Coordinator(**overrides)
    return entity.device_info


def test_the_page_describes_the_camera():
    info = _info()

    assert info["model"] == "H32_VSIPP-6DIRMD-I3"
    assert info["serial_number"] == "BD01D6APAG74E89"
    assert info["sw_version"] == "3.140.101B000.0.R"


def test_the_mac_is_offered_as_a_connection():
    """The only field Home Assistant renders a MAC in."""
    from homeassistant.helpers import device_registry as dr

    info = _info()

    assert info["connections"] == {(dr.CONNECTION_NETWORK_MAC, "30:dd:aa:f6:24:9e")}


def test_a_standalone_camera_still_describes_itself():
    """Nothing read the camera table, so every channel value is None and the
    page must look exactly as it did before this change."""
    info = _info(
        get_channel_model=None,
        get_channel_serial_number=None,
        get_channel_firmware_version=None,
        get_channel_mac=None,
    )

    assert info["model"] == "DHI-NVR5464-16P-EI"
    assert info["serial_number"] == "NVRSERIAL"
    assert info["sw_version"] == "4.005.0000000.3.R"
    assert "connections" not in info, "invented a connection from nothing"


def test_a_generic_device_type_falls_back_to_the_recorders_model():
    """`remote_device_model` returns None for "IP Camera" and the like, because
    a class of device is not a model. The page should then say what the device
    itself reports rather than nothing."""
    info = _info(get_channel_model=None)

    assert info["model"] == "DHI-NVR5464-16P-EI"
    # The rest of the camera's identity is still known and still shown.
    assert info["serial_number"] == "BD01D6APAG74E89"


# --- except the one page that is not a camera's -------------------------------
#
# `get_serial_number` appends the channel for every channel but the lowest, so
# the lowest one's identifier is the bare serial, which is the recorder's. There
# is no separate device for the recorder, so that page is where its own entities
# are filed: four disk sensors, the two storage warnings, the processor and the
# memory. Measured on a DHI-NVR5464-16P-EI, where slot 0 reports serial
# 4L07AD6PAG5F0F2 on firmware 2.622.0000000.31.R while the recorder is
# BC0A198PAJ779DF on 4.005.0000000.3.R.


def test_the_page_the_recorder_shares_stays_the_recorders():
    """The recorder's disks must not sit under a camera's model and serial.

    The firmware and serial *sensors* on that same page report the recorder, so
    describing the page as the camera would have it disagree with itself.
    """
    info = _info(get_serial_number="NVRSERIAL")

    assert info["model"] == "DHI-NVR5464-16P-EI"
    assert info["serial_number"] == "NVRSERIAL"
    assert info["sw_version"] == "4.005.0000000.3.R"


def test_the_recorders_page_does_not_claim_the_cameras_mac():
    """`connections` is how Home Assistant decides two devices are the same
    hardware, so the camera's MAC on the recorder's page invites a merge with
    whatever else has already claimed that camera."""
    info = _info(get_serial_number="NVRSERIAL")

    assert "connections" not in info


def test_every_other_channel_is_still_the_camera():
    """The narrowing is the shared page only. A channel with a device of its own
    is a camera and says so, which is what #1044 was for."""
    info = _info(get_serial_number="NVRSERIAL_11")

    assert info["model"] == "H32_VSIPP-6DIRMD-I3"
    assert info["serial_number"] == "BD01D6APAG74E89"
    assert info["sw_version"] == "3.140.101B000.0.R"


def test_the_identifiers_do_not_move():
    """The device's identity in the registry is the channel-suffixed key. If
    this changed, every existing device would be orphaned and every entity on it
    would be filed under a new one."""
    info = _info()

    assert info["identifiers"] == {("dahua", "NVRSERIAL_11")}
