"""Profile-management interpretation shared by the coordinator and client."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
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
