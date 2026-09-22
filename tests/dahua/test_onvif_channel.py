"""A channel the recorder reaches over ONVIF is not served on its Dahua paths.

#646: a camera attached to a Dahua NVR over ONVIF cannot be added through this
integration on any channel. The reporter tried channels 3 through 8; Home
Assistant's own ONVIF integration, pointed at the recorder, found it and
streamed it perfectly.

Reproduced on a DHI-NVR5464-16P-EI -- same recorder, same request, same minute:

    index 10  ch=11  Onvif    snapshot.cgi -> 400 Bad Request, 21 bytes
    index  1  ch=2   Private  snapshot.cgi -> 200, 1,420,074 bytes
    index 11  ch=12  Private  snapshot.cgi -> 200,   175,172 bytes

Fourteen channels report Private and answer; the one reporting Onvif does not.
Its RTSP path times out too. So there is nothing to fix in the channel number --
the recorder simply does not proxy that camera on the paths this integration
speaks, and the honest thing is to say so rather than leave a camera entity
returning 400 for the life of the entry.
"""

import pytest

from custom_components.dahua import is_onvif_channel, remote_device_protocol

UUID = "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{0}.ProtocolType"
BRACKET = "table.RemoteDevice[{0}].ProtocolType"

# The measured recorder, abbreviated to the interesting channels.
MEASURED = {
    UUID.format(0): "Private",
    UUID.format(10): "Onvif",
    UUID.format(11): "Private",
    UUID.format(14): "Private",
}


# --- reading the protocol ----------------------------------------------------

def test_the_measured_onvif_channel():
    assert remote_device_protocol(MEASURED, 10) == "onvif"


def test_the_measured_private_channels():
    for channel in (0, 11, 14):
        assert remote_device_protocol(MEASURED, channel) == "private"


def test_the_bracket_spelling_is_accepted_too():
    """Both key shapes, exactly as remote_device_model accepts both."""
    assert remote_device_protocol({BRACKET.format(3): "Onvif"}, 3) == "onvif"


def test_a_channel_that_said_nothing():
    assert remote_device_protocol(MEASURED, 7) is None
    assert remote_device_protocol({UUID.format(7): "   "}, 7) is None


@pytest.mark.parametrize("data", [None, "", [], 0])
def test_a_reply_that_is_not_a_table(data):
    assert remote_device_protocol(data, 0) is None


# --- and the question the setup actually asks --------------------------------

def test_only_the_onvif_channel_is_flagged():
    assert is_onvif_channel(MEASURED, 10) is True
    for channel in (0, 11, 14):
        assert is_onvif_channel(MEASURED, channel) is False


def test_a_silent_channel_is_not_flagged():
    """Absence of an answer is not evidence of ONVIF."""
    assert is_onvif_channel(MEASURED, 7) is False
    assert is_onvif_channel({}, 0) is False


def test_the_case_the_recorder_actually_uses():
    """The recorder writes "Onvif", not "onvif" or "ONVIF"."""
    assert is_onvif_channel({UUID.format(2): "Onvif"}, 2) is True
    assert is_onvif_channel({UUID.format(2): "ONVIF"}, 2) is True
