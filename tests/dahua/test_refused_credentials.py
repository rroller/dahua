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
from aiohttp import ClientResponseError

import custom_components.dahua as dahua
from custom_components.dahua import (
    DahuaDataUpdateCoordinator,
    DahuaHostEventStream,
    MAX_AUTH_REFUSALS,
    async_record_host_auth_refusal,
    async_record_host_success,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed


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

    assert isinstance(c._auth_refused(_401()), UpdateFailed), \
        "the count survived a success, so transient 401s accumulate for ever"


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

def test_channels_of_one_nvr_share_the_count():
    """The lock is per source IP, so per-entry counts would never stop it.

    Ten channels are ten entries hammering one device. If each counted alone,
    the first to give up would stop while the other nine kept the lock alive.
    """
    _clean()
    entries = [_coordinator(entry_id="e%d" % i) for i in range(MAX_AUTH_REFUSALS)]

    results = [e._auth_refused(_401()) for e in entries]

    assert isinstance(results[-1], ConfigEntryAuthFailed)
    assert all(isinstance(r, UpdateFailed) and not isinstance(r, ConfigEntryAuthFailed)
               for r in results[:-1])


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

async def test_the_event_stream_stops_once_the_budget_is_gone():
    """It backs off to ten minutes at most, well inside the half hour lock."""
    _clean()
    for _ in range(MAX_AUTH_REFUSALS - 1):
        async_record_host_auth_refusal("10.0.0.5")

    stream = object.__new__(DahuaHostEventStream)
    stream._address = "10.0.0.5"
    stream._events = frozenset({"VideoMotion"})
    stream._received_data = False
    stream._failing = False
    stream._consecutive_failures = 0

    async def _refuse(*_args, **_kwargs):
        raise _401()

    stream._owner = type("O", (), {"client": type("C", (), {
        "stream_events": staticmethod(_refuse)})()})()

    # Returns rather than sleeping and re-attaching. If it looped, this hangs.
    await stream._async_run()


async def test_the_stream_keeps_trying_while_the_budget_holds():
    """One refusal must not take the stream down, for the same reason as #714."""
    _clean()
    stream = object.__new__(DahuaHostEventStream)
    stream._address = "10.0.0.5"
    stream._received_data = False
    stream._failing = False
    stream._consecutive_failures = 0

    assert async_record_host_auth_refusal(stream._address) < MAX_AUTH_REFUSALS
