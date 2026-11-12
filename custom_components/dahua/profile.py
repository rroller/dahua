"""Profile-management interpretation shared by the coordinator and client."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import time as datetime_time
from typing import Any

PROFILE_MANAGEMENT_VALUES_BY_OPTION: dict[str, tuple[int, tuple[int, ...]]] = {
    "normal": (0, (2,)),
    "full_time_day": (0, (0,)),
    "full_time_night": (0, (1,)),
    "schedule": (1, (0, 1)),
}
PROFILE_MANAGEMENT_OPTIONS = tuple(PROFILE_MANAGEMENT_VALUES_BY_OPTION)
PROFILE_MANAGEMENT_OPTION_BY_VALUES = {
    values: option for option, values in PROFILE_MANAGEMENT_VALUES_BY_OPTION.items()
}


def _integer(value: Any) -> int | None:
    """Return an integer-shaped device value without accepting booleans."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        if value and value.lstrip("-").isdigit():
            return int(value)
    return None


def profile_management_values(
    data: Mapping[str, Any], channel: int
) -> tuple[int, tuple[int, ...]] | None:
    """Extract one channel's Profile Management values from flattened config."""
    mode = _integer(data.get(f"table.VideoInMode[{channel}].Mode"))
    first = _integer(data.get(f"table.VideoInMode[{channel}].Config[0]"))
    if mode is None or first is None:
        return None

    values = [first]
    index = 1
    while (
        raw := data.get(f"table.VideoInMode[{channel}].Config[{index}]")
    ) is not None:
        item = _integer(raw)
        if item is None:
            return None
        values.append(item)
        index += 1
    return mode, tuple(values)


def resolve_profile_management_mode(
    data: Mapping[str, Any], channel: int
) -> str | None:
    """Return the camera-reported management mode for the exact channel."""
    values = profile_management_values(data, channel)
    if values is None:
        return None
    return PROFILE_MANAGEMENT_OPTION_BY_VALUES.get(values)


def profile_management_table_with_option(
    table: Sequence[Any], channel: int, option: str
) -> list[Any]:
    """Copy a VideoInMode table and change only one channel's Config and Mode."""
    wanted = PROFILE_MANAGEMENT_VALUES_BY_OPTION.get(option)
    if wanted is None:
        raise ValueError(f"Unknown profile management option: {option}")
    if channel < 0 or channel >= len(table):
        raise ConnectionError(
            f"VideoInMode has no channel {channel} ({len(table)} entries)"
        )
    if not isinstance(table[channel], Mapping):
        raise ConnectionError(
            f"VideoInMode channel {channel} is not a configuration row"
        )

    result = deepcopy(list(table))
    mode, config = wanted
    result[channel]["Mode"] = mode
    result[channel]["Config"] = list(config)
    return result


def resolve_profile_management_table(table: Sequence[Any], channel: int) -> str | None:
    """Resolve Profile Management from an RPC2 table without flattening it."""
    if channel < 0 or channel >= len(table) or not isinstance(table[channel], Mapping):
        return None
    row = table[channel]
    mode = _integer(row.get("Mode"))
    config = row.get("Config")
    if (
        mode is None
        or not isinstance(config, Sequence)
        or isinstance(config, (str, bytes))
    ):
        return None
    values = tuple(_integer(value) for value in config)
    if any(value is None for value in values):
        return None
    return PROFILE_MANAGEMENT_OPTION_BY_VALUES.get((mode, values))


@dataclass(frozen=True)
class ProfileScheduleTimes:
    """Camera-reported start times for the Day and Night profile periods."""

    day_start: datetime_time
    night_start: datetime_time


_PROFILE_SCHEDULE_INTERVAL = re.compile(
    r"^(?P<prefix>\d+)\s+"
    r"(?P<day_start>\d{2}:\d{2}:\d{2})-"
    r"(?P<night_start>\d{2}:\d{2}:\d{2})$"
)
_PROFILE_SCHEDULE_FLAT_KEY = re.compile(
    r"^table\.VideoInMode\[(?P<channel>\d+)\]\."
    r"TimeSection\[(?P<row>\d+)\]\[(?P<section>\d+)\]$"
)


def _parse_profile_schedule_time(
    value: str,
) -> datetime_time | None:
    """Parse a camera time without accepting next-day normalization."""
    components = value.split(":")

    if len(components) != 3:
        return None

    try:
        hour, minute, second = (int(component) for component in components)
    except ValueError:
        return None

    if not 0 <= hour <= 23:
        return None
    if not 0 <= minute <= 59:
        return None
    if not 0 <= second <= 59:
        return None

    return datetime_time(hour, minute, second)


def parse_profile_schedule_interval(
    value: Any,
) -> ProfileScheduleTimes | None:
    """Parse one enabled Schedule interval without guessing its shape."""
    if not isinstance(value, str):
        return None

    match = _PROFILE_SCHEDULE_INTERVAL.fullmatch(value.strip())

    if match is None or match.group("prefix") != "1":
        return None

    day_start = _parse_profile_schedule_time(match.group("day_start"))
    night_start = _parse_profile_schedule_time(match.group("night_start"))

    if day_start is None or night_start is None:
        return None

    return ProfileScheduleTimes(
        day_start=day_start,
        night_start=night_start,
    )


def _single_profile_schedule_interval(
    intervals: Sequence[tuple[tuple[int, int], Any]],
) -> tuple[tuple[int, int], ProfileScheduleTimes] | None:
    """Return the sole enabled, parseable Schedule interval or fail closed."""
    parsed_intervals = []
    for location, value in intervals:
        schedule_times = parse_profile_schedule_interval(value)
        if schedule_times is not None:
            parsed_intervals.append((location, schedule_times))
            continue
        # Only a known disabled entry can safely be left out of the count.
        # An unknown prefix or malformed enabled neighbor could be another
        # active interval, so one parseable entry alone is not enough proof.
        match = (
            _PROFILE_SCHEDULE_INTERVAL.fullmatch(value.strip())
            if isinstance(value, str)
            else None
        )
        if match is None or match.group("prefix") != "0":
            return None
    return parsed_intervals[0] if len(parsed_intervals) == 1 else None


def resolve_profile_schedule_times(
    data: Mapping[str, Any], channel: int
) -> ProfileScheduleTimes | None:
    """Resolve Schedule times from flattened VideoInMode for the exact channel."""
    channel_intervals = []
    for key, value in data.items():
        match = _PROFILE_SCHEDULE_FLAT_KEY.fullmatch(key)
        if match is None or int(match.group("channel")) != channel:
            continue
        channel_intervals.append(
            ((int(match.group("row")), int(match.group("section"))), value)
        )
    resolved_interval = _single_profile_schedule_interval(channel_intervals)
    return resolved_interval[1] if resolved_interval is not None else None


def _profile_schedule_intervals_from_table(
    table: Sequence[Any], channel: int
) -> list[tuple[tuple[int, int], Any]] | None:
    """Return every TimeSection entry owned by one structured channel row."""
    if channel < 0 or channel >= len(table) or not isinstance(table[channel], Mapping):
        return None
    time_sections = table[channel].get("TimeSection")
    if not isinstance(time_sections, Sequence) or isinstance(
        time_sections, (str, bytes)
    ):
        return None
    channel_intervals = []
    for row_index, row in enumerate(time_sections):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            return None
        channel_intervals.extend(
            ((row_index, section_index), value)
            for section_index, value in enumerate(row)
        )
    return channel_intervals


def resolve_profile_schedule_table(
    table: Sequence[Any], channel: int
) -> ProfileScheduleTimes | None:
    """Resolve Schedule times from a structured RPC2 VideoInMode table."""
    channel_intervals = _profile_schedule_intervals_from_table(table, channel)
    if channel_intervals is None:
        return None
    resolved_interval = _single_profile_schedule_interval(channel_intervals)
    return resolved_interval[1] if resolved_interval is not None else None


def profile_schedule_table_with_times(
    table: Sequence[Any],
    channel: int,
    day_start: datetime_time,
    night_start: datetime_time,
) -> list[Any]:
    """Copy VideoInMode and replace only one channel's Schedule endpoints."""
    for value_name, schedule_time in (
        ("day_start", day_start),
        ("night_start", night_start),
    ):
        if not isinstance(schedule_time, datetime_time):
            raise TypeError(f"{value_name} must be a datetime.time")
        if schedule_time.second or schedule_time.microsecond:
            raise ValueError(f"{value_name} must use whole-minute precision")
    channel_intervals = _profile_schedule_intervals_from_table(table, channel)
    if channel_intervals is None:
        raise ConnectionError(
            f"VideoInMode has no Schedule table for channel {channel}"
        )
    resolved_interval = _single_profile_schedule_interval(channel_intervals)
    if resolved_interval is None:
        raise ConnectionError(
            f"VideoInMode channel {channel} has no unambiguous Schedule interval"
        )
    (row_index, section_index), _schedule_times = resolved_interval
    original_interval = dict(channel_intervals)[(row_index, section_index)]
    match = _PROFILE_SCHEDULE_INTERVAL.fullmatch(original_interval.strip())
    updated_table = deepcopy(list(table))
    updated_table[channel]["TimeSection"][row_index][
        section_index
    ] = f"{match.group('prefix')} {day_start:%H:%M:%S}-{night_start:%H:%M:%S}"
    return updated_table
