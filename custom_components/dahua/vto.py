"""
Copied and modified from https://github.com/elad-bar/DahuaVTO2MQTT
Thanks to @elad-bar
"""

import struct
import sys
import logging
import json
import asyncio
import hashlib
from json import JSONDecoder
from typing import Optional, Callable

PROTOCOLS = {True: "https", False: "http"}

# How long to wait for the doorbell to answer a hang-up before giving up.
# The command goes over the already-open socket on port 5000, so a reply is
# either prompt or not coming.
CANCEL_CALL_TIMEOUT_SECONDS = 5


class CancelCallRefused(Exception):
    """The doorbell did not agree to hang up.

    Raised rather than returning False so a caller cannot report success by
    forgetting to check. Translated to a HomeAssistantError at the edge,
    because this module deliberately does not import Home Assistant.
    """


_LOGGER: logging.Logger = logging.getLogger(__package__)

DAHUA_DEVICE_TYPE = "deviceType"
DAHUA_SERIAL_NUMBER = "serialNumber"
DAHUA_VERSION = "version"
DAHUA_BUILD_DATE = "buildDate"

DAHUA_GLOBAL_LOGIN = "global.login"
DAHUA_GLOBAL_KEEPALIVE = "global.keepAlive"
DAHUA_EVENT_MANAGER_ATTACH = "eventManager.attach"
DAHUA_CONFIG_MANAGER_GETCONFIG = "configManager.getConfig"
DAHUA_MAGICBOX_GETSOFTWAREVERSION = "magicBox.getSoftwareVersion"
DAHUA_MAGICBOX_GETDEVICETYPE = "magicBox.getDeviceType"

DAHUA_ALLOWED_DETAILS = [DAHUA_DEVICE_TYPE, DAHUA_SERIAL_NUMBER]


class DahuaVTOClient(asyncio.Protocol):
    requestId: int
    sessionId: int
    keep_alive_interval: int
    username: str
    password: str
    realm: Optional[str]
    random: Optional[str]
    messages: []
    dahua_details: {}
    base_url: str
    hold_time: int
    lock_status: {}
    data_handlers: {}
    buffer: bytearray

    # What the device said when it answered the login and granted no session,
    # or None while nothing has been refused. Declared on the class because the
    # coordinator reads it off whatever it got back from `create_connection`,
    # and the retry tests build a stand-in protocol that sets only the two
    # attributes that loop used to read.
    login_refused = None

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        is_ssl: bool,
        on_receive_vto_event,
    ):
        self.dahua_details = {}
        self.host = host
        self.username = username
        self.password = password
        self.is_ssl = is_ssl
        self.base_url = f"{PROTOCOLS[self.is_ssl]}://{self.host}/cgi-bin/"
        self.realm = None
        self.random = None
        self.request_id = 1
        self.sessionId = 0
        self.keep_alive_interval = 0
        self.transport = None
        self.hold_time = 0
        self.lock_status = {}
        self.data_handlers = {}
        self.buffer = bytearray()

        self._keep_alive_handle = None

        # This is the hook back into HA
        self.on_receive_vto_event = on_receive_vto_event
        self._loop = asyncio.get_event_loop()
        self.disconnected = self._loop.create_future()
        self.received_data = False

    def connection_made(self, transport):
        _LOGGER.debug("VTO connection established")

        try:
            self.transport = transport
            self.pre_login()

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(
                f"Failed to handle message, error: {ex}, Line: {exc_tb.tb_lineno}"
            )

    def data_received(self, data):
        _LOGGER.debug("Event data %s: '%s'", self.host, data)

        # Whether this device has said anything at all on this connection --
        # a login reply, a keepAlive answer, an event. The reconnect decision
        # needs it to tell a doorbell that is refusing us from one that is
        # simply quiet, which most front doors are for hours at a time.
        self.received_data = True

        self.buffer += data

        while b"\n" in self.buffer:

            newline_index = self.buffer.find(b"\n") + 1
            packet = self.buffer[:newline_index]
            self.buffer = self.buffer[newline_index:]

            try:
                messages = self.parse_response(packet)
                for message in messages:
                    if message is None:
                        continue

                    message_id = message.get("id")

                    handler: Callable = self.data_handlers.get(
                        message_id, self.handle_default
                    )
                    handler(message)
            except Exception as ex:
                exc_type, exc_obj, exc_tb = sys.exc_info()

                _LOGGER.error(
                    f"Failed to handle message, error: {ex}, Line: {exc_tb.tb_lineno}"
                )

    def handle_notify_event_stream(self, params):
        try:
            if params is None:
                return
            event_list = params.get("eventList")

            for message in event_list:
                for k in self.dahua_details:
                    if k in DAHUA_ALLOWED_DETAILS:
                        message[k] = self.dahua_details.get(k)

                self.on_receive_vto_event(message)

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(
                f"Failed to handle event, error: {ex}, Line: {exc_tb.tb_lineno}"
            )

    def handle_default(self, message):
        _LOGGER.debug("Data received without handler: %s", message)

    def eof_received(self):
        _LOGGER.debug("Server sent EOF message")

        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        if not self.disconnected.done():
            self.disconnected.set_result(True)

    def connection_lost(self, exc):
        _LOGGER.error("server closed the connection")

        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        if not self.disconnected.done():
            self.disconnected.set_result(True)

    def close(self) -> None:
        """Drop the connection and stop the keep-alive.

        Cancelling the coordinator's stream task is not enough: that task is
        parked on ``await self.disconnected`` and cancelling a task does not
        close an asyncio transport. Without this the socket to port 5000 stays
        open with its event subscription, the keep-alive keeps rescheduling
        itself on every reply, and the doorbell keeps pushing events into an
        entry Home Assistant has already unloaded -- once per reload.
        """
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        transport = self.transport
        if transport is not None:
            try:
                transport.close()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("Failed to close the VTO transport", exc_info=True)
        if not self.disconnected.done():
            self.disconnected.set_result(True)

    def send(self, action, handler, params=None, object_id=None):
        if params is None:
            params = {}

        self.request_id += 1

        message_data = {
            "id": self.request_id,
            "session": self.sessionId,
            "magic": "0x1234",
            "method": action,
            "params": params,
        }
        # Methods reached through a factory instance carry its object id, e.g.
        # VideoTalkPhone.endCall (#460). Absent for ordinary calls.
        if object_id is not None:
            message_data["object"] = object_id

        request_id = self.request_id
        self.data_handlers[request_id] = handler

        if not self.transport.is_closing():
            message = self.convert_message(message_data)

            self.transport.write(message)

        # Returned so a caller that wants to wait for this particular reply
        # can find its own handler again, and drop it afterwards.
        return request_id

    @staticmethod
    def convert_message(data):
        message_data = json.dumps(data, indent=4)

        header = struct.pack(">L", 0x20000000)
        header += struct.pack(">L", 0x44484950)
        header += struct.pack(">d", 0)
        header += struct.pack("<L", len(message_data))
        header += struct.pack("<L", 0)
        header += struct.pack("<L", len(message_data))
        header += struct.pack("<L", 0)

        message = header + message_data.encode("utf-8")

        return message

    def pre_login(self):
        _LOGGER.debug("Prepare pre-login message")

        def handle_pre_login(message):
            if message is None:
                return
            error = message.get("error")
            params = message.get("params")

            if error is not None:
                error_message = error.get("message")

                if error_message == "Component error: login challenge!":
                    self.random = params.get("random")
                    self.realm = params.get("realm")
                    self.sessionId = message.get("session")

                    self.login()
                else:
                    # A device can refuse before it challenges -- an account
                    # already locked out answers "user or password not valid!"
                    # here. This branch did nothing at all, so that connection
                    # sat open until the device dropped it and then reconnected
                    # at once, because the device had demonstrably spoken. Same
                    # unbudgeted loop as a refusal after the challenge, one
                    # round trip earlier.
                    self.login_refused = (
                        str(error_message or "").strip() or "the login was refused"
                    )
                    _LOGGER.warning(
                        "The doorbell at %s refused the login before challenging: %s",
                        self.host,
                        self.login_refused,
                    )
                    self.close()

        request_data = {
            "clientType": "",
            "ipAddr": "(null)",
            "loginType": "Direct",
            "userName": self.username,
            "password": "",
        }

        self.send(DAHUA_GLOBAL_LOGIN, handle_pre_login, request_data)

    def login(self):
        _LOGGER.debug("Prepare login message")

        def handle_login(message):
            if message is None:
                return
            params = message.get("params")
            if not isinstance(params, dict):
                # A refused login answers with no params at all, and reading
                # `keepAliveInterval` off that None raised AttributeError --
                # inside `data_received`, which logs and swallows it. So a wrong
                # password produced "Failed to handle message" and nothing else:
                # no session, no attach, no keepalive, and nothing telling the
                # caller the credentials were the problem.
                params = {}
            keep_alive_interval = params.get("keepAliveInterval")

            if keep_alive_interval is None:
                # The device answered and granted no session. Judged the way
                # dhip.py judges the same reply: `result: false`, or an error
                # where a session should be. A reply carrying neither is left
                # alone -- it is not evidence of a refusal, and the existing
                # defensive case (params without an interval) must stay a
                # no-op rather than start costing a lockout budget.
                error = message.get("error") or {}
                if message.get("result") is False or error:
                    self.login_refused = (
                        str(error.get("message") or "").strip()
                        or "the device granted no session"
                    )
                    _LOGGER.warning(
                        "The doorbell at %s refused the login: %s",
                        self.host,
                        self.login_refused,
                    )
                    # Closed rather than left to time out. The caller is parked
                    # on `disconnected`, and a socket the device holds open
                    # keeps it waiting for a connection that will never carry
                    # an event.
                    self.close()

            if keep_alive_interval is not None:
                self.keep_alive_interval = keep_alive_interval - 5

                self.load_access_control()
                self.load_version()
                self.load_serial_number()
                self.load_device_type()
                self.attach_event_manager()

                self._keep_alive_handle = self._loop.call_later(
                    self.keep_alive_interval, self.keep_alive
                )

        password = self._get_hashed_password(
            self.random, self.realm, self.username, self.password
        )

        request_data = {
            "clientType": "",
            "ipAddr": "(null)",
            "loginType": "Direct",
            "userName": self.username,
            "password": password,
            "authorityType": "Default",
        }

        self.send(DAHUA_GLOBAL_LOGIN, handle_login, request_data)

    def attach_event_manager(self):
        _LOGGER.debug("Attach event manager")

        def handle_attach_event_manager(message):
            if message is None:
                return
            method = message.get("method")
            params = message.get("params")

            if method == "client.notifyEventStream":
                self.handle_notify_event_stream(params)

        request_data = {"codes": ["All"]}

        self.send(DAHUA_EVENT_MANAGER_ATTACH, handle_attach_event_manager, request_data)

    def load_access_control(self):
        _LOGGER.debug("Get access control configuration")

        def handle_access_control(message):
            if message is None:
                return

            params = message.get("params") or {}
            table = params.get("table")

            # A VTO returns AccessControl as a list of dicts, one per door. A
            # VTH5221D with no door of its own returns a list whose entries are
            # themselves lists, so item.get('AccessProtocol') raised 'list' object
            # has no attribute 'get' and the whole packet's processing aborted
            # (measured over DHIP, #949). Iterate only a list, and only the dict
            # entries in it; any other shape means this device has no local access
            # control to read, which is the right answer for a monitor.
            if isinstance(table, list):
                for item in table:
                    if not isinstance(item, dict):
                        continue

                    access_control = item.get("AccessProtocol")

                    if access_control == "Local":
                        self.hold_time = item.get("UnlockReloadInterval")

                        _LOGGER.debug("Hold time: %s", self.hold_time)

        request_data = {"name": "AccessControl"}

        self.send(DAHUA_CONFIG_MANAGER_GETCONFIG, handle_access_control, request_data)

    async def cancel_call(self, timeout: float = CANCEL_CALL_TIMEOUT_SECONDS):
        """Hang up, and wait to hear whether the doorbell agreed.

        This used to push the command onto the socket and return True at
        once. It is declared async and never awaits anything, so every
        caller was told the call had been cancelled whatever the device
        did -- and #526, cancel_call no longer working on the VTO2211G-WP,
        is exactly that failure. The device's own answer arrived here the
        whole time and was logged at info, where nothing read it.

        Now the answer is waited for and acted on. Two things are
        unambiguous and both raise: no reply at all, and a reply that says
        result false. A reply that arrives without a result is taken as
        agreement, because the doorbell did respond and this has no
        captured console.runCmd reply to be stricter from.
        """
        _LOGGER.debug("Cancelling call on %s", self.host)
        answered = self._loop.create_future()

        def cancel(message):
            _LOGGER.debug("Got cancel call response: %s", message)
            if not answered.done():
                answered.set_result(message)

        request_id = self.send("console.runCmd", cancel, {"command": "hc"})
        try:
            message = await asyncio.wait_for(answered, timeout)
        except asyncio.TimeoutError:
            raise CancelCallRefused(
                "{0} did not answer the hang-up within {1}s".format(self.host, timeout)
            ) from None
        finally:
            # send() registers a handler for every request and only the
            # keep-alive path ever removes one, so drop ours whichever way
            # this ended rather than leaving it to accumulate.
            self.data_handlers.pop(request_id, None)

        if not (isinstance(message, dict) and message.get("result") is False):
            # hc answered and did not refuse, which is the VTO2000A path and
            # stays exactly as it was.
            return True

        # hc answered but refused. A VTO2311R-WP answers both console.runCmd hc
        # and VideoTalkPhone.disconnect with 268959743 yet ends the call through
        # VideoTalkPhone.endCall (#460). Try that over the same connection before
        # giving up, so the refused hc is not the last word on firmware that has
        # a working method.
        _LOGGER.debug(
            "hc was refused on %s (%s); trying VideoTalkPhone.endCall",
            self.host,
            message,
        )
        try:
            if await self._end_call_via_videotalkphone(timeout):
                return True
        except asyncio.TimeoutError:
            pass
        raise CancelCallRefused(
            "{0} refused the hang-up: {1}".format(self.host, message)
        )

    async def _request_reply(
        self,
        action,
        params=None,
        object_id=None,
        timeout: float = CANCEL_CALL_TIMEOUT_SECONDS,
    ):
        """Send one request and wait for the device's reply to it.

        The same wait-and-clean-up `cancel_call` does, factored out so the
        VideoTalkPhone fallback does not repeat it three times. Raises
        asyncio.TimeoutError if no reply arrives, and always drops the handler.
        """
        answered = self._loop.create_future()

        def on_reply(message):
            if not answered.done():
                answered.set_result(message)

        request_id = self.send(action, on_reply, params, object_id=object_id)
        try:
            return await asyncio.wait_for(answered, timeout)
        finally:
            self.data_handlers.pop(request_id, None)

    async def _end_call_via_videotalkphone(self, timeout: float) -> bool:
        """Hang up through VideoTalkPhone, for firmware that refuses hc (#460).

        Measured on a VTO2311R-WP: factory.instance returns an object id, endCall
        on it ends the call (Calling -> Idle), and destroy releases it. endCall is
        a recognised method on a VTO2000A too (it answers rather than
        "Method not found"), so this is the same call across models; it is only
        reached when hc has already been refused. The object is released whether
        or not the hang-up took.
        """
        created = await self._request_reply(
            "VideoTalkPhone.factory.instance", timeout=timeout
        )
        object_id = created.get("result") if isinstance(created, dict) else None
        if not isinstance(object_id, int):
            return False
        try:
            ended = await self._request_reply(
                "VideoTalkPhone.endCall", object_id=object_id, timeout=timeout
            )
        finally:
            try:
                await self._request_reply(
                    "VideoTalkPhone.destroy", object_id=object_id, timeout=timeout
                )
            except asyncio.TimeoutError:
                _LOGGER.debug("VideoTalkPhone.destroy did not answer on %s", self.host)
        return not (isinstance(ended, dict) and ended.get("result") is False)

    def load_version(self):
        _LOGGER.debug("Get version")

        def handle_version(message):
            if message is None:
                return

            params = message.get("params")
            version_details = (
                params.get("version", {}) if isinstance(params, dict) else {}
            )
            if not isinstance(version_details, dict):
                version_details = {}
            build_date = version_details.get("BuildDate")
            version = version_details.get("Version")

            self.dahua_details[DAHUA_VERSION] = version
            self.dahua_details[DAHUA_BUILD_DATE] = build_date

            _LOGGER.debug("Version: %s, Build Date: %s", version, build_date)

        self.send(DAHUA_MAGICBOX_GETSOFTWAREVERSION, handle_version)

    def load_device_type(self):
        _LOGGER.debug("Get device type")

        def handle_device_type(message):
            if message is None:
                return

            params = message.get("params")
            device_type = params.get("type") if isinstance(params, dict) else None

            self.dahua_details[DAHUA_DEVICE_TYPE] = device_type

            _LOGGER.debug("Device Type: %s", device_type)

        self.send(DAHUA_MAGICBOX_GETDEVICETYPE, handle_device_type)

    def load_serial_number(self):
        _LOGGER.debug("Get serial number")

        def handle_serial_number(message):
            if message is None:
                return

            # A VTH returns the T2UServer table as a *list*, not the dict a VTO
            # returns, so `table.get("UUID")` raised `'list' object has no
            # attribute 'get'` -- the same shape #964 handled for AccessControl,
            # in a handler it did not reach. data_received swallows the crash, so
            # it was silent apart from a traceback on every login, and the serial
            # read as None anyway. A monitor keeps no UUID here, so None is the
            # right answer for it; guard the shape rather than assume a dict.
            params = message.get("params")
            table = params.get("table", {}) if isinstance(params, dict) else {}
            serial_number = table.get("UUID") if isinstance(table, dict) else None

            self.dahua_details[DAHUA_SERIAL_NUMBER] = serial_number

            _LOGGER.debug("Serial Number: %s", serial_number)

        request_data = {"name": "T2UServer"}

        self.send(DAHUA_CONFIG_MANAGER_GETCONFIG, handle_serial_number, request_data)

    def keep_alive(self):
        _LOGGER.debug("Keep alive")

        def handle_keep_alive(message):
            self._keep_alive_handle = self._loop.call_later(
                self.keep_alive_interval, self.keep_alive
            )
            if message is None:
                return

            message_id = message.get("id")
            if message_id is not None and message_id in self.data_handlers:
                del self.data_handlers[message_id]
            else:
                _LOGGER.warning(
                    f"Could not delete keep alive handler with message ID {message_id}."
                )

        request_data = {"timeout": self.keep_alive_interval, "action": True}

        self.send(DAHUA_GLOBAL_KEEPALIVE, handle_keep_alive, request_data)

    @staticmethod
    def parse_response(response):
        result = []

        try:
            # Messages can look like like the following, note, this was shorted with ...
            # Note that there can 0 or more events per line. Typically it's 1 event, but sometimes 2 events will arrive.
            # This example shows 2 events
            # \x00\x00\x00DHIP*Q\xa8f\x08\x00\x00\x00m\x04\x00\x00\x00\x00\x00\x00m\x04\x00\x00\x00\x00\x00\x00{"id":8,"method":"client.notifyEventStream","params":{"SID":513,"eventList":[{"Action":"Start","Code":"CrossRegionDetection"...},"session":1722306858}\n \x00\x00\x00DHIP*Q\xa8f\x08\x00\x00\x00\xe8\x00\x00\x00\x00\x00\x00\x00\xe8\x00\x00\x00\x00\x00\x00\x00{"id":8,"method":"client.notifyEventStream","params":{"SID":513,"eventList":[{"Action":"Pulse","Code":"IntelliFrame",..."session":1722306858}\n'
            # Another example
            # \x00\x00\x00DHIP\x8c-\x96{\x08\x00\x00\x00{\x01\x00\x00\x00\x00\x00\x00{\x01\x00\x00\x00\x00\x00\x00{"id":8,"method":"client.notifyEventStream","params":{"SID":513,"eventList":[{"Action":"State","Code":"VideoMotionInfo","Data":[{"Id":0,"Region":[4194303,4194303,4128767,3997695,3801087,3801087,3932159,3407871,3932159,3932158,3932156,3735548,3678204,2101244,2047,2097663,3146239,524799],"RegionName":"Region1","State":"Active","Threshold":54}],"Index":0}]},"session":1722306858}\n

            # Decoded, not `str(response)`. `data_received` slices `self.buffer`,
            # which is bytes, so `str()` gives the *repr*: a UTF-8 byte becomes the
            # four characters backslash, x and two hex digits. `\x` is not a valid
            # JSON escape, so raw_decode refuses the object and extract_json_objects
            # moves past it. One u-umlaut in a card holder's name dropped the whole
            # event, in silence. ASCII survived by accident, the repr of ASCII bytes
            # being the same characters, which is why this held up for so long.
            #
            # errors="replace" is required rather than defensive: the DHIP header in
            # front of every frame is binary and not valid UTF-8, so a strict decode
            # would raise on every packet.
            if isinstance(response, (bytes, bytearray)):
                data = response.decode("utf-8", errors="replace")
            else:
                data = str(response)

            jsons = DahuaVTOClient.extract_json_objects(data)
            for j in jsons:
                result.append(j)
            return result
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            _LOGGER.error(
                f"Failed to read data: {response}, error: {e}, Line: {exc_tb.tb_lineno}"
            )

        return result

    @staticmethod
    def extract_json_objects(text, decoder=JSONDecoder()):
        """Find JSON objects in text, and yield the decoded JSON data

        Does not attempt to look for JSON arrays, text, or other JSON types outside
        of a parent JSON object.
        https://stackoverflow.com/questions/54235528/how-to-find-json-object-in-text-with-python/54235803
        """
        pos = 0
        while True:
            match = text.find("{", pos)
            if match == -1:
                break
            try:
                result, index = decoder.raw_decode(text[match:])
                yield result
                pos = match + index
            except ValueError:
                pos = match + 1

    @staticmethod
    def _get_hashed_password(random, realm, username, password):
        password_str = f"{username}:{realm}:{password}"
        password_bytes = password_str.encode("utf-8")
        password_hash = hashlib.md5(password_bytes).hexdigest().upper()

        random_str = f"{username}:{random}:{password_hash}"
        random_bytes = random_str.encode("utf-8")
        random_hash = hashlib.md5(random_bytes).hexdigest().upper()

        return random_hash
