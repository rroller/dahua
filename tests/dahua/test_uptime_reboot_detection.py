"""Tests for host-shared camera uptime and reboot generation detection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components import dahua as dahua_module
from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient


def _coordinator(address, uptime):
    """Build the minimum coordinator shape required by the helper."""
    return SimpleNamespace(
        _address=address,
        client=SimpleNamespace(
            async_get_uptime_last=AsyncMock(side_effect=uptime)
            if isinstance(uptime, list)
            else AsyncMock(return_value=uptime)
        ),
    )


@pytest.fixture(autouse=True)
def _clear_host_uptime_state():
    """Do not let one test's host sample affect another."""
    dahua_module._HOST_UPTIME_STATE.clear()
    dahua_module._HOST_UPTIME_LOCKS.clear()

    yield

    dahua_module._HOST_UPTIME_STATE.clear()
    dahua_module._HOST_UPTIME_LOCKS.clear()


async def test_same_host_multiple_coordinators_share_one_uptime_read():
    """NVR channels polling together must not each hit RPC2."""
    first = _coordinator("10.0.0.1", 500)
    second = _coordinator("10.0.0.1", 500)

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[100.0, 100.0, 100.1],
    ):
        first_generation = (
            await dahua_module._async_get_host_uptime_generation(first)
        )
        second_generation = (
            await dahua_module._async_get_host_uptime_generation(second)
        )

    assert first_generation == 0
    assert second_generation == 0

    first.client.async_get_uptime_last.assert_awaited_once()
    second.client.async_get_uptime_last.assert_not_awaited()


async def test_different_hosts_do_not_share_uptime_samples():
    first = _coordinator("10.0.0.1", 500)
    second = _coordinator("10.0.0.2", 700)

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[
            100.0,
            100.0,
            100.1,
            100.1,
        ],
    ):
        await dahua_module._async_get_host_uptime_generation(first)
        await dahua_module._async_get_host_uptime_generation(second)

    first.client.async_get_uptime_last.assert_awaited_once()
    second.client.async_get_uptime_last.assert_awaited_once()

    assert set(dahua_module._HOST_UPTIME_STATE) == {
        "10.0.0.1",
        "10.0.0.2",
    }


async def test_uptime_rollback_increments_reboot_generation():
    coordinator = _coordinator(
        "10.0.0.1",
        [500, 12],
    )

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[
            100.0,
            100.0,
            106.0,
            106.0,
        ],
    ):
        before = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )
        after = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )

    assert before == 0
    assert after == 1
    assert coordinator.client.async_get_uptime_last.await_count == 2

    state = dahua_module._HOST_UPTIME_STATE["10.0.0.1"]
    assert state["uptime"] == 12
    assert state["generation"] == 1


async def test_normal_uptime_increase_does_not_increment_generation():
    coordinator = _coordinator(
        "10.0.0.1",
        [500, 506],
    )

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[
            100.0,
            100.0,
            106.0,
            106.0,
        ],
    ):
        first = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )
        second = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )

    assert first == 0
    assert second == 0


async def test_failed_uptime_read_is_deduped_for_same_poll_burst():
    coordinator = _coordinator("10.0.0.1", 500)
    coordinator.client.async_get_uptime_last = AsyncMock(
        side_effect=RuntimeError("RPC2 unavailable")
    )

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[
            100.0,
            100.0,
            100.1,
        ],
    ):
        first = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )
        second = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )

    # Uptime support is optional; failure must not become a poll failure.
    assert first == 0
    assert second == 0

    # The second NVR-channel-style request is suppressed even though the
    # first host read failed.
    coordinator.client.async_get_uptime_last.assert_awaited_once()

    state = dahua_module._HOST_UPTIME_STATE["10.0.0.1"]
    assert state["uptime"] is None
    assert state["generation"] == 0


# --- the client method the tests above mock out -----------------------------
#
# Everything above hands the helper a fake client. These are the real
# DahuaClient.async_get_uptime_last, which reads magicBox.getUpTime over RPC2 and
# retries once, dropping the login in between.
#
# The reason it retries at all is the thing it is for: a reboot invalidates the old
# RPC2 login, so the read that would detect the reboot is also the read most likely to
# fail because of it. Without dropping the login the retry fails the same way, and a
# rebooted device never reports its new uptime.


class _Rpc2:
    """Answers magicBox.getUpTime with whatever the test lines up."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.calls = []

    async def request(self, method, params=None, **kwargs):
        self.calls.append(method)
        answer = self._answers.pop(0) if self._answers else {}
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Holder:
    """The shape the RPC2 registry holds. `keepalive` is read by conftest's teardown."""

    def __init__(self, client):
        self.client = client
        self.task = object()
        self.keepalive = None
        self.refs = 1


def _uptime_client(monkeypatch, answers, use_rpc2=True):
    """A real client whose RPC2 session answers from `answers`.

    Returns the client, the holder, the fake RPC2 and the list of `holder.task` values
    seen at each session fetch, which is how the login drop is observed.
    """
    rpc2 = _Rpc2(answers)
    client = DahuaClient("u", "p", "cam", 80, 554, object(), use_rpc2=use_rpc2)
    holder = _Holder(rpc2)
    seen = []

    async def _shared(self):
        seen.append(holder.task)
        return holder

    monkeypatch.setattr(DahuaClient, "_shared_rpc2", _shared)
    # Registered because the failure path looks the holder up here rather than reusing
    # the one it already has.
    client_module._HOST_RPC2[client._rpc2_key()] = holder
    return client, holder, rpc2, seen


def _answer(last):
    return {"params": {"info": {"Last": last}}}


async def test_the_uptime_comes_back_as_an_int(monkeypatch):
    client, _, rpc2, _ = _uptime_client(monkeypatch, [_answer(500)])

    assert await client.async_get_uptime_last() == 500
    assert rpc2.calls == ["magicBox.getUpTime"]


async def test_a_device_that_answers_with_a_string_still_gives_an_int(monkeypatch):
    """Dahua is inconsistent about this, and the caller compares it with `<`."""
    client, _, _, _ = _uptime_client(monkeypatch, [_answer("500")])

    value = await client.async_get_uptime_last()

    assert value == 500
    assert isinstance(value, int)


async def test_rpc2_being_off_is_an_error_rather_than_a_reading(monkeypatch):
    """There is no CGI equivalent, so this cannot be answered another way."""
    client, _, rpc2, _ = _uptime_client(monkeypatch, [_answer(500)], use_rpc2=False)

    with pytest.raises(RuntimeError):
        await client.async_get_uptime_last()

    assert rpc2.calls == [], "asked the device anyway"


async def test_a_missing_last_raises_rather_than_reading_as_a_reboot(monkeypatch):
    """The important one. The caller treats a value lower than the previous one as a
    reboot, so a missing reading must not come back as 0 or None: it would be lower
    than any real uptime and would report a reboot that never happened, on every poll.

    Raising is the safe answer because the caller catches everything and keeps the
    generation it already had.
    """
    client, _, _, _ = _uptime_client(
        monkeypatch, [{"params": {"info": {}}}, {"params": {"info": {}}}])

    with pytest.raises(RuntimeError):
        await client.async_get_uptime_last()


async def test_an_answer_with_no_info_at_all_raises_too(monkeypatch):
    client, _, _, _ = _uptime_client(monkeypatch, [{}, {}])

    with pytest.raises(RuntimeError):
        await client.async_get_uptime_last()


async def test_a_first_attempt_that_fails_is_retried(monkeypatch):
    client, _, rpc2, _ = _uptime_client(
        monkeypatch, [TimeoutError(), _answer(500)])

    assert await client.async_get_uptime_last() == 500
    assert len(rpc2.calls) == 2


async def test_the_login_is_dropped_before_the_retry(monkeypatch):
    """The whole reason for retrying. A reboot invalidates the RPC2 login, so the
    second attempt has to get a fresh session rather than the dead one. `task` is what
    the registry uses to decide whether to log in again, so it has to be None by the
    time the second attempt asks for the session."""
    client, holder, _, seen = _uptime_client(
        monkeypatch, [TimeoutError(), _answer(500)])
    original = holder.task

    await client.async_get_uptime_last()

    assert len(seen) == 2, "did not fetch the session twice"
    assert seen[0] is original
    assert seen[1] is None, (
        "the second attempt reused the login the first one failed on, which is the "
        "one thing a reboot guarantees is dead")


async def test_both_attempts_failing_raises(monkeypatch):
    client, _, rpc2, _ = _uptime_client(
        monkeypatch, [TimeoutError(), TimeoutError()])

    with pytest.raises(TimeoutError):
        await client.async_get_uptime_last()

    assert len(rpc2.calls) == 2


async def test_it_stops_after_two_attempts(monkeypatch):
    """Bounded on purpose: this runs inside the coordinator poll, and the caller
    already caches a failure so the next poll is not another burst of retries."""
    client, _, rpc2, _ = _uptime_client(
        monkeypatch, [TimeoutError(), TimeoutError(), _answer(500)])

    with pytest.raises(TimeoutError):
        await client.async_get_uptime_last()

    assert len(rpc2.calls) == 2, "retried more than once"


# --- and the one exception that is not the device's fault --------------------

async def test_a_cancelled_uptime_read_is_not_treated_as_an_unsupported_device():
    """Cancellation is how Home Assistant stops a coordinator, not something the
    device did.

    Everything else in `_async_get_host_uptime_generation` is swallowed on purpose,
    because uptime is an optional enhancement and a device that cannot answer must
    not fail the poll. A CancelledError caught by that same handler would be
    swallowed too, so a coordinator being shut down would carry on into the rest of
    its cycle and asyncio would warn that the cancellation was ignored.

    The read is deliberately not cached either. `last_read` exists to stop eleven
    channels of a recorder repeating a request the device just refused, and a
    cancelled read is not a refusal: the next poll should try it.
    """
    coordinator = _coordinator("10.0.0.1", 500)
    coordinator.client.async_get_uptime_last = AsyncMock(
        side_effect=asyncio.CancelledError()
    )

    with pytest.raises(asyncio.CancelledError):
        await dahua_module._async_get_host_uptime_generation(coordinator)

    state = dahua_module._HOST_UPTIME_STATE["10.0.0.1"]
    assert state["last_read"] == 0.0, (
        "a cancelled read was cached as a failed one, so the next poll will skip it")
