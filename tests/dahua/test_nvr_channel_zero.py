"""Channel 0 of a recorder whose model does not say "NVR".

is_nvr_channel decides two things that must agree: whether the deterrence entity
exists at all, and whether its commands go through coaxialControlIO on the
channel *number* (the recorder's way) or the channel *index* (the camera's way).

Channel 0 is the awkward case, because it is both the only channel a standalone
camera has and the first channel of every recorder. The model string was the
only thing separating them, and plenty of recorders do not say "NVR" in theirs --
the Lorex N843A8 on #669 does not, and neither do most OEM rebrands.

So a user who switched on NVR active deterrence got the entity on channels 1
upwards and nothing on channel 0, which took the standalone-camera branch and was
tested against a model whitelist a recorder can never match.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator(channel=0, model="", deterrence=False, device_class=None):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c.model = model
    c._nvr_active_deterrence = deterrence
    # Only set when a case is about the reported class. Left absent otherwise,
    # because that is what a device with no RPC2 identity leaves behind and what
    # every other double in this suite looks like.
    if device_class is not None:
        c._device_class = device_class
    return c


# --- the case this exists for -------------------------------------------------


def test_channel_zero_of_an_oem_recorder_is_a_channel_when_the_option_is_on():
    """A Lorex N843A8 says nothing about being an NVR."""
    assert _coordinator(channel=0, model="N843A8", deterrence=True).is_nvr_channel()


def test_the_other_channels_of_that_recorder_were_always_right():
    """Which is what made it lopsided rather than simply broken."""
    for channel in (1, 4, 6):
        assert _coordinator(
            channel=channel, model="N843A8", deterrence=True
        ).is_nvr_channel()
        assert _coordinator(
            channel=channel, model="N843A8", deterrence=False
        ).is_nvr_channel()


# --- nothing else changes -----------------------------------------------------


def test_a_standalone_camera_is_still_a_camera():
    assert not _coordinator(channel=0, model="IPC-HDW3849HP-AS-PV").is_nvr_channel()


def test_channel_zero_without_the_option_is_unchanged():
    """The option defaults off, so this is what almost every entry does."""
    assert not _coordinator(
        channel=0, model="N843A8", deterrence=False
    ).is_nvr_channel()


@pytest.mark.parametrize("model", ["DHI-NVR5464-16P-EI", "NVR2108-I", "nvr4116hs"])
def test_a_recorder_that_says_so_needs_no_option(model):
    assert _coordinator(channel=0, model=model, deterrence=False).is_nvr_channel()


def test_a_camera_whose_owner_turned_the_option_on_is_taken_at_their_word():
    """The option is offered for recorders and defaults off.

    Turning it on is a clearer statement about the device than a model string,
    and the alternative -- creating the entity but sending its commands down the
    camera path -- would be worse than either answer.
    """
    assert _coordinator(
        channel=0, model="IPC-HDW3849HP-AS-PV", deterrence=True
    ).is_nvr_channel()


def test_the_answer_is_a_boolean():
    """It is returned straight from a property and compared with `is`."""
    assert (
        _coordinator(channel=0, model="N843A8", deterrence=True).is_nvr_channel()
        is True
    )
    assert (
        _coordinator(channel=0, model="N843A8", deterrence=False).is_nvr_channel()
        is False
    )


# --- and the device's own answer, which beats both of the above ----------------
#
# The option is a good answer but it is a manual one, and the paragraph above is
# about a recorder nobody has told us is a recorder. `getDeviceClass` returns NVR
# on an N843A8 whatever its model name says, so asking removes the guess instead
# of adding another.
#
# `uses_recorder_deterrence` already preferred the class for the deterrence
# entity. The other callers did not, so on channel 0 of such a recorder the IVS
# rules were read from `VideoAnalyseRule` rather than `RemoteVideoAnalyseRule`
# (coordinator.py, the poll and the setup probe) and the floodlight drove the
# camera coaxial path rather than the recorder one (light.py). Those are the
# behaviours these tests are really about; is_nvr_channel is where they are decided.


def test_a_recorder_is_believed_when_it_says_so_itself():
    """The N843A8 case again, this time without asking the user anything."""
    coordinator = _coordinator(
        channel=0, model="N843A8", device_class="NVR", deterrence=False
    )

    assert coordinator.is_nvr_channel() is True


@pytest.mark.parametrize("device_class", ["NVR", "DVR", "XVR", "HCVR", "nvr", " Nvr "])
def test_every_recorder_class_counts_and_the_spelling_does_not_matter(device_class):
    """is_recorder_host already strips and upper-cases, and this is what says the
    two agree rather than each keeping its own idea of a recorder."""
    assert (
        _coordinator(
            channel=0, model="N843A8", device_class=device_class
        ).is_nvr_channel()
        is True
    )


def test_a_camera_that_reports_its_class_is_still_a_camera():
    """The point of asking is that the answer cuts both ways. A device that says
    IPC must not be pulled onto the recorder path by this change."""
    assert (
        _coordinator(
            channel=0, model="IPC-HDW3849HP-AS-PV", device_class="IPC"
        ).is_nvr_channel()
        is False
    )


@pytest.mark.parametrize("device_class", ["VTO", "VTH"])
def test_a_doorbell_or_an_indoor_monitor_is_not_a_recorder_channel(device_class):
    """Both are single-channel devices on channel 0 with their own entity sets,
    and both report a class, so both would be caught by a looser check."""
    assert (
        _coordinator(
            channel=0, model="VTO2000A", device_class=device_class
        ).is_nvr_channel()
        is False
    )


def test_a_device_that_reports_no_class_is_not_assumed_to_be_one():
    """Left absent rather than empty, which is what a coordinator looks like
    before the identity call answers, and what the other doubles here look like.
    An invented recorder would send a standalone camera's IVS read to
    RemoteVideoAnalyseRule, which it does not serve."""
    coordinator = _coordinator(channel=0, model="N843A8", deterrence=False)

    assert coordinator.is_nvr_channel() is False


def test_an_empty_class_string_is_no_answer_either():
    assert (
        _coordinator(channel=0, model="N843A8", device_class="   ").is_nvr_channel()
        is False
    )


def test_the_model_match_is_still_there_for_a_device_that_reports_nothing():
    """Every pre-RPC2 identity path leaves no class behind, so removing the model
    fallback would regress every recorder that does say NVR."""
    assert (
        _coordinator(
            channel=0, model="DHI-NVR5464-16P-EI", device_class=None
        ).is_nvr_channel()
        is True
    )
