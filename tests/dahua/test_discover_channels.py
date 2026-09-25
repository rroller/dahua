"""Which channels of a recorder are worth offering when adding a camera.

#186 asks to add several channels at once. The form is the easy half. The hard
half is knowing which channels are real, because credential validation is host
level: it calls `magicBox.cgi` for the machine name and system info, neither of
which knows anything about channels. So "add channels 1 to 16" validates
happily against a recorder with four cameras and creates twelve dead entries.

Measured on a DHI-NVR5464-16P-EI with sixteen slots:

    idx 0   Private  Enable=true   snapshot 200, 1.3 MB      a camera
    idx 10  Onvif    Enable=true   snapshot 400              #710, unreachable
    idx 12  Private  Enable=true   snapshot 400              removed camera
    idx 14  Private  Enable=true   snapshot 200, 96 KB       a camera
    idx 15  Private  Enable=false  snapshot 400              empty slot

Three states have to be told apart, and `Enable` alone tells two of them:

- switched off  -> no camera, never offer
- Onvif         -> a camera this integration cannot drive, never offer
- enabled       -> maybe. A camera removed from the recorder leaves its slot
                   enabled with a stale serial, so only a probe decides

The probe lives in the config flow; these pin the two decisions made from the
table itself.
"""
from custom_components.dahua import dahua_utils


INFO = "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{0}.{1}"


def _recorder(slots):
    """slots: {index: (enabled, protocol)}"""
    data = {}
    for index, (enabled, protocol) in slots.items():
        data[INFO.format(index, "Enable")] = "true" if enabled else "false"
        data[INFO.format(index, "ProtocolType")] = protocol
    return data


def _the_real_recorder():
    """The DHI-NVR5464-16P-EI above: 16 slots, 15 enabled, one of those Onvif."""
    slots = {i: (True, "Private") for i in range(16)}
    slots[10] = (True, "Onvif")
    slots[15] = (False, "Private")
    return _recorder(slots)


# --- reading the table ------------------------------------------------------

def test_every_slot_is_parsed():
    devices = dahua_utils.parse_remote_devices(_the_real_recorder())

    assert len(devices) == 16


def test_enable_and_protocol_are_read():
    devices = dahua_utils.parse_remote_devices(_the_real_recorder())

    assert devices[0] == {"enabled": True, "protocol": "private"}
    assert devices[10] == {"enabled": True, "protocol": "onvif"}
    assert devices[15] == {"enabled": False, "protocol": "private"}


def test_a_device_with_no_such_table_parses_to_nothing():
    """A standalone camera. Nothing to offer is an ordinary answer."""
    assert dahua_utils.parse_remote_devices({}) == {}
    assert dahua_utils.parse_remote_devices(None) == {}
    assert dahua_utils.parse_remote_devices("Error") == {}


def test_a_slot_missing_a_field_still_parses():
    devices = dahua_utils.parse_remote_devices(
        {INFO.format(3, "Enable"): "true"})

    assert devices[3] == {"enabled": True, "protocol": ""}


# --- deciding what to offer -------------------------------------------------

def test_the_real_recorder_offers_fourteen_of_sixteen():
    devices = dahua_utils.parse_remote_devices(_the_real_recorder())

    assert dahua_utils.channels_worth_offering(devices) == [
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14]


def test_a_switched_off_slot_is_never_offered():
    devices = dahua_utils.parse_remote_devices(
        _recorder({0: (True, "Private"), 1: (False, "Private")}))

    assert dahua_utils.channels_worth_offering(devices) == [0]


def test_an_onvif_channel_is_never_offered():
    """It has a camera, and the recorder will not serve it on Dahua paths.

    snapshot answers 400 and RTSP times out, measured in #710. An entry for it
    could never work, so offering it is worse than leaving it out.
    """
    devices = dahua_utils.parse_remote_devices(
        _recorder({0: (True, "Private"), 1: (True, "Onvif")}))

    assert dahua_utils.channels_worth_offering(devices) == [0]


def test_the_protocol_check_is_case_insensitive():
    devices = dahua_utils.parse_remote_devices(
        _recorder({0: (True, "ONVIF"), 1: (True, "onvif")}))

    assert dahua_utils.channels_worth_offering(devices) == []


def test_nothing_to_offer_is_an_empty_list_not_an_error():
    assert dahua_utils.channels_worth_offering({}) == []


# --- labelling --------------------------------------------------------------

def test_channel_titles_are_read_by_index():
    titles = dahua_utils.parse_channel_titles({
        "table.ChannelTitle[0].Name": "FRONT STREET",
        "table.ChannelTitle[9].Name": "SIDE YARD",
    })

    assert titles == {0: "FRONT STREET", 9: "SIDE YARD"}


def test_a_title_says_nothing_about_whether_a_camera_is_there():
    """Defaults are not consistent even within one recorder.

    `Channel11`, `Channel 1`, `Channel16` and `IPC` all appeared on the same
    device, and `IPC` is a name somebody could choose deliberately. So titles
    are for labelling only, and nothing reads anything into them.
    """
    titles = dahua_utils.parse_channel_titles({
        "table.ChannelTitle[10].Name": "Channel11",
        "table.ChannelTitle[11].Name": "IPC",
        "table.ChannelTitle[14].Name": "Channel 1",
    })

    assert titles == {10: "Channel11", 11: "IPC", 14: "Channel 1"}


def test_unrelated_keys_are_ignored():
    assert dahua_utils.parse_channel_titles(
        {"table.VideoWidget[0].CustomTitle[0].Text": "x"}) == {}


def test_no_titles_is_empty_not_an_error():
    assert dahua_utils.parse_channel_titles({}) == {}
    assert dahua_utils.parse_channel_titles(None) == {}


def test_a_slot_that_does_not_say_whether_it_is_enabled_is_not_offered():
    """Silence is not consent.

    A slot reporting a protocol but no Enable tells us nothing about whether a
    camera is there. Defaulting that to enabled would put it in front of the
    user as something to add, which is the dead-entry failure this whole step
    exists to avoid. Defaulting it off costs at most one channel the user can
    still add by hand.
    """
    devices = dahua_utils.parse_remote_devices(
        {INFO.format(4, "ProtocolType"): "Private"})

    assert devices[4]["enabled"] is False
    assert dahua_utils.channels_worth_offering(devices) == []
