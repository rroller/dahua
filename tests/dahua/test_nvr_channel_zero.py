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


def _coordinator(channel=0, model="", deterrence=False):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c.model = model
    c._nvr_active_deterrence = deterrence
    return c


# --- the case this exists for -------------------------------------------------

def test_channel_zero_of_an_oem_recorder_is_a_channel_when_the_option_is_on():
    """A Lorex N843A8 says nothing about being an NVR."""
    assert _coordinator(channel=0, model="N843A8", deterrence=True).is_nvr_channel()


def test_the_other_channels_of_that_recorder_were_always_right():
    """Which is what made it lopsided rather than simply broken."""
    for channel in (1, 4, 6):
        assert _coordinator(channel=channel, model="N843A8", deterrence=True).is_nvr_channel()
        assert _coordinator(channel=channel, model="N843A8", deterrence=False).is_nvr_channel()


# --- nothing else changes -----------------------------------------------------

def test_a_standalone_camera_is_still_a_camera():
    assert not _coordinator(channel=0, model="IPC-HDW3849HP-AS-PV").is_nvr_channel()


def test_channel_zero_without_the_option_is_unchanged():
    """The option defaults off, so this is what almost every entry does."""
    assert not _coordinator(channel=0, model="N843A8", deterrence=False).is_nvr_channel()


@pytest.mark.parametrize("model", ["DHI-NVR5464-16P-EI", "NVR2108-I", "nvr4116hs"])
def test_a_recorder_that_says_so_needs_no_option(model):
    assert _coordinator(channel=0, model=model, deterrence=False).is_nvr_channel()


def test_a_camera_whose_owner_turned_the_option_on_is_taken_at_their_word():
    """The option is offered for recorders and defaults off.

    Turning it on is a clearer statement about the device than a model string,
    and the alternative -- creating the entity but sending its commands down the
    camera path -- would be worse than either answer.
    """
    assert _coordinator(channel=0, model="IPC-HDW3849HP-AS-PV", deterrence=True).is_nvr_channel()


def test_the_answer_is_a_boolean():
    """It is returned straight from a property and compared with `is`."""
    assert _coordinator(channel=0, model="N843A8", deterrence=True).is_nvr_channel() is True
    assert _coordinator(channel=0, model="N843A8", deterrence=False).is_nvr_channel() is False
