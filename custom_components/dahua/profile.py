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


_VIDEO_IN_MODE_REJECTED_PREFIX = "__dahua.VideoInModeRejected"


def _video_in_mode_rejected_key(channel: int) -> str:
    """Return the internal rejection marker for one VideoInMode channel."""
    return f"{_VIDEO_IN_MODE_REJECTED_PREFIX}[{channel}]"


def video_in_mode_channel_is_rejected(
    data: Mapping[str, Any],
    channel: int,
) -> bool:
    """Whether raw RPC2 ownership rejected this channel as ambiguous."""
    return data.get(_video_in_mode_rejected_key(channel)) is True


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


def _video_in_mode_row_value(value: Any) -> bool:
    """Whether value is one semantic VideoInMode configuration row."""
    if not isinstance(value, Mapping):
        return False
    mode = _integer(value.get("Mode"))
    config = value.get("Config")
    if (
        mode is None
        or not isinstance(config, Sequence)
        or isinstance(config, (str, bytes))
    ):
        return False
    return bool(config) and all(_integer(item) is not None for item in config)


def resolve_video_in_mode_table_owner(
    table: Sequence[Any], channel: int
) -> tuple[Mapping[str, Any], tuple[int, ...]] | None:
    """Resolve one channel's physical owner through singleton RPC2 wrappers."""
    if channel < 0 or channel >= len(table):
        return None

    value: Any = table[channel]
    path = [channel]
    while not isinstance(value, Mapping):
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes))
            or len(value) != 1
        ):
            return None
        value = value[0]
        path.append(0)

    return value, tuple(path)


def resolve_video_in_mode_table_row(
    table: Sequence[Any], channel: int
) -> tuple[Mapping[str, Any], tuple[int, ...]] | None:
    """Resolve one complete Profile Management row and its physical path."""
    resolved = resolve_video_in_mode_table_owner(table, channel)
    if resolved is None:
        return None

    row, path = resolved
    if not _video_in_mode_row_value(row):
        return None

    return row, path


def _video_in_mode_flat_owner(
    data: Mapping[str, Any],
    channel: int,
) -> tuple[str | None, bool]:
    """Return one physical flat owner and whether its ownership is ambiguous."""
    channel_prefix = f"table.VideoInMode[{channel}]"
    physical_paths = set()
    malformed = False

    for key in data:
        if not key.startswith(channel_prefix):
            continue

        remainder = key[len(channel_prefix) :]
        if not remainder:
            malformed = True
            continue

        if remainder.startswith("."):
            physical_paths.add("")
            continue

        match = re.match(r"^((?:\[\d+\])+)\.", remainder)
        if match is None:
            malformed = True
            continue

        path = match.group(1)
        indices = [int(value) for value in re.findall(r"\[(\d+)\]", path)]

        # A singleton sequence can only own index zero at every level.
        if not indices or any(index != 0 for index in indices):
            malformed = True
            continue

        physical_paths.add(path)

    if malformed or len(physical_paths) > 1:
        return None, True

    if not physical_paths:
        return None, False

    path = physical_paths.pop()
    return channel_prefix + path, False


def _video_in_mode_flat_owners(data: Mapping[str, Any], channel: int) -> list[str]:
    """Return one unambiguous owner with Profile Management Config data."""
    owner, rejected = _video_in_mode_flat_owner(data, channel)
    if rejected or owner is None:
        return []

    if f"{owner}.Config[0]" not in data:
        return []

    return [owner]


def canonicalize_video_in_mode(
    data: Mapping[str, Any],
    *,
    resolved_channels: set[int] | None = None,
    rejected_channels: set[int] | None = None,
) -> dict[str, Any]:
    """Canonicalize authoritative channels and mark ambiguous ownership."""
    result = dict(data)

    for channel in rejected_channels or ():
        result[_video_in_mode_rejected_key(channel)] = True

    channels = set()
    table_prefix = "table.VideoInMode["

    for key in data:
        if not key.startswith(table_prefix):
            continue

        end = key.find("]", len(table_prefix))
        if end == -1:
            continue

        channel = _integer(key[len(table_prefix) : end])
        if channel is not None and channel >= 0:
            channels.add(channel)

    for channel in channels:
        if video_in_mode_channel_is_rejected(result, channel):
            continue

        owner, flat_rejected = _video_in_mode_flat_owner(data, channel)

        if resolved_channels is None:
            if flat_rejected:
                result[_video_in_mode_rejected_key(channel)] = True
                continue
        elif channel not in resolved_channels:
            continue

        if owner is None:
            continue

        channel_prefix = f"table.VideoInMode[{channel}]"
        physical_items = [
            (key, value)
            for key, value in data.items()
            if key == owner or key.startswith(owner + ".")
        ]

        for key in tuple(result):
            if not key.startswith(channel_prefix):
                continue

            remainder = key[len(channel_prefix) :]
            if not remainder or remainder.startswith((".", "[")):
                del result[key]

        for key, value in physical_items:
            result[channel_prefix + key[len(owner) :]] = value

    return result


def profile_management_values(
    data: Mapping[str, Any], channel: int
) -> tuple[int, tuple[int, ...]] | None:
    """Extract one channel's Profile Management values from flattened config."""
    if video_in_mode_channel_is_rejected(data, channel):
        return None

    owners = _video_in_mode_flat_owners(data, channel)
    if len(owners) != 1:
        return None

    prefix = owners[0]
    mode = _integer(data.get(f"{prefix}.Mode"))
    first = _integer(data.get(f"{prefix}.Config[0]"))
    if mode is None or first is None:
        return None

    values = [first]
    index = 1
    while (raw := data.get(f"{prefix}.Config[{index}]")) is not None:
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
    resolved = resolve_video_in_mode_table_row(table, channel)
    if resolved is None:
        if channel < 0 or channel >= len(table):
            raise ConnectionError(
                f"VideoInMode has no channel {channel} ({len(table)} entries)"
            )
        raise ConnectionError(
            f"VideoInMode channel {channel} is not an unambiguous configuration row"
        )
    _row, location = resolved
    result = deepcopy(list(table))
    target = result
    for index in location:
        target = target[index]
    mode, config = wanted
    target["Mode"] = mode
    target["Config"] = list(config)
    return result


def resolve_profile_management_table(table: Sequence[Any], channel: int) -> str | None:
    """Resolve Profile Management from an RPC2 table without flattening it."""
    resolved = resolve_video_in_mode_table_row(table, channel)
    if resolved is None:
        return None
    row, _location = resolved
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
    if video_in_mode_channel_is_rejected(data, channel):
        return None

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
    resolved = resolve_video_in_mode_table_row(table, channel)
    if resolved is None:
        return None
    owner, _path = resolved
    time_sections = owner.get("TimeSection")
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
    resolved = resolve_video_in_mode_table_row(table, channel)
    if resolved is None:
        raise ConnectionError(
            f"VideoInMode has no Schedule table for channel {channel}"
        )
    _owner, path = resolved
    updated_table = deepcopy(list(table))
    target = updated_table[path[0]]
    for index in path[1:]:
        target = target[index]
    target["TimeSection"][row_index][
        section_index
    ] = f"{match.group('prefix')} {day_start:%H:%M:%S}-{night_start:%H:%M:%S}"
    return updated_table
