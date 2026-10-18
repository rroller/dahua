"""Pushing a clip to a device's speaker (#177).

`audio.cgi?action=postAudio&httptype=singlepart` is a stream, not an upload.
Measured on a VTO2000A at 192.168.0.232, firmware 3.120.0000.0.R:

    thiscgidoesnotexist.cgi?action=getCollect   400 Bad Request!   0.15s
    audio.cgi?action=nonsenseAction             400 Bad Request!   0.26s
    audio.cgi?action=postAudio...               held open, no status line at all

Four bodies were sent to it -- raw and multipart, 8 KB and 40 KB, and one
straight after another -- and every one was taken in silence. All four went
unauthenticated, so nothing there says what a device that does challenge would
do. What it does say is that there is no status code to read success from, and
that finishing the body is the only thing marking the end of a clip.

It does play what it takes, which the socket settles. With the client's send
buffer pinned to 8 KB, the device accepted 194,000 bytes in 6.1s. The kernel
could not have held that much and a device discarding it would have taken it
at LAN speed, so it was consuming in something like real time.

How fast is a separate question, and it is not answered. The 6.1s average is
31,683 bytes a second, but most of that is the device's buffer filling: the
second half of the same push settled at about 21,000. The `getAudio` side of
the same run reads `Audio/PCM` at 32,000 B/s, which is 16-bit PCM at 16 kHz,
but that is the microphone and not the speaker, and every published recipe
pushes A-law at 8 kHz, which is 8,000. The 21,000 sits between them and nearer
the recipes. Treat the speaker's own format as unmeasured.

What follows, and what this file exists to hold in place.

* **The length must be declared.** aiohttp sends a body of unknown size with
  `Transfer-Encoding: chunked`, and a 2018 embedded server will not de-chunk
  it, so the chunk-length lines would be played as audio.
* **The clip should be paced.** This device paces it anyway by backpressure,
  but one with a larger buffer would not, and #177's @elkarto hearing only
  the last two or three seconds of a long message is what that sounds like.
* **The rate is the caller's to say.** The two candidates are a factor of
  four apart, so a built-in constant would play one device's audio at four
  times or a quarter of its speed.
* **Being wrong about it must not abandon the push.** A clip given up on part
  way through is the shape a VTO2000A was found hung after, so a device
  draining slower than it was told gets the whole clip, and only one that has
  stopped is cut off.
* **Nothing the device sends back can be taken at face value.** It answers
  nothing when it is happy, a refused write can arrive as 200 with the body
  `Error`, and an answer that comes before the last byte means the rest of
  the clip never went.
* **The cleanup must not eat a cancellation**, or an enclosing
  `asyncio.timeout` silently stops firing.
"""

import asyncio
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient


def _client(session, address="192.168.1.50", port=80):
    return DahuaClient("admin", "secret", address, port, 554, session)


@pytest.fixture(autouse=True)
def _forget_shared_device_state():
    """Both of these are module level and keyed per device, not per test.

    The digest one bit: these tests all build a client for the same address,
    so the challenge seeded by the test that checks the header was still
    there for the test that checks no header is invented, which then passed
    or failed depending on the order they ran in.
    """
    client_module._HOST_SPEAKER_LOCKS.clear()
    client_module._HOST_DIGEST_STATE.clear()
    yield
    client_module._HOST_SPEAKER_LOCKS.clear()
    client_module._HOST_DIGEST_STATE.clear()


class _RecordingSession:
    """Collects what the request was, and drains the body as the device would.

    Stands in for aiohttp only where the question is what this method asked
    for. The question of what aiohttp then puts on the wire is a different one
    and is asked against a real server below.
    """

    def __init__(self, status=200):
        self.status = status
        self.headers = None
        self.url = None
        self.body = b""
        self.chunks = []
        self.session = self

    def post(self, url, headers=None, data=None, timeout=None):
        self.url = url
        self.headers = dict(headers or {})
        return _RecordingRequest(self, data)


class _RecordingRequest:
    def __init__(self, session, data):
        self._session = session
        self._data = data

    async def __aenter__(self):
        async for piece in self._data:
            self._session.chunks.append(piece)
            self._session.body += piece
        return _RecordingResponse(self._session.status)

    async def __aexit__(self, *exc):
        return False


class _RecordingResponse:
    def __init__(self, status, text=""):
        self.status = status
        self._text = text

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                None, (), status=self.status, message="Bad Request"
            )

    async def text(self):
        return self._text


async def test_the_whole_clip_arrives_unchanged():
    session = _RecordingSession()
    audio = bytes(range(256)) * 40  # 10240 bytes

    await _client(session).async_play_audio(audio, "Audio/PCM", 32000)

    assert session.body == audio


async def test_the_length_is_declared_so_aiohttp_cannot_choose_chunked():
    """The one header that stops the chunk markers being played as sound."""
    session = _RecordingSession()
    audio = b"\x00" * 4000

    await _client(session).async_play_audio(audio, "Audio/PCM", 32000)

    assert session.headers["Content-Length"] == "4000"
    assert session.headers["Content-Type"] == "Audio/PCM"


async def test_the_caller_names_the_codec():
    """A device that wants A-law gets A-law, not whatever a constant said."""
    session = _RecordingSession()

    await _client(session).async_play_audio(b"\xd5" * 800, "Audio/G.711A", 8000)

    assert session.headers["Content-Type"] == "Audio/G.711A"


async def test_the_url_names_postaudio_and_the_channel():
    session = _RecordingSession()

    await _client(session).async_play_audio(
        b"\x00" * 100, "Audio/PCM", 32000, channel_number=2
    )

    # Exact, because a substring match passes for the wrong scheme, host or
    # port. The channel on the wire is the device's own number rather than the
    # 0-based index the coordinator holds, which is the distinction #724 exists
    # for and the one a first caller is most likely to get wrong.
    assert session.url == (
        "http://192.168.1.50:80/cgi-bin/audio.cgi"
        "?action=postAudio&httptype=singlepart&channel=2"
    )


async def test_the_clip_is_fed_at_the_rate_it_plays_at(monkeypatch):
    """Counted against a fake clock, never timed against the real one.

    The clock has to move with the sleeps or the measurement is nonsense: the
    pacing is written against a deadline per byte rather than a fixed nap, so
    with a clock stuck at zero it correctly asks to wait 0.1s, then 0.2s, then
    0.3s, and a test that summed those would read 82 seconds for a four second
    clip. That is the pacing working, not failing. Advancing the clock by what
    was slept is what turns the request into the thing worth asserting.
    """
    waits = []
    now = [1000.0]

    async def fake_sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now[0])
    session = _RecordingSession()
    # Four seconds of PCM at 16 kHz.
    audio = b"\x00" * (32000 * 4)

    await _client(session).async_play_audio(audio, "Audio/PCM", 32000)

    # Ten writes a second, so four seconds is forty of them.
    assert len(session.chunks) == 40
    assert {len(chunk) for chunk in session.chunks} == {3200}
    # One tenth of a second between each. Thirty-nine waits and not forty:
    # there is nothing after the last piece to hold back, so pacing it would
    # only delay the close.
    assert len(waits) == 39
    assert {round(wait, 6) for wait in waits} == {0.1}
    assert sum(waits) == pytest.approx(3.9)


async def test_pacing_catches_up_rather_than_stretching_the_clip(monkeypatch):
    """A write that blocks must not push everything after it later.

    The deadline is measured from the start of the clip, so time lost to one
    slow write is taken out of the next wait instead of being added to the
    total. Paced by a fixed nap per write it would be added, and a clip would
    drift longer every time the network hesitated.
    """
    waits = []
    now = [500.0]

    async def fake_sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now[0])

    class _StallingSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _StallingRequest(self, data)

    class _StallingRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                self._session.chunks.append(piece)
                if len(self._session.chunks) == 1:
                    now[0] += 0.25  # the first write took far too long
            return _RecordingResponse(200)

    session = _StallingSession()
    await _client(session).async_play_audio(b"\x00" * 32000, "Audio/PCM", 32000)

    # One second of audio in ten writes. The 0.25s stall swallows the first
    # two slots whole, so those ask for no wait at all rather than for a
    # negative one, and the clip still finishes at one second instead of
    # 1.25s.
    assert len(session.chunks) == 10
    assert len(waits) == 7
    assert sum(waits) == pytest.approx(0.65)
    # Back on schedule by the end: 0.9s of clock for the first nine slots,
    # despite the first one overrunning by 0.15s.
    assert now[0] == pytest.approx(500.9)


async def test_a_clip_shorter_than_one_write_is_still_sent():
    session = _RecordingSession()

    await _client(session).async_play_audio(b"\x00" * 10, "Audio/PCM", 32000)

    assert session.body == b"\x00" * 10


async def test_nothing_is_sent_for_an_empty_clip():
    session = _RecordingSession()

    await _client(session).async_play_audio(b"", "Audio/PCM", 32000)

    assert session.url is None


async def test_a_rate_of_zero_is_refused_rather_than_dividing_by_it():
    session = _RecordingSession()

    with pytest.raises(ValueError):
        await _client(session).async_play_audio(b"\x00" * 10, "Audio/PCM", 0)


async def test_silence_from_the_device_is_success_not_a_timeout(monkeypatch):
    """postAudio answers nothing at all, so nothing must not read as failure."""

    monkeypatch.setattr(client_module, "AUDIO_REFUSAL_GRACE_SECONDS", 0.2)

    class _SilentSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            self.url = url
            self.headers = dict(headers or {})
            return _SilentRequest(self, data)

    class _SilentRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                self._session.body += piece
            # The device takes the clip and says nothing, for ever.
            await asyncio.sleep(3600)

    session = _SilentSession()

    await asyncio.wait_for(
        _client(session).async_play_audio(b"\x00" * 320, "Audio/PCM", 32000),
        timeout=5,
    )

    assert session.body == b"\x00" * 320


async def test_an_answer_before_the_clip_has_finished_is_a_refusal():
    """A device answering mid-clip has not played the rest of it.

    Whatever its status code. A 200 that arrives while the body is still going
    out means the device stopped listening, so reporting success would claim a
    clip that was truncated.
    """
    status_seen = []

    class _EagerSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _EagerRequest(self, data)

    class _EagerRequest(_RecordingRequest):
        async def __aenter__(self):
            # One write of a long clip, then an answer: the rest never goes.
            await self._data.__anext__()
            status_seen.append(200)
            return _RecordingResponse(200)

    session = _EagerSession()
    with pytest.raises(client_module.DahuaWriteDeclined):
        await _client(session).async_play_audio(
            b"\x00" * (32000 * 3), "Audio/PCM", 32000
        )

    assert status_seen == [200], "the premise: it answered 200, not an error"


async def test_a_refusal_that_arrives_after_the_clip_is_still_raised(monkeypatch):
    """The grace period exists for exactly this, and nothing else tested it."""
    monkeypatch.setattr(client_module, "AUDIO_REFUSAL_GRACE_SECONDS", 2.0)

    class _LateSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _LateRequest(self, data)

    class _LateRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                self._session.body += piece
            await asyncio.sleep(0.05)  # after the body, inside the grace
            return _RecordingResponse(400)

    with pytest.raises(aiohttp.ClientResponseError):
        await _client(_LateSession()).async_play_audio(
            b"\x00" * 320, "Audio/PCM", 32000
        )


async def test_a_refusal_that_arrives_after_the_grace_is_let_go(monkeypatch):
    """Past the grace the clip has played, so a late answer changes nothing."""
    monkeypatch.setattr(client_module, "AUDIO_REFUSAL_GRACE_SECONDS", 0.05)

    class _TooLateSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _TooLateRequest(self, data)

    class _TooLateRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                self._session.body += piece
            await asyncio.sleep(2)  # well past the grace
            return _RecordingResponse(400)

    session = _TooLateSession()
    await _client(session).async_play_audio(b"\x00" * 320, "Audio/PCM", 32000)

    assert session.body == b"\x00" * 320


async def test_a_200_carrying_the_word_error_is_a_refusal():
    """This family says no in two voices, and 200 `Error` is the quiet one.

    Every write through `get()` is checked for it. This request does not go
    through `get()`, so a device declining the audio that way would otherwise
    be reported as a clip that played.
    """

    class _DecliningSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _DecliningRequest(self, data)

    class _DecliningRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                self._session.body += piece
            return _RecordingResponse(200, text="Error\r\n")

    with pytest.raises(client_module.DahuaWriteDeclined):
        await _client(_DecliningSession()).async_play_audio(
            b"\x00" * 320, "Audio/PCM", 32000
        )


async def test_the_cached_digest_challenge_is_sent_when_there_is_one():
    """A device that does challenge must not cost the clip to find out.

    The body is generated as it goes, so `DigestAuth.request`'s answer to a
    401 -- send the whole thing again -- would send an empty one. The header
    goes up front instead.
    """
    session = _RecordingSession()
    client = _client(session)
    client._digest_state.update(  # pylint: disable=protected-access
        {
            "scheme": "digest",
            "challenge": {"realm": "Device_CGI", "nonce": "abc123"},
        }
    )

    await client.async_play_audio(b"\x00" * 320, "Audio/PCM", 32000)

    header = session.headers["AUTHORIZATION"]
    assert header.startswith("Digest ")
    assert 'realm="Device_CGI"' in header
    assert 'nonce="abc123"' in header
    assert 'username="admin"' in header


async def test_no_header_is_invented_when_no_challenge_is_known():
    """An unauthenticated attempt beats a made-up one."""
    session = _RecordingSession()

    await _client(session).async_play_audio(b"\x00" * 320, "Audio/PCM", 32000)

    assert "AUTHORIZATION" not in session.headers


async def test_two_announcements_on_one_device_do_not_overlap():
    """One speaker, so one clip at a time, whichever channel asked."""
    order = []

    class _SlowSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _SlowRequest(self, data)

    class _SlowRequest(_RecordingRequest):
        async def __aenter__(self):
            order.append("start")
            async for _ in self._data:
                pass
            order.append("end")
            return _RecordingResponse(200)

    session = _SlowSession()
    first = _client(session)
    second = _client(session)
    assert first._device == second._device  # pylint: disable=protected-access

    await asyncio.gather(
        first.async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000),
        second.async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000),
    )

    assert order == ["start", "end", "start", "end"]


async def test_two_devices_behind_one_address_have_a_speaker_each():
    """Two boxes on one IP, different ports, are two speakers (#1084 again).

    Asserted through `async_play_audio` rather than against `_speaker_lock`,
    because the bug being guarded is the method reaching for the wrong key. A
    test that calls the helper with the right key cannot see that, and an
    earlier draft of this one proved it: keyed on `_address` the method
    silently made one doorbell wait for the other's clip and the test still
    passed.

    The rendezvous is what makes it fail rather than hang: if the two really
    do share a lock, the first never sees the second start, and the wait it is
    holding the lock for times out.
    """
    both_started = asyncio.Event()
    started = []

    class _RendezvousSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _RendezvousRequest(self, data)

    class _RendezvousRequest(_RecordingRequest):
        async def __aenter__(self):
            started.append(len(started))
            if len(started) == 2:
                both_started.set()
            # Serialised, the second clip has not begun and never will.
            await asyncio.wait_for(both_started.wait(), timeout=5)
            async for _ in self._data:
                pass
            return _RecordingResponse(200)

    one = _client(_RendezvousSession(), port=80)
    two = _client(_RendezvousSession(), port=8081)
    assert one._device != two._device  # pylint: disable=protected-access
    assert one._address == two._address  # pylint: disable=protected-access

    await asyncio.gather(
        one.async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000),
        two.async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000),
    )

    assert len(started) == 2, "each doorbell has a speaker of its own"


async def test_aiohttp_really_sends_a_length_and_not_chunked(
    aiohttp_server, socket_enabled
):
    """The claim above, asked of aiohttp itself rather than of a double.

    `Transfer-Encoding: chunked` is the default for a body of unknown size, and
    a generator body has no size, so this is the assertion the Content-Length
    header exists to satisfy. A double cannot answer it: whether aiohttp
    chunks is aiohttp's decision, not this method's.
    """
    seen = {}

    async def handler(request):
        seen["transfer_encoding"] = request.headers.get("Transfer-Encoding")
        seen["content_length"] = request.headers.get("Content-Length")
        seen["content_type"] = request.headers.get("Content-Type")
        seen["body"] = await request.read()
        return web.Response(text="")

    app = web.Application()
    app.router.add_post("/cgi-bin/audio.cgi", handler)
    server = await aiohttp_server(app)

    audio = b"\x01\x02\x03\x04" * 800  # 3200 bytes, one write at 32000 B/s
    async with aiohttp.ClientSession() as session:
        client = _client(session, address=str(server.host), port=server.port)
        await client.async_play_audio(audio, "Audio/PCM", 32000)

    assert seen["transfer_encoding"] is None, "chunk markers would be played as audio"
    assert seen["content_length"] == str(len(audio))
    assert seen["content_type"] == "Audio/PCM"
    assert seen["body"] == audio


async def test_two_channels_of_one_recorder_do_not_queue_behind_each_other():
    """A recorder's channels are different cameras, so different speakers.

    The doorbell this was measured on has one of each, which is exactly why
    the scope has to be chosen rather than noticed: keyed per device, eight
    cameras on one recorder would take turns for no reason. Scope is the
    recurring bug here, so the narrow key is the cheap direction to be wrong
    in.
    """
    both_started = asyncio.Event()
    started = []

    class _RendezvousSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _RendezvousRequest(self, data)

    class _RendezvousRequest(_RecordingRequest):
        async def __aenter__(self):
            started.append(len(started))
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
            async for _ in self._data:
                pass
            return _RecordingResponse(200)

    client = _client(_RendezvousSession())

    await asyncio.gather(
        client.async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000, channel_number=1),
        client.async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000, channel_number=2),
    )

    assert len(started) == 2


async def test_an_announcement_does_not_take_one_of_the_hosts_two_slots():
    """Deliberate, and worth pinning because nothing else would notice.

    `MAX_CONCURRENT_REQUESTS_PER_HOST` is 2 and an announcement lasts as long
    as the clip, so taking a slot would stall every other channel's poll for
    its duration. Wrapping this method in `_request_slot()` passes every other
    test in this file, so without this one the choice is undefended.
    """
    session = _RecordingSession()
    client = _client(session)
    taken = []

    @asynccontextmanager
    async def watched_slot():
        taken.append(1)
        yield

    client._request_slot = watched_slot  # pylint: disable=protected-access

    await client.async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000)

    assert taken == [], "an announcement must not hold one of the two slots"
    assert session.body == b"\x00" * 3200


async def test_the_post_carries_a_clock_of_its_own():
    """Without one the session default applies, which cuts a long clip off.

    aiohttp's default is a five minute total, so a clip longer than that would
    be stopped mid-play by the library rather than by anything here, which the
    docstring's "lasts as long as it lasts" would then be wrong about.
    """

    class _TimeoutRecordingSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            self.timeout = timeout
            return _RecordingRequest(self, data)

    session = _TimeoutRecordingSession()
    # A tenth of a second of audio. The clip is paced in real time, so asking
    # this question with a long one would buy a slower test and no more
    # certainty.
    await _client(session).async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000)

    assert isinstance(session.timeout, aiohttp.ClientTimeout)
    # Looser than the limits this method applies itself, so that they are the
    # ones that decide and can say why. Level with them, aiohttp would be free
    # to cut in first and turn "playing far slower than it was sent as" into a
    # bare TimeoutError.
    assert session.timeout.total > 0.1 * client_module.AUDIO_SLOWEST_TOLERATED + (
        client_module.TIMEOUT_SECONDS
    )
    assert session.timeout.sock_connect == client_module.TIMEOUT_SECONDS


def test_a_stalled_device_is_given_up_on_before_a_slow_one_is():
    """An ordering between two constants, asserted because nothing else shows it.

    A device that never takes a byte must be reported as never having
    started, not as playing slowly. Which of the two limits fires decides
    which it is told, so the stall window has to be inside the flat part of
    the ceiling. They are equal today, which satisfies this and leaves the
    wording to the order the checks are made in.
    """
    assert client_module.AUDIO_STALL_SECONDS <= client_module.TIMEOUT_SECONDS


async def test_a_device_that_never_takes_a_byte_is_not_blamed_for_stopping():
    """The two failures read the same from here and mean different things."""

    class _UnreachableSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _UnreachableRequest(self, data)

    class _UnreachableRequest(_RecordingRequest):
        async def __aenter__(self):
            await asyncio.sleep(3600)  # never pulls the body

    import custom_components.dahua.client as module

    original = (module.TIMEOUT_SECONDS, module.AUDIO_STALL_SECONDS)
    # Both, because the stall window is what decides this one and the ceiling
    # is what stops the test taking twenty seconds to get there.
    module.TIMEOUT_SECONDS = 0.1
    module.AUDIO_STALL_SECONDS = 0.1
    try:
        with pytest.raises(asyncio.TimeoutError) as raised:
            await _client(_UnreachableSession()).async_play_audio(
                b"\x00" * 320, "Audio/PCM", 32000
            )
    finally:
        module.TIMEOUT_SECONDS, module.AUDIO_STALL_SECONDS = original

    assert "never started taking the audio" in str(raised.value)


async def test_a_device_draining_slower_than_it_was_told_still_gets_it_all(
    monkeypatch,
):
    """The caller naming the wrong rate must not abandon a push mid-clip.

    This is the safety case, not a nicety. The two formats on offer are a
    factor of four apart -- 16-bit PCM at 16 kHz is 32000 bytes a second,
    A-law at 8 kHz is 8000 -- so a caller who picks the wrong one declares a
    clip four times shorter than the device will take to play it. Bounded by
    the clip's own length plus a flat allowance, that ends as a push given up
    on part way through, which is the shape a VTO2000A was found hung after.
    What hung it is not actually known -- the device stopped taking bytes, the
    stalled session was held for about ten minutes, and it was then killed --
    but tolerating a slow drain is right under every reading of that.

    So the device here drains at a quarter of the rate it was told, which is
    exactly the size of the mistake on offer, and the whole clip still has to
    arrive. The elapsed assertion is the point of the test: it fails unless
    the send really did run past what the old cap would have allowed.
    """
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(client_module, "AUDIO_STALL_SECONDS", 1.0)
    monkeypatch.setattr(client_module, "AUDIO_PROGRESS_CHECK_SECONDS", 0.02)

    class _SlowSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _SlowRequest(self, data)

    class _SlowRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                # A tenth of a second of audio, taken in a quarter of one.
                await asyncio.sleep(0.25)
                self._session.chunks.append(piece)
                self._session.body += piece
            return _RecordingResponse(200)

    session = _SlowSession()
    # A quarter second of A-law at 8 kHz, in three writes.
    audio = bytes(range(256)) * 8
    started = asyncio.get_running_loop().time()

    await _client(session).async_play_audio(audio, "Audio/G.711A", 8000)

    elapsed = asyncio.get_running_loop().time() - started
    assert session.body == audio
    # Past what "the clip's own length plus TIMEOUT_SECONDS" would have given
    # it, so the test is standing where the old bound used to cut in.
    assert elapsed > len(audio) / 8000 + client_module.TIMEOUT_SECONDS


async def test_a_device_that_stops_part_way_is_still_given_up_on(monkeypatch):
    """Tolerating slowness must not mean tolerating a device that has stopped.

    The other half of the pair above. Nothing arrives after the second write,
    and the ceiling is left wide enough that only the stall window can end
    this, so the message it raises says which limit fired.
    """
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(client_module, "AUDIO_STALL_SECONDS", 0.2)
    monkeypatch.setattr(client_module, "AUDIO_PROGRESS_CHECK_SECONDS", 0.02)

    class _StoppingSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _StoppingRequest(self, data)

    class _StoppingRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                self._session.chunks.append(piece)
                self._session.body += piece
                if len(self._session.chunks) == 2:
                    await asyncio.sleep(3600)  # takes no more
            return _RecordingResponse(200)

    session = _StoppingSession()

    with pytest.raises(asyncio.TimeoutError) as raised:
        await _client(session).async_play_audio(
            bytes(range(256)) * 8, "Audio/G.711A", 8000
        )

    assert "stopped taking audio part way through" in str(raised.value)
    # It really did start, which is what separates this message from the one
    # above it.
    assert len(session.chunks) == 2


async def test_a_device_slower_than_four_times_is_not_waited_on_for_ever(
    monkeypatch,
):
    """The ceiling still exists, and says what it means when it fires.

    A device taking bytes steadily but far slower than any format explains is
    not a format mistake, and the stall window never fires on it because
    something always just arrived. Without the ceiling this would be waited on
    until the caller's own deadline, holding the speaker lock throughout.
    """
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(client_module, "AUDIO_STALL_SECONDS", 1.0)
    monkeypatch.setattr(client_module, "AUDIO_PROGRESS_CHECK_SECONDS", 0.02)

    class _CrawlingSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _CrawlingRequest(self, data)

    class _CrawlingRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                # Each write carries a tenth of a second of audio and takes
                # half of one, so progress never stops and the stall window is
                # never reached -- but the device is five times slower than
                # the rate it was given, which is past what any format
                # mistake could account for.
                await asyncio.sleep(0.5)
                self._session.chunks.append(piece)
            return _RecordingResponse(200)

    with pytest.raises(asyncio.TimeoutError) as raised:
        await _client(_CrawlingSession()).async_play_audio(
            b"\x00" * 2400, "Audio/G.711A", 8000
        )

    assert "far slower than the 8000 bytes a second" in str(raised.value)


async def test_a_cancellation_during_the_silent_wait_is_not_swallowed(monkeypatch):
    """An outer deadline must still fire if it lands during the cleanup.

    The cleanup awaits the request task it has just cancelled. Awaiting a
    cancelled task raises `CancelledError`, and suppressing that would eat a
    cancellation aimed at this method: an enclosing `asyncio.timeout` would
    then never raise, and the caller's deadline would silently not exist.
    """
    monkeypatch.setattr(client_module, "AUDIO_REFUSAL_GRACE_SECONDS", 30)

    class _SilentSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _SilentRequest(self, data)

    class _SilentRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                self._session.body += piece
            await asyncio.sleep(3600)

    client = _client(_SilentSession())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await client.async_play_audio(b"\x00" * 320, "Audio/PCM", 32000)


async def test_the_socket_is_not_returned_to_the_pool(
    aiohttp_server, socket_enabled, monkeypatch
):
    """The next poll must not travel down a socket that is still an audio stream.

    This endpoint never answers, so the normal successful path ends by
    cancelling a request that is still waiting for a response. If aiohttp
    returned that connection to its keep-alive pool, the following ordinary
    GET would go down a socket the device regards as an open clip: the request
    would be played rather than answered, and that poll would time out.

    aiohttp closes it instead. That is a property of the library rather than
    of this code, which is exactly why it is pinned here.
    """
    monkeypatch.setattr(client_module, "AUDIO_REFUSAL_GRACE_SECONDS", 0.3)
    peers = []

    async def handler(request):
        peers.append(request.transport.get_extra_info("peername"))
        await request.read()
        await asyncio.sleep(30)  # the device takes the clip and says nothing

    async def answering(request):
        peers.append(request.transport.get_extra_info("peername"))
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/cgi-bin/audio.cgi", handler)
    app.router.add_get("/after", answering)
    server = await aiohttp_server(app)

    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector) as session:
        client = _client(session, address=str(server.host), port=server.port)
        await client.async_play_audio(b"\x00" * 3200, "Audio/PCM", 32000)

        assert not connector._acquired, "the audio connection is still held"
        assert sum(len(v) for v in connector._conns.values()) == 0, "it was pooled"

        async with session.get(str(server.make_url("/after"))) as response:
            assert response.status == 200

    assert len(peers) == 2
    assert peers[0] != peers[1], "the next request reused the audio socket"


async def test_a_cancellation_landing_inside_the_cleanup_is_not_swallowed(monkeypatch):
    """The narrow window, built on purpose, because it is the whole bug.

    The cleanup cancels the request task and waits for it. If that wait is
    `await sending` under `suppress(CancelledError)`, then a cancellation
    aimed at *this* method which arrives during it is eaten: the method
    returns normally, and an enclosing `asyncio.timeout` calls `uncancel()`
    with no exception pending and never raises. The caller's deadline simply
    does not exist.

    The sibling test above misses it, because there the cancellation arrives
    before the cleanup rather than during it, and the original CancelledError
    is still pending to resume. So this one makes the request slow to cancel
    and lands the deadline in the gap.
    """
    monkeypatch.setattr(client_module, "AUDIO_REFUSAL_GRACE_SECONDS", 0.05)

    class _StubbornSession(_RecordingSession):
        def post(self, url, headers=None, data=None, timeout=None):
            return _StubbornRequest(self, data)

    class _StubbornRequest(_RecordingRequest):
        async def __aenter__(self):
            async for piece in self._data:
                self._session.body += piece
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Slow to let go, which is what holds the cleanup open long
                # enough for the deadline below to land inside it.
                await asyncio.shield(asyncio.sleep(0.4))
                raise

    client = _client(_StubbornSession())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await client.async_play_audio(b"\x00" * 320, "Audio/PCM", 32000)

    # The deadline fired while the request was still shielding its own
    # cleanup, which is the point of the test and also means that request
    # outlives this block. Wait it out rather than leaving a task behind for
    # the next test to trip over.
    await asyncio.sleep(0.5)
