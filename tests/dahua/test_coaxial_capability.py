"""A camera's siren and deterrence light, decided by the device rather than its name.

#676. Whether a camera got a Siren or a Security Light entity was decided by
matching its model string. The siren check asked for `-AS-PV`, with a leading
hyphen, and `IPC-HDBW3549R1-ZAS-PV` contains `AS-PV` without one. So a hyphen
decided whether that camera's siren existed in Home Assistant, and the camera
plainly has one. Every OEM rebrand is the same problem with a different name:
SL-IPC-T7828C-V3, GX-HT728P-AI-EPTZ-CF-28AD and N43BU82 are all in that issue.

The device already answers the question, and the answer is already on the wire.
`coaxialControlIO.cgi?action=getStatus` is fetched during setup to find out
whether coaxial control works at all, and its reply names the outputs:

    status.status.Speaker=Off
    status.status.WhiteLight=Off

Presence of the field is the signal. `Off` means the output is there and
currently off, which is the case a value check would throw away.

**Recorders are excluded, and that is the load-bearing part.** Measured on a
DHI-NVR5464-16P-EI, the recorder answers `Speaker=Off` and `WhiteLight=Off` for
every channel that exists, and 400 for a slot with nothing in it. None of those
cameras has a siren or a deterrence light, and coaxial control does nothing at
all on that recorder. Without the guard this would invent a siren and a light on
all ten channels. NVR deterrence stays the explicit opt-in it already is.

The model lists stay as a fallback. A device that says nothing keeps exactly the
behaviour it has always had, so nothing that works today can stop working.
"""
from custom_components.dahua import DahuaDataUpdateCoordinator, dahua_utils


def _coordinator(outputs=None, model="SL-IPC-T7828C-V3", nvr=False):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.model = model
    c._channel = 0
    c._coaxial_outputs = outputs or {"speaker": False, "light": False}
    c._supports_rpc2_siren = False
    c._supports_rpc2_security_light = False
    c._nvr_active_deterrence = False
    c.is_nvr_channel = lambda: nvr
    return c


def _reply(*fields, nested=True):
    """A coaxialControlIO getStatus reply naming the given outputs."""
    prefix = "status.status." if nested else "status."
    return {prefix + name: "Off" for name in fields}


# --- reading what the device said -------------------------------------------

def test_a_device_that_names_both_outputs():
    assert dahua_utils.coaxial_outputs_reported(
        _reply("Speaker", "WhiteLight")) == {"speaker": True, "light": True}


def test_the_flatter_reply_shape_is_read_the_same():
    """Measured on a recorder: one level of nesting, not two."""
    assert dahua_utils.coaxial_outputs_reported(
        _reply("Speaker", "WhiteLight", nested=False)) == {
            "speaker": True, "light": True}


def test_a_device_with_only_a_speaker():
    assert dahua_utils.coaxial_outputs_reported(_reply("Speaker")) == {
        "speaker": True, "light": False}


def test_off_means_present_and_off_not_absent():
    """The whole point. A value check would read Off as "no siren"."""
    assert dahua_utils.coaxial_outputs_reported(
        {"status.status.Speaker": "Off"})["speaker"] is True


def test_a_device_that_refuses_names_nothing():
    for refusal in ({}, None, "Error", []):
        assert dahua_utils.coaxial_outputs_reported(refusal) == {
            "speaker": False, "light": False}


def test_only_the_white_light_field_counts():
    """A device naming some other light must not get a deterrence entity.

    The red and blue deterrence output is reported as `WhiteLight`, which is
    confusing but is the only field that means it. Matching anything with
    "light" in the name would hand an illuminator or an infrared row the
    deterrence light entity, which is the #540 mistake in a new place.
    """
    assert dahua_utils.coaxial_outputs_reported({
        "status.status.Speaker": "Off",
        "status.status.IlluminatorLight": "On",
    }) == {"speaker": True, "light": False}


def test_unrelated_keys_are_not_read_as_outputs():
    assert dahua_utils.coaxial_outputs_reported(
        {"status.status.Mode": "Off", "table.Lighting[0][0].Mode": "Auto"}) == {
            "speaker": False, "light": False}


# --- what the camera then gets ----------------------------------------------

def test_a_camera_that_reports_a_speaker_gets_a_siren():
    """The #676 case. This model is in no list and has the hardware."""
    c = _coordinator({"speaker": True, "light": False})

    assert c.supports_siren() is True


def test_a_camera_that_reports_a_light_gets_a_security_light():
    c = _coordinator({"speaker": False, "light": True})

    assert c.supports_security_light() is True


def test_the_hyphen_no_longer_decides():
    """`IPC-HDBW3549R1-ZAS-PV` contains `AS-PV` but not `-AS-PV`."""
    named = _coordinator(model="DH-IPC-HDBW3549R1-ZAS-PV")
    assert named.supports_siren() is False, "the model list still misses it"

    answered = _coordinator({"speaker": True, "light": True},
                            model="DH-IPC-HDBW3549R1-ZAS-PV")
    assert answered.supports_siren() is True
    assert answered.supports_security_light() is True


def test_a_camera_that_reports_nothing_keeps_its_model_list_answer():
    """Nothing that works today may stop working."""
    listed = _coordinator(model="IPC-HDW3849HP-AS-PV")

    assert listed.supports_siren() is True
    assert listed.supports_security_light() is True


def test_a_camera_with_neither_gets_neither():
    c = _coordinator(model="IPC-HFW1431S-S4")

    assert c.supports_siren() is False
    assert c.supports_security_light() is False


# --- the recorder guard -----------------------------------------------------

def test_a_recorder_channel_is_not_given_hardware_it_lacks():
    """Measured: this recorder answers for every channel that exists.

    Ten channels, none with a siren, all answering Speaker=Off. Reading that
    as a capability would put a siren and a light on all ten.
    """
    channel = _coordinator({"speaker": True, "light": True}, model="DHI-NVR5464-16P-EI",
                           nvr=True)

    assert channel._reported_coaxial_output("speaker") is False
    assert channel.supports_siren() is False
    assert channel.supports_security_light() is False


def test_the_recorder_opt_in_is_untouched():
    """NVR deterrence stays a deliberate choice, not something probed into."""
    channel = _coordinator(model="DHI-NVR5464-16P-EI", nvr=True)
    assert channel.supports_nvr_active_deterrence() is False

    channel._nvr_active_deterrence = True
    assert channel.supports_nvr_active_deterrence() is True
