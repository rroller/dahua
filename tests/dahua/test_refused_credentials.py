"""Credentials a host keeps refusing must stop being offered.

#729: an NVR asked for reauth, and then refused the correct password typed into
the reauth dialog. The loop is that the integration never stopped polling:

  - a 401 in the init block called async_start_reauth by hand and raised
    UpdateFailed, which leaves `auth_failed` False in Home Assistant's
    coordinator, so `_schedule_refresh()` still runs;
  - a 401 on a later poll did not start a reauth at all;
  - the event stream kept re-attaching on its own timer.

A Dahua box locks the source IP for about thirty minutes after repeated failed
logins, so every one of those retries renewed the lock the user was waiting out.

The fix must not re-create #714, where one 401 was treated as proof of a wrong
password. Channels of one NVR share a digest challenge and a raced nonce is
refused exactly like a bad credential, so a single refusal stays non-fatal.
"""

from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

import custom_components.dahua as dahua
from custom_components.dahua import client as client_module
from custom_components.dahua.client import note_credentials_accepted
from custom_components.dahua import (
    DahuaDataUpdateCoordinator,
    DahuaHostEventStream,
    MAX_AUTH_REFUSALS,
    async_record_host_auth_refusal,
    async_record_host_success,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed


@pytest.fixture(autouse=True)
def _no_issue_registry(monkeypatch):
    """Clearing the count also withdraws this host's repair issues.

    That goes through the issue registry, which wants a real hass. The count is
    what these tests are about, so stub the registry rather than build one.
    """
    monkeypatch.setattr(dahua.ir, "async_delete_issue", lambda *a, **k: None)


class _Hass:
    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro):
        # The coroutine is never awaited here; close it so Python does not warn.
        try:
            coro.close()
        except AttributeError:
            pass
        self.tasks.append(coro)


def _coordinator(address="10.0.0.5", entry_id="e1"):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._address = address
    c.hass = _Hass()
    c.config_entry = type("E", (), {"entry_id": entry_id})()
    c.backed_off = []
    c._back_off_poll_interval = lambda n: c.backed_off.append(n)
    return c


def _clean():
    """These counters are module level and shared, so every test starts empty."""
    dahua._HOST_FAILURES.clear()
    client_module._CREDENTIALS_ACCEPTED.clear()
    client_module._CREDENTIALS_REFUSED.clear()


def _401():
    return ClientResponseError(request_info=None, history=(), status=401)


# --- one refusal is not a wrong password (#714 must not come back) -----------


def test_the_first_refusal_is_an_ordinary_failed_poll():
    _clean()
    c = _coordinator()

    result = c._auth_refused(_401())

    assert isinstance(result, UpdateFailed)
    assert not isinstance(result, ConfigEntryAuthFailed)


def test_refusals_below_the_budget_stay_ordinary():
    _clean()
    c = _coordinator()

    for _ in range(MAX_AUTH_REFUSALS - 1):
        assert isinstance(c._auth_refused(_401()), UpdateFailed)


def test_a_success_in_between_clears_the_count():
    """Otherwise a device that races one nonce a day eventually locks itself out."""
    _clean()
    c = _coordinator()

    for _ in range(MAX_AUTH_REFUSALS - 1):
        c._auth_refused(_401())
    async_record_host_success(c.hass, c._address)

    assert isinstance(
        c._auth_refused(_401()), UpdateFailed
    ), "the count survived a success, so transient 401s accumulate for ever"


# --- at the budget, stop ----------------------------------------------------


def test_at_the_budget_it_asks_for_new_credentials():
    _clean()
    c = _coordinator()

    for _ in range(MAX_AUTH_REFUSALS - 1):
        c._auth_refused(_401())
    result = c._auth_refused(_401())

    assert isinstance(result, ConfigEntryAuthFailed), (
        "UpdateFailed leaves auth_failed False in Home Assistant's coordinator, "
        "so _schedule_refresh() runs and the polls that hold the lock continue"
    )


# --- the count is the host's, not the entry's -------------------------------


def test_many_channels_refused_once_each_is_one_bad_moment():
    """The count is per source, so a recorder having a moment is not a wrong password.

    Measured on a live twelve channel NVR: a burst of motion, one transient refusal,
    and twelve channels incremented a single counter inside fourteen milliseconds.
    Home Assistant then demanded a new password for credentials that were correct
    throughout, and a probe with the stored password answered 200 straight afterwards.

    Counting requests made that inevitable: any threshold below the channel count is
    reached by one instant.
    """
    _clean()
    channels = [_coordinator(entry_id="e%d" % i) for i in range(12)]

    results = [c._auth_refused(_401()) for c in channels]

    assert all(
        isinstance(r, UpdateFailed) and not isinstance(r, ConfigEntryAuthFailed)
        for r in results
    ), "one moment was read as a wrong password"


def test_one_channel_refused_to_the_budget_still_stops():
    """A password that is wrong is wrong every time, and nothing succeeded in between."""
    _clean()
    c = _coordinator()

    results = [c._auth_refused(_401()) for _ in range(MAX_AUTH_REFUSALS)]

    assert isinstance(results[-1], ConfigEntryAuthFailed)
    assert all(not isinstance(r, ConfigEntryAuthFailed) for r in results[:-1])


def test_once_the_budget_is_spent_every_channel_stops():
    """The property the per-host count exists for, and the reason it stays per host.

    The lock is per source IP, so ten channels are ten pollers renewing one lock. If
    the budget were per entry, the first to give up would stop while the rest kept the
    lock alive, and the correct password typed into the reauth dialog would be refused
    along with everything else. That is #729.
    """
    _clean()
    first = _coordinator(entry_id="e1")
    for _ in range(MAX_AUTH_REFUSALS):
        first._auth_refused(_401())

    second = _coordinator(entry_id="e2")

    assert isinstance(
        second._auth_refused(_401()), ConfigEntryAuthFailed
    ), "another channel kept polling after the host's budget was spent"


def test_a_different_host_keeps_its_own_count():
    _clean()
    mine = _coordinator(address="10.0.0.5")
    theirs = _coordinator(address="10.0.0.9")

    for _ in range(MAX_AUTH_REFUSALS):
        mine._auth_refused(_401())
    result = theirs._auth_refused(_401())

    assert isinstance(result, UpdateFailed)
    assert not isinstance(result, ConfigEntryAuthFailed)


# --- the event stream stops too ---------------------------------------------


ADDRESS = "10.0.0.5"


def _client(username="admin", password="right", port=80):
    """What `credentials_identity` reads off a DahuaClient, and nothing else."""
    return SimpleNamespace(
        _device="%s:%s" % (ADDRESS, port), _username=username, _password=password
    )


def _let_in(client):
    """The device answers 2xx to a request this client signed, and the poll
    that made it finishes. A client that names nothing, or None, is only the
    poll finishing."""
    note_credentials_accepted(client)
    async_record_host_success(None, ADDRESS)


def _spend_the_budget():
    for _ in range(MAX_AUTH_REFUSALS):
        async_record_host_auth_refusal(ADDRESS, "a channel")


def _stream(after_recheck, client=None, refuse_first_attach=True):
    """A stream, the attaches it makes, and each look it takes while waiting.

    The first attach is refused with a 401 after the host's budget has been
    spent, which is how it happens: the polls spend it while the stream is up,
    and the stream finds out when its own socket is next refused. Every later
    attach ends the run with CancelledError, which is what an unload delivers.

    `after_recheck(n)` runs each time the waiting stream looks at whether it
    may go on, n counting from 1. The recheck interval is zero, and a wait
    that never ends is cancelled at the hundredth look.
    """
    import asyncio

    stream = object.__new__(DahuaHostEventStream)
    stream._address = ADDRESS
    stream._events = frozenset({"VideoMotion"})
    stream._received_data = False
    stream._failing = False
    stream._consecutive_failures = 7
    stream._by_channel = {}
    stream.attaches = []
    stream.calls = []
    stream.checks = []

    async def _reconcile_latched():
        stream.calls.append("reconcile latched events")

    async def _reconcile_alarms():
        stream.calls.append("reconcile alarm inputs")

    stream._async_reconcile_latched_events = _reconcile_latched
    stream._async_reconcile_alarm_inputs = _reconcile_alarms

    async def _attach(*_args, **_kwargs):
        stream.calls.append("attach")
        stream.attaches.append(
            (
                stream._consecutive_failures,
                stream._waiting_for_credentials,
                stream._owner.client._username,
            )
        )
        if refuse_first_attach and len(stream.attaches) == 1:
            _spend_the_budget()
            raise _401()
        raise asyncio.CancelledError()

    stream.attach = _attach
    stream.owned_by = lambda owner_client: _own(stream, owner_client)
    stream.owned_by(client or _client())

    real = stream._owner_has_been_let_in

    def _counting():
        stream.checks.append(stream._waiting_for_credentials)
        if len(stream.checks) >= 100:
            raise asyncio.CancelledError()
        after_recheck(len(stream.checks))
        return real()

    stream._owner_has_been_let_in = _counting
    return stream


def _own(stream, client):
    client.stream_events = stream.attach
    stream._owner = SimpleNamespace(client=client)
    return client


@pytest.fixture
def no_wait(monkeypatch):
    import custom_components.dahua.host as host

    _clean()
    monkeypatch.setattr(host, "EVENT_STREAM_AUTH_RECHECK_SECONDS", 0)
    yield
    _clean()


async def _run(stream):
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        await stream._async_run()


def _never(_n):
    return None


async def test_the_event_stream_sends_nothing_while_the_budget_is_spent(no_wait):
    """#729: it backs off to ten minutes at most, well inside the half hour lock.

    The budget is spent here by a channel's polls, which is how it happens: the polls
    run every interval and the stream only notices when its socket next fails. The
    count is per source and shared per host, so a stream stops because of refusals it
    never saw itself. That is the whole point of keeping it per host.

    It waits rather than ending (#1235), and while it waits it attaches nothing.
    """
    stream = _stream(_never)

    await _run(stream)

    assert len(stream.attaches) == 1, "attached again while the budget was spent"
    # Nor reads anything else: the reconciles are requests too.
    assert stream.calls == ["attach"]
    assert len(stream.checks) == 100
    assert all(stream.checks), "not marked as waiting while it waited"
    assert stream._waiting_for_credentials is False


async def test_the_event_stream_resumes_once_its_own_poll_is_let_in(no_wait):
    """#1235: a DH-C4K-P with a full user table answered 401 with no challenge.

    The stream counted three of those and ended for good. The credentials were
    right, so no reauth was raised; the camera recovered and its polls
    succeeded, and the stream stayed gone, more than a day of it with the
    camera answering every poll, until the integration was reloaded. The
    owner's poll succeeding is the evidence, and the stream must come back on
    it.
    """

    def _the_owners_poll_succeeds_on_the_third_look(n):
        if n == 3:
            _let_in(stream._owner.client)

    stream = _stream(_the_owners_poll_succeeds_on_the_third_look)

    await _run(stream)

    assert len(stream.attaches) == 2, "did not attach again once its poll was let in"
    # Not reset by the wait: refused again on that attach, it goes straight to
    # the longest backoff instead of every ten seconds into a full device.
    assert stream.attaches[1] == (7, False, "admin")
    assert stream._refused_since is None
    # A Stop lost while the host refused us left its sensor on; the reads that
    # close it run once the host lets us in, before the stream attaches again.
    assert stream.calls == [
        "attach",
        "reconcile latched events",
        "reconcile alarm inputs",
        "attach",
    ]


# --- whose poll it was (rroller's review of #1241) -----------------------------


@pytest.mark.parametrize(
    "another",
    [
        pytest.param(lambda: _client(username="viewer"), id="another account"),
        pytest.param(lambda: _client(password="old"), id="another password"),
        pytest.param(lambda: _client(port=81), id="another device on the address"),
        pytest.param(lambda: SimpleNamespace(), id="a client that does not say"),
        pytest.param(lambda: None, id="no client named"),
    ],
)
async def test_another_entrys_poll_on_the_address_is_not_evidence(no_wait, another):
    """The same-address, different-account control. The other entry's poll
    clears the host's count, and the stream attached again with the account
    the device was still refusing: the lockout #729 is about, renewed."""

    def _the_other_entry_polls(n):
        if n in (3, 20, 60):
            _let_in(another())

    stream = _stream(_the_other_entry_polls)

    await _run(stream)

    assert not dahua.host.host_auth_budget_exhausted(
        ADDRESS
    ), "the control only means something if the host's count was cleared"
    assert len(stream.attaches) == 1, "offered the refused account again"
    assert stream.calls == ["attach"]
    assert stream._refused_since is not None


async def test_the_owners_poll_after_another_entrys_is_evidence(no_wait):
    def _polls(n):
        if n == 3:
            _let_in(_client(username="viewer"))
        if n == 9:
            _let_in(_client())

    stream = _stream(_polls)

    await _run(stream)

    assert len(stream.checks) == 9
    assert stream.attaches[1] == (7, False, "admin")


async def test_a_poll_that_succeeded_before_the_refusal_is_not_evidence(no_wait):
    """The account worked once; that is how the stream was up at all. The
    host's count is cleared by another entry here, so only the order of the
    owner's poll and the refusal is holding the stream back."""
    client = _client()
    _let_in(client)

    def _another_entry_polls(n):
        if n == 3:
            _let_in(_client(username="viewer"))

    stream = _stream(_another_entry_polls, client=client)

    await _run(stream)

    assert not dahua.host.host_auth_budget_exhausted(ADDRESS)
    assert len(stream.attaches) == 1


async def test_the_hosts_budget_still_has_to_be_clear(no_wait):
    """Host wide, as before: the owner was let in, and then the host began
    refusing again. Nothing on it offers credentials while that holds."""

    def _let_in_and_refused_again(n):
        if n == 3:
            _let_in(stream._owner.client)
            _spend_the_budget()

    stream = _stream(_let_in_and_refused_again)

    await _run(stream)

    assert len(stream.attaches) == 1


async def test_an_owner_replaced_by_a_verified_one_resumes_with_it(no_wait):
    """The owner's entry is unloaded and the stream passes to another entry's
    client. That one is held to the same test: a poll of its own."""
    viewer = _client(username="viewer")

    def _handed_over_then_verified(n):
        if n == 3:
            stream.owned_by(viewer)
        if n == 6:
            _let_in(viewer)

    stream = _stream(_handed_over_then_verified)

    await _run(stream)

    assert len(stream.checks) == 6
    assert stream.attaches[1] == (7, False, "viewer")


async def test_an_owner_replaced_by_an_unverified_one_keeps_waiting(no_wait):
    """The new owner's poll succeeded before the wait, and the old owner's
    succeeds during it. Neither is the new owner being let in since."""
    viewer = _client(username="viewer")
    _let_in(viewer)

    def _handed_over(n):
        if n == 3:
            stream.owned_by(viewer)
        if n == 6:
            _let_in(_client())

    stream = _stream(_handed_over)

    await _run(stream)

    assert len(stream.attaches) == 1


async def test_a_restarted_task_does_not_offer_what_the_old_one_stopped_offering(
    no_wait,
):
    """The task is cancelled and started again whenever the owner or the event
    list changes. The host's count has been cleared by another entry by then,
    so only the stream remembers its account was refused."""
    import asyncio

    def _cancelled_on_the_fifth_look(n):
        if n == 5:
            raise asyncio.CancelledError()

    stream = _stream(_cancelled_on_the_fifth_look)
    await _run(stream)
    assert stream._refused_since is not None
    _let_in(_client(username="viewer"))
    assert not dahua.host.host_auth_budget_exhausted(ADDRESS)

    await _run(stream)

    assert len(stream.attaches) == 1, "the new task attached with the refused account"
    assert stream.calls == ["attach"]


async def test_a_restarted_task_resumes_on_its_owners_poll(no_wait):
    import asyncio

    def _cancelled_then_let_in(n):
        if n == 5:
            raise asyncio.CancelledError()
        if n == 8:
            _let_in(stream._owner.client)

    stream = _stream(_cancelled_then_let_in)
    await _run(stream)

    await _run(stream)

    assert stream.calls == [
        "attach",
        "reconcile latched events",
        "reconcile alarm inputs",
        "attach",
    ]
    assert stream._refused_since is None


async def test_the_owners_poll_between_two_tasks_still_counts(no_wait):
    """The refusal is dated from when it happened, not from when the newest
    task began to wait, so a poll that succeeded in the gap is not lost."""
    import asyncio

    def _cancelled_on_the_fifth_look(n):
        if n == 5:
            raise asyncio.CancelledError()

    stream = _stream(_cancelled_on_the_fifth_look)
    await _run(stream)
    _let_in(stream._owner.client)

    await _run(stream)

    assert len(stream.checks) == 6, "let in at the new task's first look"
    assert len(stream.attaches) == 2


async def test_a_stream_started_on_a_host_whose_budget_is_spent_waits_first(no_wait):
    """The reload after a reauth starts the stream before its first poll has
    finished. It attaches once that poll has, and not before."""
    _spend_the_budget()

    def _first_poll_finishes(n):
        if n == 4:
            _let_in(stream._owner.client)

    stream = _stream(_first_poll_finishes, refuse_first_attach=False)

    await _run(stream)

    assert len(stream.checks) == 4
    assert stream.calls == [
        "reconcile latched events",
        "reconcile alarm inputs",
        "attach",
    ]


async def test_a_stream_started_on_a_host_in_good_standing_attaches_at_once(no_wait):
    stream = _stream(_never, refuse_first_attach=False)

    await _run(stream)

    assert stream.checks == []
    assert stream.calls == ["attach"]


async def test_stopping_the_stream_forgets_the_refusal(no_wait):
    stream = _stream(_never)
    await _run(stream)
    stream._task = None
    stream._video_motion_poll_task = None

    await stream.async_stop()

    assert stream._refused_since is None


def test_what_identifies_an_account():
    from custom_components.dahua.client import credentials_identity

    one = credentials_identity(_client())
    assert one == credentials_identity(_client())
    assert one != credentials_identity(_client(username="viewer"))
    assert one != credentials_identity(_client(password="old"))
    assert one != credentials_identity(_client(port=81))
    assert "right" not in repr(one), "the password itself is not kept"
    assert credentials_identity(None) is None
    assert credentials_identity(SimpleNamespace(_device="d", _username="u")) is None


async def test_the_owners_poll_finishing_is_not_evidence(no_wait):
    """rroller's second review of #1241. A coordinator with nothing due, or
    whose reads were all answered from the cache, finishes a poll without
    sending the device anything. That clears the host's count and proves
    nothing about the account, and it used to resume the stream."""

    def _a_poll_finishes_having_asked_nothing(n):
        if n in (3, 20, 60):
            async_record_host_success(None, ADDRESS)

    stream = _stream(_a_poll_finishes_having_asked_nothing)

    await _run(stream)

    assert not dahua.host.host_auth_budget_exhausted(ADDRESS)
    assert len(stream.attaches) == 1
    assert stream.calls == ["attach"]


def test_a_finished_poll_records_nothing_about_an_account():
    _clean()
    mark = client_module.credentials_acceptance_mark()

    async_record_host_success(None, ADDRESS)

    assert client_module.credentials_acceptance_mark() == mark
    assert client_module._CREDENTIALS_ACCEPTED == {}


def test_only_an_authenticated_exchange_records_an_account_as_accepted():
    """The poll's last line names the host and nothing else. The one caller
    of `note_credentials_accepted` is the constructor every signed request
    goes through, and the RPC2 session records through `_note_accepted` in
    two places: its login, and an answer on it."""
    from .integration_source import source

    text = source()
    assert "async_record_host_success(self.hass, self._address)\n" in text
    assert text.count("note_credentials_accepted(") == 2, "its definition and one call"
    assert text.count("_note_accepted(") == 4, "its definition and three calls"
    assert (
        text.count("DigestAuth(") == 1
    ), "a second place that builds one is a signed request nothing hears about"


async def test_the_stream_keeps_trying_while_the_budget_holds():
    """One refusal must not take the stream down, for the same reason as #714."""
    _clean()
    stream = object.__new__(DahuaHostEventStream)
    stream._address = "10.0.0.5"
    stream._received_data = False
    stream._failing = False
    stream._consecutive_failures = 0

    assert async_record_host_auth_refusal(stream._address) < MAX_AUTH_REFUSALS
