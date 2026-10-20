"""DHIP as a transport: one-shot for the add flow, and held open for an entry.

DHIP is Dahua's own protocol on port 5000, the one vto.py keeps open to a doorbell for
its events. vto.py is a long-lived connection: once logged in it attaches the event
stream, reads the door configuration and starts a keepalive, and it only reacts to a
login that succeeds, so a refused password would leave a caller waiting for nothing.

This is for asking a device something once, where the answer to "did the login work"
matters as much as the reply: the add flow, for a device that serves no HTTP at all.
#949 measured that on a VTH5221D (3.000.0012000.0.R, only 5000 and 37777 open): the
login completes over DHIP, and magicBox.getDeviceType and configManager.getConfig
answer there.

The wire format is vto.py's, reused rather than copied, because it is what has been
talking to real doorbells: the same 32-byte header from convert_message, the same
login challenge and password hash, and the same reply parsing, which splits on newlines
and pulls the JSON objects out of each packet. Nothing here imports Home Assistant.

`DhipTransport` at the bottom is the other half: a connection that stays open and
carries RPC2 envelopes, so an entry for a monitor with no web server can be read by
`rpc2.py`'s existing client rather than by a second one. Its docstring has what the
hardware made necessary.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from .vto import DahuaVTOClient, DAHUA_GLOBAL_LOGIN

_LOGGER: logging.Logger = logging.getLogger(__package__)

DHIP_PORT = 5000

# Per reply. The whole exchange is a handful of small messages to a device that
# has already answered on this port, so a reply is prompt or not coming.
DHIP_REPLY_TIMEOUT_SECONDS = 5

# What a device answers a login without a password with, carrying the realm and
# random the real login is hashed with. vto.py's pre-login matches the same text.
LOGIN_CHALLENGE = "Component error: login challenge!"


class DhipLoginRefused(Exception):
    """The device answered the login, and said no.

    Distinct from every other failure on purpose. A refused login is a statement
    about the credentials; a timeout or a closed socket is not, and reporting it as
    a wrong password sends the user to change something that was right.
    """

    def __init__(self, description: str, code=None):
        super().__init__(description)
        self.code = code


class DhipSession:
    """A logged-in DHIP connection. Use as `async with async_dhip_session(...)`."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._buffer = b""
        self._id = 0
        self.session_id = 0

    async def _send(self, method: str, params=None) -> int:
        self._id += 1
        message = {
            "id": self._id,
            "session": self.session_id,
            "magic": "0x1234",
            "method": method,
            "params": {} if params is None else params,
        }
        self._writer.write(DahuaVTOClient.convert_message(message))
        await self._writer.drain()
        return self._id

    async def exchange(self, message: dict) -> dict:
        """Send an envelope somebody else built, and return its reply.

        `call` is for this module's own questions and numbers them itself.
        This is for an RPC envelope that already carries its `id`, `method`,
        `params` and `session`, so `rpc2.py` can put its request on this
        transport without either side renumbering the other's.

        The device's session lives with the socket rather than with the caller,
        so it is filled in here from whatever this connection actually holds. A
        caller that built its envelope before a reconnect would otherwise quote
        a session the device has already forgotten.
        """
        message = dict(message)
        message.setdefault("magic", "0x1234")
        if message.get("method") != DAHUA_GLOBAL_LOGIN:
            message["session"] = self.session_id
        request_id = message.get("id")
        if request_id is None:
            self._id += 1
            request_id = message["id"] = self._id
        else:
            # Keep this session's counter ahead of anything the caller has used,
            # so a later `call` of our own cannot collide with it.
            self._id = max(self._id, request_id)
        self._writer.write(DahuaVTOClient.convert_message(message))
        await self._writer.drain()
        return await self._reply(request_id)

    async def _reply(self, request_id: int) -> dict:
        """The reply carrying this request's id.

        Anything else on the wire, such as a notification the device decides to
        send, is skipped rather than taken as the answer.
        """
        while True:
            while b"\n" in self._buffer:
                end = self._buffer.find(b"\n") + 1
                packet, self._buffer = self._buffer[:end], self._buffer[end:]
                for message in DahuaVTOClient.parse_response(packet):
                    if isinstance(message, dict) and message.get("id") == request_id:
                        return message
            chunk = await asyncio.wait_for(
                self._reader.read(4096), DHIP_REPLY_TIMEOUT_SECONDS
            )
            if not chunk:
                raise ConnectionError("DHIP connection closed before the reply")
            self._buffer += chunk

    async def call(self, method: str, params=None) -> dict:
        """Send one request and return the device's reply to it, whole."""
        return await self._reply(await self._send(method, params))

    async def login(self, username: str, password: str) -> dict:
        """Log in the way vto.py does: a challenge, then the hashed password.

        Returns the login reply. Raises DhipLoginRefused when the device answers
        and says no, and ValueError for an answer that is neither a challenge
        nor a verdict.
        """
        challenge = await self.call(
            DAHUA_GLOBAL_LOGIN,
            {
                "clientType": "",
                "ipAddr": "(null)",
                "loginType": "Direct",
                "userName": username,
                "password": "",
            },
        )
        error = challenge.get("error") or {}
        params = challenge.get("params") or {}
        if error.get("message") != LOGIN_CHALLENGE or not params.get("random"):
            # A device that refuses before even challenging, an account locked
            # out for example, still answered: that is a refusal, not a fault.
            if error:
                self.session_id = 0
                raise DhipLoginRefused(
                    "DHIP login refused before the challenge: {0}".format(
                        error.get("message")
                    ),
                    code=error.get("code"),
                )
            raise ValueError("DHIP login answered without a challenge")
        self.session_id = challenge.get("session") or 0

        verdict = await self.call(
            DAHUA_GLOBAL_LOGIN,
            {
                "clientType": "",
                "ipAddr": "(null)",
                "loginType": "Direct",
                "userName": username,
                "password": DahuaVTOClient._get_hashed_password(
                    params.get("random"), params.get("realm"), username, password
                ),
                "authorityType": "Default",
            },
        )
        # vto.py takes a keepAliveInterval as success; `result: true` is the RPC
        # answer shape, so either counts. An explicit false with an error is the
        # device refusing these credentials.
        if (
            verdict.get("result") is True
            or (verdict.get("params") or {}).get("keepAliveInterval") is not None
        ):
            if verdict.get("session"):
                self.session_id = verdict["session"]
            return verdict
        if verdict.get("result") is False:
            error = verdict.get("error") or {}
            # No session was granted, so there is nothing to log out of.
            self.session_id = 0
            raise DhipLoginRefused(
                "DHIP login refused: {0}".format(error.get("message")),
                code=error.get("code"),
            )
        raise ValueError("DHIP login answered with neither a session nor a refusal")

    async def close(self) -> None:
        """Log out if logged in, then close. Never raises: it runs in a finally."""
        try:
            if self.session_id:
                # Waited for, briefly: closing straight after writing it can drop
                # the request before the device reads it, leaving the session open
                # on the device until it times out.
                await asyncio.wait_for(
                    self._reply(await self._send("global.logout")),
                    min(DHIP_REPLY_TIMEOUT_SECONDS, 2),
                )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("DHIP logout failed", exc_info=True)
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("DHIP close failed", exc_info=True)


class async_dhip_session:  # pylint: disable=invalid-name
    """Connect, log in, and hand back the session; log out and close on exit.

    Exactly one login attempt is made. A Dahua device locks an account after a few
    refused logins, so this never retries, and a caller that already knows the
    credentials were refused over another protocol should not call it at all.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = DHIP_PORT,
        connect_timeout: float = DHIP_REPLY_TIMEOUT_SECONDS,
    ):
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._connect_timeout = connect_timeout
        self._session: DhipSession | None = None

    async def __aenter__(self) -> DhipSession:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), self._connect_timeout
        )
        self._session = DhipSession(reader, writer)
        try:
            await self._session.login(self._username, self._password)
        except BaseException:
            await self._session.close()
            raise
        return self._session

    async def __aexit__(self, *exc_info) -> None:
        if self._session is not None:
            await self._session.close()


class DhipTransport:
    """A DHIP connection that stays open, carries RPC2 envelopes, and relogs in.

    `async_dhip_session` above is for asking a device one question. This is for
    a device that has no other way to be talked to at all: a VTH5221D serves no
    HTTP, so there is no `/RPC2` to POST to, and every read an entry makes has
    to go down this socket for as long as the entry is loaded (#949).

    Three things it adds over a bare session, each measured rather than assumed.

    **One request at a time.** Two calls through `asyncio.gather` on one session
    do not interleave, they raise::

        RuntimeError: read() called while another coroutine is already waiting
        for incoming data

    straight out of the StreamReader, measured on a VTH5221D. The coordinator
    fans its reads out with `gather`, so without the lock below the second read
    of every poll would fail. A lock rather than a reply-routing reader task
    because these boxes are small and already limited to
    MAX_CONCURRENT_REQUESTS_PER_HOST elsewhere: serialising costs a little
    latency, and a reader task would add a second failure mode to a transport
    that has no other.

    **The session belongs to the socket.** Reconnecting means logging in again,
    which is why the login lives here and not in the caller.

    **A refused login must be able to stop this.** `General` on that firmware
    reads `LockLoginEnable: true, LockLoginTimes: 5, LoginFailLockTime: 300`, so
    five refusals lock the account for five minutes, including for the person
    trying to correct it. That is #729 on a third transport. `on_login_refused`
    is called with the reason and, when it returns True, no further login is
    attempted until `clear_refusals` is called. Nothing here reaches into Home
    Assistant to find that budget; the owner passes it in.
    """

    def __init__(
        self,
        address: str,
        username: str,
        password: str,
        port: int = DHIP_PORT,
        connect_timeout: float = DHIP_REPLY_TIMEOUT_SECONDS,
        on_login_refused=None,
    ) -> None:
        self._address = address
        self._username = username
        self._password = password
        self._port = port
        self._connect_timeout = connect_timeout
        self._on_login_refused = on_login_refused
        self._session: DhipSession | None = None
        self._lock = asyncio.Lock()
        self._give_up = False
        # Bumped on every new socket, so a caller can tell that the session it
        # was quoting has been replaced underneath it.
        self.generation = 0
        # What the device asked for when it granted the session. Read by the
        # RPC2 client to set its keepalive, the same way the HTTP login's reply
        # is read. None until a login has succeeded.
        self.keep_alive_interval = None

    @property
    def session_id(self):
        return self._session.session_id if self._session is not None else 0

    @property
    def connected(self) -> bool:
        return self._session is not None

    def clear_refusals(self) -> None:
        """Allow logins again, after the credentials have been changed."""
        self._give_up = False

    async def exchange(self, message: dict) -> dict:
        """Send one RPC2 envelope and return the device's reply.

        Connects and logs in first if needed. A failed config or identity read
        is retried on a fresh socket once. Actions are never replayed after a
        lost reply, because the device may already have performed them.
        """
        async with self._lock:
            session = await self._ensure_session()
            try:
                return await session.exchange(message)
            except (DhipLoginRefused, asyncio.CancelledError):
                raise
            except Exception as first_failure:
                _LOGGER.debug(
                    "DHIP exchange with %s failed (%s); reconnecting once",
                    self._address,
                    type(first_failure).__name__,
                    exc_info=True,
                )
                await self._drop()
                # A lost reply does not prove an action failed. Only replay
                # known reads; an unlock, call or config write may have run.
                method = message.get("method", "")
                if not method.startswith(("magicBox.get", "configManager.get")):
                    raise _transient(first_failure) from first_failure
                session = await self._ensure_session()
                try:
                    return await session.exchange(message)
                except DhipLoginRefused:
                    raise
                except Exception as second_failure:
                    await self._drop()
                    raise _transient(second_failure) from second_failure

    async def _ensure_session(self) -> DhipSession:
        if self._session is not None:
            return self._session
        if self._give_up:
            # Stated as a refusal rather than a transport failure, because that
            # is what it is: the credentials were rejected and nothing has said
            # they changed. Retrying here is what locks the device.
            raise DhipLoginRefused(
                "Not logging in to {0} again: these credentials were "
                "refused".format(self._address)
            )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._address, self._port),
                self._connect_timeout,
            )
        except Exception as error:
            raise _transient(error) from error
        session = DhipSession(reader, writer)
        try:
            verdict = await session.login(self._username, self._password)
            self.keep_alive_interval = (verdict.get("params") or {}).get(
                "keepAliveInterval"
            )
        except DhipLoginRefused as refused:
            await session.close()
            if self._on_login_refused is not None and self._on_login_refused(refused):
                self._give_up = True
            raise
        except BaseException as error:
            await session.close()
            if isinstance(error, Exception):
                raise _transient(error) from error
            raise
        self._session = session
        self.generation += 1
        _LOGGER.debug(
            "DHIP session %d established with %s", self.generation, self._address
        )
        return session

    async def _drop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("DHIP close failed", exc_info=True)

    async def close(self) -> None:
        """Log out and close. Safe to call more than once."""
        async with self._lock:
            await self._drop()


def _transient(error: Exception) -> Exception:
    """Re-shape a socket failure as one the RPC2 callers already read as transient.

    `rpc2_failure_is_permanent` in client.py treats anything that is not a
    TimeoutError or an `aiohttp.ClientConnectionError` as proof the device does
    not speak RPC2, and writes the transport off for the whole process. For a
    device whose only transport this is, that would take the entry down until
    Home Assistant restarted, on one dropped socket.

    A `ConnectionRefusedError` is an OSError and would be judged permanent,
    which is exactly backwards: the device being unreachable for a moment says
    nothing about whether it speaks DHIP. So the socket failures are reported in
    the vocabulary that layer already understands.
    """
    if isinstance(error, (TimeoutError, aiohttp.ClientConnectionError)):
        return error
    return aiohttp.ClientConnectionError(
        "DHIP transport failed: {0}: {1}".format(type(error).__name__, error)
    )
