"""SmartMotionDetect is indexed by channel, and the rows are sparse.

Taken from a real DHI-NVR5464-16P-EI, which reports rows for channels 1 and 11
only -- the channels the option is actually configured on. There is no row 0.
Reading row 0 for every channel therefore reported false for the whole NVR,
including for the one channel that had it switched on.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator

# getConfig&name=SmartMotionDetect, as the NVR returns it.
NVR_TABLE = {
    "table.SmartMotionDetect[1].Enable": "true",
    "table.SmartMotionDetect[1].ObjectTypes.Human": "true",
    "table.SmartMotionDetect[1].ObjectTypes.Vehicle": "true",
    "table.SmartMotionDetect[1].Sensitivity": "Middle",
    "table.SmartMotionDetect[11].Enable": "true",
    "table.SmartMotionDetect[11].ObjectTypes.Human": "true",
    "table.SmartMotionDetect[11].ObjectTypes.Vehicle": "true",
    "table.SmartMotionDetect[11].Sensitivity": "Middle",
}

SINGLE_CAMERA_TABLE = {
    "table.SmartMotionDetect[0].Enable": "true",
    "table.SmartMotionDetect[0].Sensitivity": "Middle",
}


def _coordinator(channel, data, amcrest=False):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c.data = data
    c.model = "AD410" if amcrest else "DHI-NVR5464-16P-EI"
    return c


@pytest.mark.parametrize("channel,expected", [
    (1, True),
    (11, True),
    (0, False),
    (2, False),
    (9, False),
])
def test_each_channel_reads_its_own_row(channel, expected):
    assert _coordinator(channel, NVR_TABLE).is_smart_motion_detection_enabled() is expected


def test_the_channel_that_has_it_on_is_not_reported_off():
    """The live symptom: one channel enabled, every switch showing off."""
    assert _coordinator(1, NVR_TABLE).is_smart_motion_detection_enabled() is True


def test_a_table_with_no_row_zero_does_not_make_every_channel_false():
    """Reading row 0 against this table is false for all eleven channels."""
    states = {_coordinator(c, NVR_TABLE).is_smart_motion_detection_enabled()
              for c in (0, 1, 2, 9, 11)}

    assert states == {True, False}, "every channel reported the same state"


# --- a single camera is untouched ------------------------------------------

def test_a_single_camera_still_reads_row_zero():
    assert _coordinator(0, SINGLE_CAMERA_TABLE).is_smart_motion_detection_enabled() is True


def test_a_channel_with_no_row_of_its_own_falls_back_to_row_zero():
    assert _coordinator(3, SINGLE_CAMERA_TABLE).is_smart_motion_detection_enabled() is True


def test_an_empty_table_is_off():
    assert _coordinator(0, {}).is_smart_motion_detection_enabled() is False
    assert _coordinator(7, {}).is_smart_motion_detection_enabled() is False


def test_a_missing_value_does_not_raise():
    """The row can exist with no Enable key at all."""
    assert _coordinator(1, {"table.SmartMotionDetect[1].Sensitivity": "Middle"}
                        ).is_smart_motion_detection_enabled() is False


# --- the write side ---------------------------------------------------------
#
# test_switch.py asserts the arguments the switch passes to the client. Nothing
# asserted the URL the client then builds, so the hardcoded index survived
# there unnoticed. This closes that.

class _Session:
    def __init__(self):
        self.urls = []

    async def request(self, method, url, headers=None, **kwargs):
        self.urls.append(url)
        return _Resp()


class _Resp:
    status = 200
    headers = {}

    def raise_for_status(self):
        return None

    async def text(self):
        return "OK"

    def close(self):
        return None


def _client(session):
    from custom_components.dahua.client import DahuaClient
    return DahuaClient("u", "p", "10.0.0.1", 80, 554, session)


@pytest.mark.parametrize("channel", [0, 1, 4, 11])
async def test_the_write_names_the_channel(channel):
    session = _Session()

    await _client(session).async_enabled_smart_motion_detection(channel, True)

    assert "SmartMotionDetect[%d].Enable=true" % channel in session.urls[-1]


async def test_channel_zero_writes_exactly_what_it_always_did():
    session = _Session()

    await _client(session).async_enabled_smart_motion_detection(0, False)

    assert session.urls[-1] == (
        "http://10.0.0.1:80/cgi-bin/configManager.cgi"
        "?action=setConfig&SmartMotionDetect[0].Enable=false"
    )


async def test_two_channels_do_not_write_to_the_same_row():
    session = _Session()
    c = _client(session)

    await c.async_enabled_smart_motion_detection(3, True)
    await c.async_enabled_smart_motion_detection(7, True)

    assert session.urls[-2] != session.urls[-1]
