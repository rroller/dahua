"""A channel that never comes back must not probe the device for ever.

@rroller on #1085: the startup retry waited exactly one scan interval every
round, and each round builds a fresh coordinator and runs its whole
initialization. A camera unplugged for a month therefore paid the full setup
cost against the recorder every two minutes, indefinitely, and the temporary
coordinator could not moderate it because it is discarded at the end of each
round, so its own `update_interval` never sees a second attempt.

The delay now doubles and is capped, mirroring what Home Assistant itself does
for an entry raising `ConfigEntryNotReady`:

    wait_time = min(2**self._tries * 5, SETUP_RETRY_MAX_WAIT)

The ceiling is imported from `homeassistant.config_entries` rather than copied,
so a change there is inherited rather than drifted from.

The state that has to survive a round lives in the loop, which is the only
thing that does.
"""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SETUP_RETRY_MAX_WAIT, ConfigSubentry

from custom_components import dahua
from custom_components.dahua.config_flow import CHANNEL_SUBENTRY
from tests.dahua.test_setup_lifecycle import _entry, _no_platforms, _setting_up


class _Slept:
    """Records what the retry loop waited, and never actually waits.

    The durations are the subject here, so sleeping them would make the test
    measure the clock instead. Counting is what the claim is about, and a
    timing test of a growing delay would be the flakiest thing in the suite.
    """

    def __init__(self, rounds):
        self.waits = []
        self._rounds = rounds

    async def sleep(self, seconds):
        self.waits.append(seconds)
        if len(self.waits) >= self._rounds:
            raise asyncio.CancelledError("enough rounds for this test")


async def _waits_over(hass, rounds, interval):
    """The delays the loop would use, over `rounds` failing attempts."""
    entry = _entry(hass)
    for channel in (0, 1):
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data={"channel": channel},
                subentry_type=CHANNEL_SUBENTRY,
                title=f"Channel {channel}",
                unique_id=f"backoff-{channel}",
            ),
        )

    async def update(coordinator):
        if coordinator.get_channel() == 1:
            raise TimeoutError("this channel is simply not there")
        return {"serialNumber": "SER1"}

    slept = _Slept(rounds)
    starts, forward, unload = _no_platforms(hass)
    _setting_up(hass, entry)
    with patch.object(
        dahua.DahuaDataUpdateCoordinator, "_async_update_data", update
    ), starts, forward, unload, patch.object(
        dahua, "get_configured_scan_interval", return_value=timedelta(seconds=interval)
    ), patch.object(
        dahua.asyncio, "sleep", slept.sleep
    ), patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as reload:
        assert await dahua.async_setup_entry(hass, entry)
        await asyncio.gather(*entry._background_tasks, return_exceptions=True)
        reload.assert_not_called()
        await dahua.async_unload_entry(hass, entry)
    return slept.waits


# --- the delay grows ----------------------------------------------------------


async def test_the_first_wait_is_still_one_scan_interval(hass):
    """Unchanged, deliberately. A transient failure must recover as fast as it
    did before; only a channel that keeps failing is slowed down."""
    waits = await _waits_over(hass, rounds=1, interval=120)

    assert waits == [120]


async def test_each_failure_doubles_the_wait(hass):
    waits = await _waits_over(hass, rounds=4, interval=120)

    assert waits == [120, 240, 480, 600]


async def test_the_wait_stops_growing_at_the_ceiling(hass):
    """And the ceiling is Home Assistant's, not a number invented here."""
    waits = await _waits_over(hass, rounds=7, interval=120)

    assert waits[-3:] == [SETUP_RETRY_MAX_WAIT] * 3
    assert max(waits) == SETUP_RETRY_MAX_WAIT


async def test_the_cap_never_retries_faster_than_the_configured_interval(hass):
    """An entry polled more slowly than the ceiling must not be retried at the
    ceiling: that would be the backoff making the device busier."""
    interval = SETUP_RETRY_MAX_WAIT + 300
    waits = await _waits_over(hass, rounds=3, interval=interval)

    assert waits == [interval] * 3


async def test_a_zero_interval_stays_zero(hass):
    """The shape the rest of the suite runs in. Doubling nothing is nothing, so
    the existing recovery tests keep spinning at full speed."""
    waits = await _waits_over(hass, rounds=3, interval=0)

    assert waits == [0, 0, 0]


# --- and the two properties the backoff must not break ------------------------


async def test_a_healthy_channel_is_never_probed_again(hass):
    """The reason this loop is selective at all. Backing off must not start
    re-initializing the channels that came up.

    Counted rather than asserted on the first round, because the point is that
    it stays true across the slower rounds too.
    """
    entry = _entry(hass)
    for channel in (0, 1):
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data={"channel": channel},
                subentry_type=CHANNEL_SUBENTRY,
                title=f"Channel {channel}",
                unique_id=f"healthy-{channel}",
            ),
        )
    attempts = {0: 0, 1: 0}

    async def update(coordinator):
        channel = coordinator.get_channel()
        attempts[channel] += 1
        if channel == 1:
            raise TimeoutError("still absent")
        return {"serialNumber": "SER1"}

    slept = _Slept(3)
    starts, forward, unload = _no_platforms(hass)
    _setting_up(hass, entry)
    with patch.object(
        dahua.DahuaDataUpdateCoordinator, "_async_update_data", update
    ), starts, forward, unload, patch.object(
        dahua, "get_configured_scan_interval", return_value=timedelta(seconds=120)
    ), patch.object(
        dahua.asyncio, "sleep", slept.sleep
    ), patch.object(
        hass.config_entries, "async_schedule_reload"
    ):
        assert await dahua.async_setup_entry(hass, entry)
        await asyncio.gather(*entry._background_tasks, return_exceptions=True)
        await dahua.async_unload_entry(hass, entry)

    assert attempts[0] == 1, "the healthy channel was re-probed"
    assert attempts[1] > 1, "the failing channel was not retried at all"


def test_unload_cancelling_the_task_is_already_covered():
    """@rroller asked for this alongside the backoff, and it exists already.

    `test_startup_retries_are_cancelled_and_closed_on_unload` in
    `test_project_audit_bugs.py` unloads the entry while the loop is asleep on
    a 3600 second interval and asserts the background task is gone and the
    temporary client closed. That is the same property a longer backoff makes
    more important, so a second, weaker version here would add risk rather
    than coverage.

    This asserts the test it relies on still exists, so that deleting or
    renaming it is noticed here rather than leaving the claim above dangling.
    """
    from tests.dahua import test_project_audit_bugs

    assert hasattr(
        test_project_audit_bugs,
        "test_startup_retries_are_cancelled_and_closed_on_unload",
    )


# --- the ceiling is borrowed, not invented ------------------------------------


def test_the_ceiling_is_home_assistant_s_own():
    """Derived rather than duplicated. If Home Assistant changes how long it
    waits before retrying a failed entry, this inherits it, and a hardcoded
    600 here would quietly stop agreeing."""
    import inspect

    source = inspect.getsource(dahua._async_retry_startup_channels)

    assert "SETUP_RETRY_MAX_WAIT" in source
    assert "600" not in source, "the ceiling is written out instead of imported"
