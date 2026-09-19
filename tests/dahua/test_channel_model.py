"""Which camera is actually on an NVR channel.

Every channel of a recorder reports the *recorder's* model, because that is what
magicBox.cgi getSystemInfo answers. So a doorbell behind an NVR is invisible as a
doorbell, and every model-string capability check sees the wrong device.

The camera's own model is in RemoteDevice, indexed by channel. Measured on two
recorders:

    DHI-NVR5464-16P-EI   377 lines, DeviceType on 12 of 16 entries
      INFO_0.DeviceType=IP Camera              <- generic
      INFO_1.DeviceType=IPC                    <- generic
      INFO_11.DeviceType=H32_VSIPP-6DIRMD-I3   <- a real model

    Lorex N843A8 (#669)  DeviceType on every populated channel
      INFO_6.DeviceType=B451AJ                 <- the doorbell

Both index by the 0-based channel: INFO_1 is channel 1 on mine by camera name,
INFO_11 is channel 11, and #669's INFO_0..INFO_7 match their channels 0-7.

The generic values are the reason this returns None rather than a string. A
caller that could not tell "no answer" from "IP Camera" would confidently
misidentify every channel of a recorder like mine.
"""

from custom_components.dahua import GENERIC_DEVICE_TYPES, remote_device_model

# The measured Lorex, abbreviated.
LOREX = {
    "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.DeviceType": "E893DD",
    "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_4.DeviceType": "W462AQC",
    "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_6.DeviceType": "B451AJ",
    "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_7.DeviceType": "",
}

# The measured DHI-NVR5464-16P-EI, abbreviated.
MINE = {
    "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.DeviceType": "IP Camera",
    "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.DeviceType": "IPC",
    "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_11.DeviceType": "H32_VSIPP-6DIRMD-I3",
}


# --- the recorders that answer -----------------------------------------------

def test_the_doorbell_is_named():
    """The whole point: #690's B451AJ, which getSystemInfo cannot see."""
    assert remote_device_model(LOREX, 6) == "B451AJ"


def test_each_channel_gets_its_own_camera():
    assert remote_device_model(LOREX, 0) == "E893DD"
    assert remote_device_model(LOREX, 4) == "W462AQC"


def test_a_real_model_is_kept_even_where_its_neighbours_are_generic():
    assert remote_device_model(MINE, 11) == "H32_VSIPP-6DIRMD-I3"


def test_the_bracket_spelling_is_accepted_too():
    """Not measured anywhere yet; accepted in case firmware elsewhere uses it."""
    assert remote_device_model({"table.RemoteDevice[3].DeviceType": "IPC-HDW3849"}, 3) \
        == "IPC-HDW3849"


# --- what must not be mistaken for a model ------------------------------------

def test_a_class_of_device_is_not_a_model():
    """My recorder answers these for most channels."""
    assert remote_device_model(MINE, 0) is None
    assert remote_device_model(MINE, 1) is None


def test_an_empty_slot_is_not_a_model():
    assert remote_device_model(LOREX, 7) is None


def test_every_generic_value_is_rejected_however_it_is_cased():
    for generic in GENERIC_DEVICE_TYPES:
        for spelling in (generic, generic.upper(), generic.title(), " %s " % generic):
            data = {"table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.DeviceType": spelling}
            assert remote_device_model(data, 2) is None, spelling


def test_a_channel_that_is_not_in_the_table_has_no_model():
    assert remote_device_model(LOREX, 9) is None
    assert remote_device_model({}, 0) is None


def test_whitespace_around_a_real_model_is_trimmed():
    data = {"table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.DeviceType": "  B451AJ  "}

    assert remote_device_model(data, 2) == "B451AJ"


# --- it runs during setup, so it must never raise -----------------------------

def test_anything_that_is_not_a_table_is_no_model():
    assert remote_device_model(None, 0) is None
    assert remote_device_model("table.RemoteDevice[0].DeviceType=B451AJ", 0) is None
    assert remote_device_model([], 0) is None
