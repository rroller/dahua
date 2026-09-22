"""One channel's handler failing must not silence the whole host.

The event stream is per host, shared by every channel on it, and
`stream_events` wraps its call to `on_receive` in `try`/`finally` with no
handler. So an exception raised while handling one event does not merely lose
that event -- it leaves the read loop, and every camera on the device stops
receiving events until the retry backoff reconnects.

#475 is one way in: a truncated JSON payload left `data` as a string, and
`.get()` on it raised AttributeError. That specific read is now guarded, but
the blast radius is the real problem -- any handler raising anything does this.
"""

import json

from custom_components.dahua import DahuaHostEventStream


def _stream(*coordinators):
    stream = DahuaHostEventStream.__new__(DahuaHostEventStream)
    stream._address = "10.0.0.9"
    stream._received_data = False
    stream._by_channel = {0: list(coordinators)}
    return stream


class _Recorder:
    def __init__(self):
        self.seen = []

    def handle_event(self, event):
        self.seen.append(event.get("Code"))


class _Exploder:
    def __init__(self):
        self.calls = 0

    def handle_event(self, event):
        self.calls += 1
        raise AttributeError("'str' object has no attribute 'get'")


def _wire(*payloads):
    out = b""
    for code in payloads:
        body = "Code=%s;action=Start;index=0;data=%s" % (code, json.dumps({"x": 1}))
        out += ("--myboundary\r\nContent-Type: text/plain\r\n"
                "Content-Length: 0\r\n\r\n" + body + "\r\n").encode()
    return out


def test_a_failing_handler_does_not_escape_on_receive():
    """What used to leave the read loop and end the stream."""
    stream = _stream(_Exploder())

    stream.on_receive(_wire("VideoMotion"), 0)  # must not raise


def test_the_other_channels_still_get_the_event():
    """Per coordinator, not per event -- one bad listener is not all of them."""
    exploder, recorder = _Exploder(), _Recorder()
    stream = _stream(exploder, recorder)

    stream.on_receive(_wire("CrossLineDetection"), 0)

    assert exploder.calls == 1
    assert recorder.seen == ["CrossLineDetection"], (
        "a sibling coordinator lost an event it could have handled")


def test_the_stream_keeps_working_afterwards():
    """The point: the next event still arrives."""
    exploder, recorder = _Exploder(), _Recorder()
    stream = _stream(exploder, recorder)

    stream.on_receive(_wire("VideoMotion"), 0)
    stream.on_receive(_wire("DoorbellPressed"), 0)

    assert recorder.seen == ["VideoMotion", "DoorbellPressed"]
    assert exploder.calls == 2


def test_a_healthy_stream_is_unchanged():
    recorder = _Recorder()
    stream = _stream(recorder)

    stream.on_receive(_wire("VideoMotion", "AlarmLocal"), 0)

    assert recorder.seen == ["VideoMotion", "AlarmLocal"]
