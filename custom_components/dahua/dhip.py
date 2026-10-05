"""One-shot DHIP: log in, ask, log out.

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
"""

from __future__ import annotations

import asyncio
import logging

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
