"""#1184: `set_privacy_masking` enabled a mask that covered nothing.

@jackouille2 reported that `index: 2` did nothing on an older camera, while
driving `VideoWidget[0].Covers[2].EncodeBlend=true` by hand worked. Their first
reading was that the integration mapped the index onto the channel brackets. It
does not: there is one write behind the service and it has always been
`PrivacyMasking[0][index].Enable`.

**What the two config dumps on the issue actually show.** Read through the
integration's own `get_config`, on the camera, on 3.0.0-rc.5:

    PrivacyMasking            24 rows, [0][0] .. [0][23]
                              every Rect[0..3] and every Polygon[n][m] is "0"
                              [0][1] and [0][2] Enable true, the rest false

    VideoWidget[0].Covers      4 rows
                              Covers[0] Rect 4652,0,8175,2212   EncodeBlend true
                              Covers[1] Rect 5275,2194,8175,3778 EncodeBlend false
                              Covers[2] Rect 5979,3804,8183,5673 EncodeBlend false
                              Covers[3] Rect 6799,5677,8175,8180 EncodeBlend false

So the masks the owner drew are the four `Covers` rows with real coordinates,
and the `Enable=true` values sitting in `PrivacyMasking[0][1]` and `[0][2]` are
the integration's own earlier writes. The old write was neither refused nor
ignored: it enabled a mask whose region is a zero-area box, and `200 OK` was an
honest answer. That is why nothing in the integration could tell, and why
`write_was_declined` and a read-back of `Enable` would both have said it worked.

The fix therefore judges the row rather than the firmware: a mask row with no
coordinates cannot blank anything, so the write goes to the `Covers` row of the
same index instead, and where both tables describe a region `PrivacyMasking`
keeps the write it has always had.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua.client import (
    DahuaClient,
    privacy_mask_has_a_region,
    privacy_mask_row_count,
)

# ---------------------------------------------------------------------------
# What each camera serves, in the shape a flat config read gives
# ---------------------------------------------------------------------------


def _empty_masking_rows(count=24):
    """`PrivacyMasking` as #1184's camera serves it: rows, and no geometry.

    Built rather than pasted so the row count is one number, and so a reader can
    see that the only non-zero thing in the table is the Enable this integration
    put there.
    """
    rows = {}
    for row in range(count):
        prefix = "table.PrivacyMasking[0][%d]." % row
        rows[prefix + "Enable"] = "true" if row in (1, 2) else "false"
        for corner in range(4):
            rows["%sRect[%d]" % (prefix, corner)] = "0"
        for point in range(4):
            rows["%sPolygon[%d][0]" % (prefix, point)] = "0"
            rows["%sPolygon[%d][1]" % (prefix, point)] = "0"
    return rows


def _covers(channel=0):
    """`VideoWidget[channel].Covers`, with the four measured rectangles."""
    measured = [
        ("4652", "0", "8175", "2212", "true"),
        ("5275", "2194", "8175", "3778", "false"),
        ("5979", "3804", "8183", "5673", "false"),
        ("6799", "5677", "8175", "8180", "false"),
    ]
    rows = {}
    for index, (left, top, right, bottom, blend) in enumerate(measured):
        prefix = "table.VideoWidget[%d].Covers[%d]." % (channel, index)
        rows[prefix + "EncodeBlend"] = blend
        rows[prefix + "PreviewBlend"] = "false"
        for corner, value in enumerate((left, top, right, bottom)):
            rows["%sRect[%d]" % (prefix, corner)] = value
    return rows


# A camera that keeps its masks where the integration always wrote them.
MASKING_IS_LIVE = {
    "PrivacyMasking": {
        "table.PrivacyMasking[0][0].Enable": "false",
        "table.PrivacyMasking[0][0].Rect[0]": "1024",
        "table.PrivacyMasking[0][0].Rect[1]": "2048",
        "table.PrivacyMasking[0][0].Rect[2]": "4096",
        "table.PrivacyMasking[0][0].Rect[3]": "6144",
        "table.PrivacyMasking[0][2].Enable": "false",
        "table.PrivacyMasking[0][2].Rect[0]": "100",
        "table.PrivacyMasking[0][2].Rect[1]": "200",
        "table.PrivacyMasking[0][2].Rect[2]": "300",
        "table.PrivacyMasking[0][2].Rect[3]": "400",
    },
    "VideoWidget": {},
}

# #1184's camera: rows in one table, masks in the other.
THE_1184_CAMERA = {
    "PrivacyMasking": _empty_masking_rows(),
    "VideoWidget": _covers(),
}

# A camera with neither, which is what a refusal looks like through
# `async_get_config`.
SAYS_NOTHING = {"PrivacyMasking": {}, "VideoWidget": {}}


def _client(served, address="192.168.1.223"):
    """A real DahuaClient with its reads and its one write intercepted."""
    client = DahuaClient("u", "p", address, 80, 554, SimpleNamespace(closed=False))
    asked = []
    written = []

    async def request(url, verify_ok=False, **kwargs):
        for name in ("PrivacyMasking", "VideoWidget"):
            if url == f"/cgi-bin/configManager.cgi?action=getConfig&name={name}":
                asked.append(name)
                return dict(served.get(name, {}))
        written.append((url, verify_ok))
        return {"OK": "OK"}

    client._request = request
    client.asked = asked
    client.written = written
    return client


# ---------------------------------------------------------------------------
# The two cameras
# ---------------------------------------------------------------------------


async def test_a_camera_that_keeps_masks_in_privacy_masking_is_written_there():
    """Unchanged behaviour, and the reason the new table is a fallback.

    Nobody reported this path broken, so a camera it worked on has to be written
    exactly where it was written before, and without reading VideoWidget at all.
    """
    client = _client(MASKING_IS_LIVE)

    await client.async_setprivacymask(2, True)

    assert client.written == [
        (
            "/cgi-bin/configManager.cgi?action=setConfig"
            "&PrivacyMasking[0][2].Enable=true",
            True,
        )
    ]
    assert client.asked == ["PrivacyMasking"]


async def test_the_reported_camera_is_written_where_it_keeps_its_masks():
    """#1184 itself: index 2, the exact URL the reporter drove by hand."""
    client = _client(THE_1184_CAMERA)

    await client.async_setprivacymask(2, True)

    assert client.written == [
        (
            "/cgi-bin/configManager.cgi?action=setConfig"
            "&VideoWidget[0].Covers[2].EncodeBlend=true",
            True,
        )
    ]
    assert client.asked == ["PrivacyMasking", "VideoWidget"]


async def test_the_mask_can_be_hidden_again():
    """The other direction, because a mask that cannot be turned off is half a fix."""
    client = _client(THE_1184_CAMERA)

    await client.async_setprivacymask(0, False)

    assert client.written[0][0].endswith("VideoWidget[0].Covers[0].EncodeBlend=false")


async def test_preview_blend_is_never_written():
    """EncodeBlend is burned into the stream; PreviewBlend is the camera's own screen.

    The visible mask in the dump has EncodeBlend true and PreviewBlend false, so
    writing both would change something the owner did not ask about.
    """
    client = _client(THE_1184_CAMERA)

    await client.async_setprivacymask(1, True)

    assert "PreviewBlend" not in client.written[0][0]


async def test_the_covers_row_is_taken_from_this_channel():
    """A recorder channel's covers are not channel 0's.

    `VideoWidget` is channel scoped, and every other write to it in this client
    passes the channel. The service now passes the coordinator's.
    """
    client = _client({"PrivacyMasking": {}, "VideoWidget": _covers(channel=5)})

    await client.async_setprivacymask(3, True, 5)

    assert client.written[0][0].endswith("VideoWidget[5].Covers[3].EncodeBlend=true")


async def test_a_channel_with_no_covers_of_its_own_is_not_written():
    """The same table read, a different channel: index 3 of channel 5 is not channel 0's."""
    client = _client(THE_1184_CAMERA)

    with pytest.raises(ValueError) as refused:
        await client.async_setprivacymask(3, True, 5)

    assert client.written == []
    assert "channel 5" in str(refused.value)


# ---------------------------------------------------------------------------
# What it must not do
# ---------------------------------------------------------------------------


async def test_a_mask_that_exists_in_neither_table_is_refused_not_written():
    """The bug, stated as behaviour: no write may be made that cannot do anything.

    Index 7 is a row `PrivacyMasking` offers and nobody has drawn in, and it is
    past the end of `Covers`. The old code wrote it and reported success.
    """
    client = _client(THE_1184_CAMERA)

    with pytest.raises(ValueError) as refused:
        await client.async_setprivacymask(7, True)

    assert client.written == []
    message = str(refused.value)
    assert "no privacy mask at index 7" in message
    # The counts, because "there is no mask 7" and "this table is empty" send
    # somebody to different places.
    assert "24 row(s)" in message
    assert "4 cover(s)" in message


async def test_a_camera_that_answered_neither_table_says_so_instead():
    """`async_get_config` returns {} for a refusal as well as for an empty table.

    Reporting "no mask there" would send somebody to look at their masks when
    the thing to look at is why two reads came back empty.
    """
    client = _client(SAYS_NOTHING)

    with pytest.raises(ValueError) as refused:
        await client.async_setprivacymask(0, True)

    assert client.written == []
    assert "could not be read" in str(refused.value)
    assert "no privacy mask at index" not in str(refused.value)


async def test_a_row_of_zeroes_is_not_a_region():
    """The whole discriminator, on its own.

    A camera serving only empty rows must never be written in that table, however
    many rows it offers.
    """
    client = _client({"PrivacyMasking": _empty_masking_rows(), "VideoWidget": {}})

    with pytest.raises(ValueError):
        await client.async_setprivacymask(2, True)

    assert client.written == []


# ---------------------------------------------------------------------------
# The discriminator, directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"Rect[0]": "0", "Rect[1]": "0", "Rect[2]": "0", "Rect[3]": "0"}, False),
        ({"Rect[0]": "0", "Rect[1]": "0", "Rect[2]": "8175", "Rect[3]": "0"}, True),
        ({"Polygon[0][0]": "0", "Polygon[0][1]": "0"}, False),
        ({"Polygon[0][0]": "0", "Polygon[3][1]": "4096"}, True),
        ({"Enable": "true"}, False),
        ({"Rect[0]": "", "Rect[1]": "  "}, False),
        ({"Rect[0]": 0, "Rect[1]": 2048}, True),
    ],
    ids=[
        "a zero box",
        "one corner set",
        "a zero polygon",
        "one point set",
        "enabled, and nowhere",
        "blank values",
        "numbers rather than text",
    ],
)
def test_whether_a_row_describes_a_region(row, expected):
    """`Enable` on its own is never a region: that is the value #1184 wrote."""
    prefix = "table.PrivacyMasking[0][2]."
    config = {prefix + key: value for key, value in row.items()}

    assert privacy_mask_has_a_region(config, prefix) is expected


def test_a_row_prefix_does_not_match_a_longer_row_number():
    """Row 2 is not row 21.

    The closing bracket is what does that, not the trailing dot: `[2]` is not a
    prefix of `[21]`. Said here because the first version of this test claimed
    the dot, and a break that removed the dot reddened nothing at all. The break
    that does redden it removes the bracket as well.
    """
    config = {
        "table.PrivacyMasking[0][21].Rect[0]": "4096",
        "table.PrivacyMasking[0][21].Rect[1]": "2048",
    }

    assert privacy_mask_has_a_region(config, "table.PrivacyMasking[0][2].") is False
    assert privacy_mask_has_a_region(config, "table.PrivacyMasking[0][21].") is True


def test_the_row_counts_in_the_message_are_the_rows_the_camera_served():
    masking = _empty_masking_rows()
    widget = _covers()

    assert (
        privacy_mask_row_count(masking, r"table\.PrivacyMasking\[\d+\]\[(\d+)\]\.")
        == 24
    )
    assert (
        privacy_mask_row_count(widget, r"table\.VideoWidget\[0\]\.Covers\[(\d+)\]\.")
        == 4
    )
    assert (
        privacy_mask_row_count(widget, r"table\.VideoWidget\[5\]\.Covers\[(\d+)\]\.")
        == 0
    )


# ---------------------------------------------------------------------------
# Through the service
# ---------------------------------------------------------------------------


async def test_the_service_reports_a_missing_mask_as_an_error():
    """A service that answers successfully while changing nothing is the bug.

    The camera entity turns the refusal into a HomeAssistantError carrying the
    reason, the way the clock and NTP services already do.
    """
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.dahua.camera import DahuaCamera

    client = _client(THE_1184_CAMERA)
    camera = object.__new__(DahuaCamera)
    camera._coordinator = SimpleNamespace(
        client=client,
        get_channel=lambda: 0,
        get_device_name=lambda: "Front door",
    )

    with pytest.raises(HomeAssistantError) as raised:
        await camera.async_set_privacy_masking(7, True)

    assert raised.value.translation_key == "privacy_mask_not_found"
    assert client.written == []


async def test_the_service_passes_this_channel():
    """So a recorder channel writes its own covers rather than channel 0's."""
    from custom_components.dahua.camera import DahuaCamera

    client = _client({"PrivacyMasking": {}, "VideoWidget": _covers(channel=5)})
    camera = object.__new__(DahuaCamera)
    camera._coordinator = SimpleNamespace(
        client=client,
        get_channel=lambda: 5,
        get_device_name=lambda: "Gate",
    )

    await camera.async_set_privacy_masking(1, True)

    assert client.written[0][0].endswith("VideoWidget[5].Covers[1].EncodeBlend=true")
