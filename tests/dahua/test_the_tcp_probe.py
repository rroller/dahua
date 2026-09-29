"""The probe that decides whether to offer the switch to HTTPS.

`_async_probe_tcp` is the one piece of the unreachable-host repair that touches the
network, which is why every test in `test_repairs.py` replaces it: those tests are about
which card is raised, and a real connection attempt there would be slow and would depend
on what happens to be listening on the machine running them. The consequence is that the
function itself was never executed by the suite at all.

It is worth executing, because what it promises is narrow and easy to break by accident.
It answers one question, it never raises whatever happens, and it closes what it opened.
The last of those is not decoration: it runs against a host that has already failed
several polls, so a leaked socket would accumulate against a device that is in trouble.
"""

import asyncio

import pytest

from custom_components.dahua.host import _async_probe_tcp


async def _a_server(on_connect=None):
    """A real listener on a free port, and its port."""
    async def handle(reader, writer):
        if on_connect is not None:
            await on_connect(reader, writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_a_port_that_answers_is_reported_open(socket_enabled):
    server, port = await _a_server()
    try:
        assert await _async_probe_tcp("127.0.0.1", port) is True
    finally:
        server.close()
        await server.wait_closed()


async def test_nothing_listening_is_reported_closed(socket_enabled):
    """The port is one that was listening a moment ago, rather than a number picked
    out of the air: a hard coded port would be answered by whatever else is running
    on the machine, and the test would then pass or fail on that."""
    server, port = await _a_server()
    server.close()
    await server.wait_closed()

    assert await _async_probe_tcp("127.0.0.1", port) is False


async def test_the_connection_is_closed_again(socket_enabled):
    """The probe is a question, not a session. It runs against a host that has
    already missed several polls, so a socket left open here would pile up against
    the device least able to afford it.

    The server reads to end-of-file, which is what a closed client looks like from
    the other side. Nothing else here would notice the difference.
    """
    closed = asyncio.Event()

    async def on_connect(reader, writer):
        await reader.read()  # returns b"" when the client goes away
        closed.set()

    server, port = await _a_server(on_connect)
    try:
        assert await _async_probe_tcp("127.0.0.1", port) is True
        await asyncio.wait_for(closed.wait(), timeout=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_a_host_that_never_answers_gives_up(monkeypatch):
    """The timeout is the reason this cannot be a bare `open_connection`. A device
    that has stopped answering typically does not refuse the connection, it says
    nothing at all, and the repair flow is waiting on this.

    `host.asyncio` is the asyncio module itself, so this patch is global for the
    duration of the test rather than scoped to the module under test. That is worth
    saying out loud: the same mistake once made a test count Home Assistant's own
    `sleep(0)` as one of the integration's (#857). It is safe here only because
    nothing else in this test opens a connection.
    """
    async def never(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "open_connection", never)

    assert await asyncio.wait_for(
        _async_probe_tcp("192.0.2.1", 443, timeout=0.2), timeout=5) is False


async def test_a_refusal_is_an_answer_not_an_error(monkeypatch):
    """Whatever the failure is, the caller gets False. `_async_evaluate_host` has no
    handler around this call, and it runs inside a task created by
    `async_record_host_failure`, so an exception escaping here would surface as an
    unhandled task exception in the log of a user whose only actual problem is a
    camera that is offline.
    """
    async def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(asyncio, "open_connection", refuse)

    assert await _async_probe_tcp("192.0.2.1", 443, timeout=0.2) is False


async def test_a_socket_that_will_not_close_cleanly_still_answers(monkeypatch):
    """The answer is already known by the time the socket is closed, so a failure
    while closing must not turn a successful probe into a failed one. It is reachable:
    `wait_closed` raises when the peer resets the connection.
    """
    class _Writer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            raise ConnectionResetError("peer reset")

    writer = _Writer()

    async def connect(*args, **kwargs):
        return object(), writer

    monkeypatch.setattr(asyncio, "open_connection", connect)

    assert await _async_probe_tcp("192.0.2.1", 443, timeout=0.2) is True
    assert writer.closed, "the writer must still be closed"
