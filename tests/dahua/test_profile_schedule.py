"""Profile Schedule parsing is camera-owned, channel-owned and fail-closed."""

from datetime import time

import pytest

from custom_components.dahua.profile import (
    ProfileScheduleTimes,
    parse_profile_schedule_interval,
    profile_schedule_table_with_times,
    resolve_profile_schedule_table,
    resolve_profile_schedule_times,
)


def test_physically_measured_schedule_accepts_one_minute_api_precision():
    assert parse_profile_schedule_interval("1 08:23:00-15:30:00") == (
        ProfileScheduleTimes(time(8, 23), time(15, 30))
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "0 08:23:00-15:30:00",
        "2 08:23:00-15:30:00",
        "7 08:23:00-15:30:00",
        "1 24:00:00-15:30:00",
        "1 08:23-15:30",
    ],
)
def test_unknown_disabled_or_invalid_intervals_fail_closed(value):
    assert parse_profile_schedule_interval(value) is None


def test_exact_channel_ownership_has_no_row_zero_fallback():
    data = {"table.VideoInMode[0].TimeSection[0][0]": "1 08:23:00-15:30:00"}
    assert resolve_profile_schedule_times(data, 0) == ProfileScheduleTimes(
        time(8, 23), time(15, 30)
    )
    assert resolve_profile_schedule_times(data, 1) is None


def test_two_enabled_intervals_are_ambiguous_and_unsupported():
    data = {
        "table.VideoInMode[0].TimeSection[0][0]": "1 08:23:00-15:30:00",
        "table.VideoInMode[0].TimeSection[0][1]": "1 09:00:00-16:00:00",
    }
    assert resolve_profile_schedule_times(data, 0) is None


def _table(interval="1 05:30:00-15:30:00"):
    return [
        {
            "Mode": 1,
            "Config": [0, 1],
            "TimeSection": [[interval, "0 00:00:00-00:00:00"]],
            "Future": {"keep": True},
        },
        {"OtherChannel": True},
    ]


def test_whole_table_transform_preserves_every_unowned_value():
    original = _table()
    updated = profile_schedule_table_with_times(original, 0, time(8, 23), time(17, 47))
    assert updated[0]["TimeSection"][0][0] == "1 08:23:00-17:47:00"
    assert updated[0]["Config"] == [0, 1]
    assert updated[0]["Mode"] == 1
    assert updated[0]["Future"] == {"keep": True}
    assert updated[1] == {"OtherChannel": True}
    assert original[0]["TimeSection"][0][0] == "1 05:30:00-15:30:00"


def test_structured_schedule_requires_the_exact_channel():
    assert resolve_profile_schedule_table(_table(), 0) == ProfileScheduleTimes(
        time(5, 30), time(15, 30)
    )
    assert resolve_profile_schedule_table(_table(), 3) is None


def test_subminute_write_is_rejected_instead_of_rounded():
    with pytest.raises(ValueError, match="whole-minute"):
        profile_schedule_table_with_times(_table(), 0, time(8, 23, 1), time(17, 47))
