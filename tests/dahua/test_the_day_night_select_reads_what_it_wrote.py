"""The Day/Night select reads the table the write lands in.

#1194, reported by @jpeu77 against three SD1A404DB-GNY PTZ cameras on firmware
3.201.0000007.1.R. Setting the select to Color or BlackWhite changed the
picture, and the select went on reporting `Auto` for ever, through reloads and
camera reboots.

The cause is that the two halves named different tables:

    async_set_video_in_day_night_mode  ->  VideoInDayNight[ch][2].Mode
    get_day_night_color                ->  VideoInOptions[ch].DayNightColor

and the write returns as soon as the first table answers OK, so on a camera
carrying both, the table that was read never moved.

The measurement that settles which row matters, captured twice on the
reporter's camera, first in Color and then right after switching to BlackWhite:

    VideoInDayNight[0][2].Mode   (Name: Normal)   Color  ->  BlackWhite
    VideoInDayNight[0][0,1,3..8].Mode             Brightness, both times
    VideoInOptions[0].DayNightColor               1, both times
    VideoInMode[0]                                unchanged

So row 2 is the row the picture follows, `VideoInOptions` is not connected to
the picture on this firmware, and reading any other row would report `Auto` on
a camera showing black and white.
"""

from types import SimpleNamespace

import aiohttp
import pytest

from custom_components.dahua.client import (
    VIDEO_IN_DAY_NIGHT,
    VIDEO_IN_OPTIONS,
    DahuaClient,
)
from custom_components.dahua.coordinator import (
    DahuaDataUpdateCoordinator,
    day_night_mode_name,
)

# The reporter's own rows, as `dahua.get_config` printed them. Rows other than
# 2 are included because they are what makes a wrong row number look right:
# every one of them says `Brightness`, which maps to `Auto`, which is the
# stale value being fixed.
BLACKWHITE = {
    "table.VideoInDayNight[0][0].Mode": "Brightness",
    "table.VideoInDayNight[0][1].Mode": "Brightness",
    "table.VideoInDayNight[0][2].Mode": "BlackWhite",
    "table.VideoInDayNight[0][3].Mode": "Brightness",
    "table.VideoInDayNight[0][8].Mode": "Brightness",
    "table.VideoInOptions[0].DayNightColor": "1",
}
COLOUR = dict(BLACKWHITE, **{"table.VideoInDayNight[0][2].Mode": "Color"})


def _coordinator(data, reads_day_night, channel=0):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = data
    coordinator._channel = channel
    coordinator._reads_video_in_day_night = reads_day_night
    return coordinator


# --- the parser -------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("BlackWhite", "BlackWhite"),
        ("Color", "Color"),
        ("Brightness", "Auto"),
        ("blackwhite", "BlackWhite"),
        ("  Color  ", "Color"),
    ],
)
def test_the_general_row_is_read_by_name(value, expected):
    data = {"table.VideoInDayNight[0][2].Mode": value}

    assert day_night_mode_name(data, 0) == expected


def test_a_word_this_mapping_does_not_cover_is_unknown():
    """None rather than Auto. A camera naming a mode we do not model is saying
    something, and `Auto` would be inventing an answer for it."""
    data = {"table.VideoInDayNight[0][2].Mode": "SomethingElse"}

    assert day_night_mode_name(data, 0) is None


def test_a_table_without_this_channel_is_unknown():
    """A recorder answers one request for every channel, so a row for channel 0
    says nothing about channel 5."""
    assert day_night_mode_name(BLACKWHITE, 5) is None


def test_the_other_rows_are_not_read():
    """The neighbouring-but-wrong case, and the realistic defect. Every other
    row on the reporter's camera holds `Brightness`, so a wrong row number
    reports `Auto` on a camera that is plainly in black and white."""
    assert day_night_mode_name(BLACKWHITE, 0) == "BlackWhite"

    only_other_rows = {
        key: value
        for key, value in BLACKWHITE.items()
        if not key.startswith("table.VideoInDayNight[0][2]")
    }
    assert day_night_mode_name(only_other_rows, 0) is None


# --- which table the select reads -------------------------------------------


def test_the_select_follows_the_table_that_moved():
    """The reporter's case end to end: the picture went to black and white and
    the select must go with it."""
    coordinator = _coordinator(BLACKWHITE, reads_day_night=True)

    assert coordinator.get_day_night_color() == "BlackWhite"


def test_the_stale_other_table_is_not_consulted():
    """`VideoInOptions` holds `1` in both of the reporter's captures. Falling
    back to it for any reason reintroduces #1194 exactly."""
    unmappable = dict(BLACKWHITE)
    unmappable["table.VideoInDayNight[0][2].Mode"] = "SomethingElse"
    coordinator = _coordinator(unmappable, reads_day_night=True)

    assert coordinator.get_day_night_color() is None, "it read the stale table"


def test_a_device_without_the_table_is_unchanged():
    """Measured refusals for VideoInDayNight: a DHI-NVR5464-16P-EI answers 400,
    a VTO answers "Unknown error", the DHI-VTO2311R-WP on #687 answers 400. All
    of them carry VideoInOptions, and none of their behaviour may move."""
    coordinator = _coordinator(
        {"table.VideoInOptions[0].DayNightColor": "2"}, reads_day_night=False
    )

    assert coordinator.get_day_night_color() == "BlackWhite"


def test_the_other_table_moving_does_not_move_the_select():
    """On this firmware `VideoInOptions` is not connected to the picture, so a
    select that followed it would report a mode the camera is not in."""
    options_say_color = dict(BLACKWHITE)
    options_say_color["table.VideoInOptions[0].DayNightColor"] = "0"
    coordinator = _coordinator(options_say_color, reads_day_night=True)

    assert coordinator.get_day_night_color() == "BlackWhite"


def test_the_two_captures_differ_the_way_the_report_says():
    """Both of the reporter's readings, through the real accessor."""
    assert _coordinator(COLOUR, True).get_day_night_color() == "Color"
    assert _coordinator(BLACKWHITE, True).get_day_night_color() == "BlackWhite"


# --- following the write ----------------------------------------------------


def test_a_fallback_write_moves_the_select_back():
    """A device can serve VideoInDayNight and still refuse the write to it. The
    write then lands in VideoInOptions, and a select still reading the first
    table would be #1194 with the two swapped."""
    coordinator = _coordinator(BLACKWHITE, reads_day_night=True)

    coordinator.note_day_night_write_table(VIDEO_IN_OPTIONS)

    assert coordinator.reads_video_in_day_night() is False
    assert coordinator.get_day_night_color() == "Auto"


def test_a_write_that_landed_in_the_first_table_sets_it():
    coordinator = _coordinator(BLACKWHITE, reads_day_night=False)

    coordinator.note_day_night_write_table(VIDEO_IN_DAY_NIGHT)

    assert coordinator.reads_video_in_day_night() is True
    assert coordinator.get_day_night_color() == "BlackWhite"


def test_an_answer_that_names_no_table_changes_nothing():
    """`None` is what an older caller passes, and it must not silently move a
    channel onto the wrong table."""
    coordinator = _coordinator(BLACKWHITE, reads_day_night=True)

    coordinator.note_day_night_write_table(None)

    assert coordinator.reads_video_in_day_night() is True


# --- what the client sends and says -----------------------------------------


class _Sent:
    def __init__(self, answers):
        self.urls = []
        self.answers = list(answers)

    async def get(self, url, verify_response=False):
        self.urls.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _client(answers):
    client = object.__new__(DahuaClient)
    client._device = "day-night-camera"
    sent = _Sent(answers)
    client.get = sent.get
    client._sent = sent
    return client


async def test_the_write_names_the_table_it_used():
    client = _client(["OK"])

    table = await client.async_set_video_in_day_night_mode(0, "general", "BlackWhite")

    assert table == VIDEO_IN_DAY_NIGHT
    assert client._sent.urls == [
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&VideoInDayNight[0][2].Mode=BlackWhite"
    ]


async def test_an_unexpected_failure_is_not_turned_into_a_fallback():
    """Only the two refusal shapes send it to VideoInOptions. Anything else
    comes out, because a bug must not look like a device without the table."""
    client = _client([ValueError("something broke")])

    with pytest.raises(ValueError):
        await client.async_set_video_in_day_night_mode(0, "general", "Color")

    assert len(client._sent.urls) == 1, "it tried the fallback anyway"


async def test_the_fallback_reports_video_in_options():
    import aiohttp

    refusal = aiohttp.ClientResponseError(request_info=None, history=(), status=400)
    client = _client([refusal, "OK"])

    table = await client.async_set_video_in_day_night_mode(0, "general", "Color")

    assert table == VIDEO_IN_OPTIONS
    assert client._sent.urls[1] == (
        "/cgi-bin/configManager.cgi?action=setConfig&VideoInOptions[0].DayNightColor=0"
    )


# --- the probe ---------------------------------------------------------------


async def test_the_probe_reads_the_whole_table_and_sets_the_flag():
    """The reporter's own `dahua.get_config` proves the whole-table read is the
    shape that answers, so that is the shape asked for."""
    client = _client([COLOUR])
    coordinator = _coordinator({}, reads_day_night=False)
    coordinator.client = client

    assert await coordinator._async_probe_video_in_day_night() is True
    assert coordinator.reads_video_in_day_night() is True
    assert client._sent.urls == [
        "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInDayNight"
    ]


async def test_a_device_that_does_not_serve_the_table_stays_on_the_old_one():
    """`async_get_config` turns a 400 into {}, which is the measured answer from
    an NVR5464 and two VTOs. They must come out of this unchanged."""
    client = _client([{}])
    coordinator = _coordinator({}, reads_day_night=False)
    coordinator.client = client

    assert await coordinator._async_probe_video_in_day_night() is False
    assert coordinator.reads_video_in_day_night() is False


async def test_a_table_whose_row_two_is_unreadable_does_not_claim_it():
    """Present but unmappable is not a table we can read back, so the channel
    stays on VideoInOptions rather than reporting unknown for ever."""
    client = _client([{"table.VideoInDayNight[0][2].Mode": "Mystery"}])
    coordinator = _coordinator({}, reads_day_night=False)
    coordinator.client = client

    assert await coordinator._async_probe_video_in_day_night() is False


# --- a refused poll keeps the row, through the real client ------------------
#
# @rroller's P2 on #1201. `async_get_config` catches ClientResponseError and
# returns {}, so an HTTP 400, 403 or 500 never reached the poll wrapper's
# handler. It returned an empty table, the next data replacement dropped the
# row, and the select went to unknown on one bad read.
#
# Driven through the real DahuaClient rather than a stub that raises, because a
# stub skips the very conversion that caused this.


class _Refusing:
    """A session that answers one status to everything."""

    def __init__(self, status):
        self.status = status
        self.calls = 0

    async def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        return _Response(self.status)


class _Response:
    def __init__(self, status):
        self.status = status
        self.headers = {}

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="http://10.0.0.9/x"),
                history=(),
                status=self.status,
            )

    async def text(self):
        return ""

    async def read(self):
        return b""

    def close(self):
        return None


@pytest.mark.parametrize("status", [400, 403, 500])
async def test_a_refused_poll_keeps_the_row_it_had(status):
    """The held BlackWhite row must survive a refusal, whatever its status."""
    session = _Refusing(status)
    coordinator = _coordinator(dict(BLACKWHITE), reads_day_night=True)
    coordinator.client = DahuaClient("u", "p", "10.0.0.9", 80, 554, session)

    carried = await coordinator._async_fetch_video_in_day_night()

    assert session.calls, "the real client was never reached"
    assert carried is not None, "a refused read wiped the table"
    assert carried["table.VideoInDayNight[0][2].Mode"] == "BlackWhite"


async def test_the_select_still_reads_blackwhite_after_a_refused_poll():
    """The consequence, stated as the user sees it. Without the carry the next
    data replacement leaves the select unknown."""
    session = _Refusing(500)
    coordinator = _coordinator(dict(BLACKWHITE), reads_day_night=True)
    coordinator.client = DahuaClient("u", "p", "10.0.0.10", 80, 554, session)

    carried = await coordinator._async_fetch_video_in_day_night()
    coordinator.data = carried  # what the coordinator does with the answer

    assert coordinator.get_day_night_color() == "BlackWhite"


async def test_a_genuinely_empty_table_is_not_mistaken_for_a_refusal():
    """The other half: a device that answers an empty table has said something,
    and that must stay distinguishable from one that refused."""
    session = _Refusing(200)
    coordinator = _coordinator(dict(BLACKWHITE), reads_day_night=True)
    coordinator.client = DahuaClient("u", "p", "10.0.0.11", 80, 554, session)

    answered = await coordinator._async_fetch_video_in_day_night()

    assert answered == {}, "an empty 200 was treated as a refusal"
