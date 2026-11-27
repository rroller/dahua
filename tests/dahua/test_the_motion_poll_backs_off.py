"""The motion poll slows down when it fails, and stops for refused credentials (#1182).

`poll_video_motion` asks a recorder for its motion state every two seconds. It
asked again two seconds later whatever the answer had been, for as long as the
option was on: thirty requests a minute against a device that was not
answering, and thirty refused logins a minute against one whose password had
been changed, after the polls and the event stream had both stopped offering
it. That last one is the lockout #729 is about.

These run the real loop. Time is a recorded list of the sleeps it asked for,
so nothing here waits and the delays are asserted exactly.
"""

import asyncio
import json
import pathlib
from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError, RequestInfo
from multidict import CIMultiDict
from yarl import URL

import custom_components.dahua as dahua
from custom_components.dahua import client as client_module
from custom_components.dahua import host as host_module
from custom_components.dahua.client import note_credentials_accepted
from custom_components.dahua.host import (
    MAX_AUTH_REFUSALS,
    VIDEO_MOTION_POLL_MAX_RETRY_SECONDS,
    VIDEO_MOTION_POLL_SECONDS,
    DahuaHostEventStream,
    async_record_host_auth_refusal,
    async_record_host_success,
)

ADDRESS = "10.0.0.9"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(dahua.ir, "async_delete_issue", lambda *a, **k: None)
    dahua._HOST_FAILURES.clear()
    client_module._CREDENTIALS_ACCEPTED.clear()
    client_module._CREDENTIALS_REFUSED.clear()
    yield
    dahua._HOST_FAILURES.clear()
    client_module._CREDENTIALS_ACCEPTED.clear()
    client_module._CREDENTIALS_REFUSED.clear()


def _answered(status):
    """A refusal the way aiohttp raises one, request details included: the
    loop logs it, and one built without them cannot be turned into text."""
    url = URL("http://%s/cgi-bin/eventManager.cgi" % ADDRESS)
    return ClientResponseError(
        RequestInfo(url, "GET", CIMultiDict(), url), (), status=status
    )


def _refused():
    return _answered(401)


class _Camera:
    """A device whose answers are scripted, one per request.

    An exception is raised, a set is the channels in motion, and a callable
    is run first (to change the world mid-poll) and its result used.
    The script running out ends the run the way an unload does.
    """

    def __init__(self, script, username="admin", password="right"):
        self.script = list(script)
        self.asked = 0
        self._device = "%s:80" % ADDRESS
        self._username = username
        self._password = password

    async def async_get_event_indexes_cgi(self, code):
        assert code == "VideoMotion"
        if not self.script:
            raise asyncio.CancelledError()
        self.asked += 1
        answer = self.script.pop(0)
        if callable(answer):
            answer = answer()
        if isinstance(answer, BaseException):
            raise answer
        return set(answer)


class _Run:
    """The real poll loop over a scripted camera, with the sleeps recorded."""

    def __init__(self, monkeypatch, script, **camera):
        self.camera = _Camera(script, **camera)
        self.sleeps = []
        self.events = []
        self.on_sleep = None
        self.stream = object.__new__(DahuaHostEventStream)
        self.stream._address = ADDRESS
        self.stream._owner = SimpleNamespace(client=self.camera)
        self.stream._dispatch_events = lambda data, video_motion_source=None: (
            self.events.append(data.decode().strip().split(";", 1)[1])
        )

        really_sleep = asyncio.sleep

        async def _sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) > 500:
                raise asyncio.CancelledError()
            if self.on_sleep is not None:
                self.on_sleep(len(self.sleeps))
            await really_sleep(0)

        monkeypatch.setattr(host_module.asyncio, "sleep", _sleep, raising=True)
        # The loop subtracts how long the request took. Nothing takes any time
        # here, so a frozen clock makes each sleep exactly the interval chosen.
        monkeypatch.setattr(host_module.time, "monotonic", lambda: 1000.0)

    async def go(self):
        with pytest.raises(asyncio.CancelledError):
            await self.stream._async_poll_video_motion()
        return self


def _spend_the_budget(source="a channel"):
    for _ in range(MAX_AUTH_REFUSALS):
        async_record_host_auth_refusal(ADDRESS, source)


def _let_in(camera):
    note_credentials_accepted(camera)
    async_record_host_success(None, ADDRESS)


RECHECK = host_module.EVENT_STREAM_AUTH_RECHECK_SECONDS


# --- a poll that keeps answering is unchanged -------------------------------------------


async def test_a_camera_that_answers_is_asked_every_two_seconds(monkeypatch):
    run = await _Run(monkeypatch, [set(), {2}, {2}, set()]).go()

    assert run.sleeps == [VIDEO_MOTION_POLL_SECONDS] * 4
    assert run.events == ["action=Start;index=2", "action=Stop;index=2"]


# --- one that fails is asked less and less often ----------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ConnectionResetError(),
        _answered(500),
        _refused(),
    ],
)
async def test_each_failure_doubles_the_wait_up_to_a_ceiling(monkeypatch, failure):
    """It was two seconds after every one of them, for as long as it lasted."""
    run = await _Run(monkeypatch, [failure] * 9).go()

    assert run.sleeps == [4, 8, 16, 32, 60, 60, 60, 60, 60]
    assert max(run.sleeps) == VIDEO_MOTION_POLL_MAX_RETRY_SECONDS


async def test_a_minute_of_failures_is_a_handful_of_requests_not_thirty(monkeypatch):
    run = _Run(monkeypatch, [TimeoutError()] * 500)

    def _stop_after_ten_minutes(_n):
        if sum(run.sleeps) >= 600:
            raise asyncio.CancelledError()

    run.on_sleep = _stop_after_ten_minutes
    await run.go()

    assert run.camera.asked <= 14, "about one a minute once it has backed off"
    assert 600 / VIDEO_MOTION_POLL_SECONDS == 300, "what it used to send"


async def test_the_first_answer_puts_it_back_to_two_seconds(monkeypatch):
    run = await _Run(
        monkeypatch, [TimeoutError(), TimeoutError(), set(), set(), TimeoutError()]
    ).go()

    assert run.sleeps == [4, 8, 2, 2, 4]


async def test_motion_seen_before_a_failure_still_gets_its_stop_afterwards(
    monkeypatch,
):
    """Backing off must not lose the edge: what was active is kept through
    the failures and compared with the first answer after them."""
    run = await _Run(monkeypatch, [{3}, TimeoutError(), TimeoutError(), set()]).go()

    assert run.events == ["action=Start;index=3", "action=Stop;index=3"]


async def test_a_wait_that_has_lasted_a_week_is_still_a_minute(monkeypatch):
    """The doubling is capped before it is computed."""
    run = await _Run(monkeypatch, [TimeoutError()] * 400).go()

    assert run.sleeps[-1] == VIDEO_MOTION_POLL_MAX_RETRY_SECONDS
    assert set(run.sleeps[5:]) == {VIDEO_MOTION_POLL_MAX_RETRY_SECONDS}


# --- and one whose account is refused is not asked at all -------------------------------


async def test_nothing_is_sent_while_the_hosts_budget_is_spent(monkeypatch):
    """The polls reached the budget and stopped. This loop was the one thing
    left offering the refused password."""
    _spend_the_budget()
    run = await _Run(monkeypatch, [set()] * 5).go()

    assert run.camera.asked == 0
    assert set(run.sleeps) == {RECHECK}, "it only looked, every few seconds"


async def test_it_starts_again_once_its_own_account_is_let_in(monkeypatch):
    _spend_the_budget()
    run = _Run(monkeypatch, [{1}, {1}])

    def _the_owners_poll_succeeds(n):
        if n == 4:
            _let_in(run.camera)

    run.on_sleep = _the_owners_poll_succeeds
    await run.go()

    assert run.camera.asked == 2
    assert run.sleeps[:4] == [RECHECK] * 4
    assert run.sleeps[4:] == [VIDEO_MOTION_POLL_SECONDS] * 2
    assert run.events == ["action=Start;index=1"]


async def test_another_accounts_success_on_the_address_does_not_start_it(monkeypatch):
    """Two entries on one recorder with two accounts. The other one's poll
    clears the host's count and says nothing about this one's password."""
    _spend_the_budget()
    run = _Run(monkeypatch, [set()] * 3)
    other = _Camera([], username="viewer")

    def _the_other_entry_polls(n):
        if n == 4:
            _let_in(other)

    run.on_sleep = _the_other_entry_polls
    await run.go()

    assert run.camera.asked == 0


async def test_the_budget_being_spent_mid_run_stops_it(monkeypatch):
    """The usual order: polling works, the password is changed on the device,
    this loop is refused and backs off, the entry's polls reach the budget."""
    run = _Run(monkeypatch, [set(), _refused(), _refused(), _refused(), set()])

    def _the_polls_reach_the_budget(n):
        if n == 3:
            _spend_the_budget("the entry")

    run.on_sleep = _the_polls_reach_the_budget
    await run.go()

    assert run.camera.asked == 3, "one answer and two refusals, then nothing"
    assert run.sleeps[:3] == [2, 4, 8]
    assert set(run.sleeps[3:]) == {RECHECK}


async def test_after_the_pause_it_is_back_at_two_seconds(monkeypatch):
    _spend_the_budget()
    run = _Run(monkeypatch, [set(), set()])
    run.on_sleep = lambda n: _let_in(run.camera) if n == 2 else None
    await run.go()

    assert run.sleeps == [RECHECK, RECHECK, 2, 2]


async def test_an_acceptance_from_before_the_pause_does_not_end_it(monkeypatch):
    note_credentials_accepted(_Camera([]))
    _spend_the_budget()
    run = _Run(monkeypatch, [set()])
    run.on_sleep = lambda n: (
        async_record_host_success(None, ADDRESS) if n == 3 else None
    )
    await run.go()

    assert run.camera.asked == 0, "the count cleared, and nothing let it in"


async def test_being_let_in_is_not_enough_while_the_budget_is_still_spent(
    monkeypatch,
):
    """Both halves, as for the event stream: something else on this host is
    still being refused, and that is a lock this loop must not renew."""
    _spend_the_budget()
    run = _Run(monkeypatch, [set()] * 3)
    run.on_sleep = lambda n: note_credentials_accepted(run.camera) if n == 3 else None
    await run.go()

    assert run.camera.asked == 0


async def test_a_failure_straight_after_the_pause_starts_the_doubling_again(
    monkeypatch,
):
    """Two refusals before the pause had the wait at eight seconds. Coming
    back, the first failure is the first failure."""
    run = _Run(monkeypatch, [_refused(), _refused(), TimeoutError(), set()])

    def _budget_then_let_in(n):
        if n == 2:
            _spend_the_budget("the entry")
        if n == 4:
            _let_in(run.camera)

    run.on_sleep = _budget_then_let_in
    await run.go()

    assert run.sleeps == [4, 8, RECHECK, RECHECK, 4, 2]


# --- it obeys the budget and never spends it --------------------------------------------


async def test_being_refused_does_not_count_towards_asking_for_new_credentials(
    monkeypatch,
):
    """A recorder briefly refusing a two second poll is not evidence that the
    password is wrong, and the budget is what raises the reauth card."""
    await _Run(monkeypatch, [_refused()] * 10).go()

    assert dahua._HOST_FAILURES == {}
    assert not host_module.host_auth_budget_exhausted(ADDRESS)


# --- what the option says about itself --------------------------------------------------


@pytest.mark.parametrize("language, word", [("en", "seconds"), ("es", "segundos")])
def test_the_option_names_how_often_it_asks(language, word):
    """It read "one request per recorder", which is true of one poll and says
    nothing about there being 43,200 of them a day."""
    strings = json.loads(
        (
            pathlib.Path(host_module.__file__).parent
            / "translations"
            / ("%s.json" % language)
        ).read_text(encoding="utf-8")
    )
    text = json.dumps(strings, ensure_ascii=False)
    start = text.index('"poll_video_motion": "')
    label = text[start : text.index('"', start + 22 + 1)]

    assert "%d %s" % (VIDEO_MOTION_POLL_SECONDS, word) in label
