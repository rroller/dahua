"""A keepalive that has stopped must be started again, not left as a corpse.

The keepalive loop returns on any failed keepalive, deliberately: it drops the
login so the next read logs in again. But the guard that starts it asked only
whether the slot was empty, and a finished task is not an empty slot -- so the
session came back and the keepalive did not, for the life of that host's
session.

Nothing looks broken when that happens, which is the problem. Reads still work;
they just each pay a fresh login after any idle gap longer than the device's
session timeout, which is the entire cost this was written to remove.

Observed on a DHI-NVR5464-16P-EI running 0.9.94: ten entries sharing one
session, `rpc2_session_open` true, `rpc2_ruled_out_for_host` false, and
`rpc2_keepalive_running` false -- with the device answering `global.keepAlive`
perfectly well when asked directly.
"""

import asyncio
from contextlib import suppress

from custom_components.dahua.client import keepalive_needs_starting


class _Finished:
    """Stands in for the task the failure path leaves behind."""

    @staticmethod
    def done():
        return True


class _Running:
    """Stands in for a keepalive still looping."""

    @staticmethod
    def done():
        return False


def test_no_keepalive_at_all_needs_one():
    assert keepalive_needs_starting(None) is True


def test_a_finished_keepalive_needs_a_new_one():
    """The case that regressed: the loop returned and was never replaced."""
    assert keepalive_needs_starting(_Finished()) is True


def test_a_running_keepalive_is_left_alone():
    """Starting a second would double the keepalive traffic against the device."""
    assert keepalive_needs_starting(_Running()) is False


def test_a_cancelled_keepalive_also_needs_replacing():
    """Cancellation completes a task too, and a later read may still want one."""
    loop = asyncio.new_event_loop()
    try:
        async def forever():
            await asyncio.sleep(3600)

        task = loop.run_until_complete(_spawn(loop, forever))
        task.cancel()
        with suppress(asyncio.CancelledError):
            loop.run_until_complete(task)

        assert keepalive_needs_starting(task) is True
    finally:
        loop.close()


async def _spawn(loop, coro_fn):
    """Create a task on the running loop, which ensure_future needs."""
    return asyncio.ensure_future(coro_fn())
