"""
Copied and modified from https://github.com/elad-bar/DahuaVTO2MQTT
Thanks to @elad-bar
"""

from __future__ import annotations

import struct
import sys
import logging
import json
import asyncio
import hashlib
from collections.abc import Callable, Generator
from json import JSONDecoder
from typing import Any

PROTOCOLS = {True: "https", False: "http"}

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
    realm: str | None
    random: str | None
    messages: list[Any]
    dahua_details: dict[str, Any]
    base_url: str
    hold_time: int
    lock_status: dict[str, Any]
    data_handlers: dict[int, Callable[..., None]]
    buffer: bytearray

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        is_ssl: bool,
        on_receive_vto_event: Callable[[dict[str, Any]], None],
    ) -> None:
        self.dahua_details: dict[str, Any] = {}
        self.host = host
        self.username = username
        self.password = password
        self.is_ssl = is_ssl
        self.base_url = f"{PROTOCOLS[self.is_ssl]}://{self.host}/cgi-bin/"
        self.realm: str | None = None
        self.random: str | None = None
        self.request_id = 1
        self.sessionId = 0
        self.keep_alive_interval = 0
        self.transport: asyncio.Transport | None = None
        self.hold_time = 0
        self.lock_status: dict[str, Any] = {}
        self.data_handlers: dict[int, Callable[..., None]] = {}
        self.buffer = bytearray()

        self._keep_alive_handle: asyncio.TimerHandle | None = None

        # This is the hook back into HA
        self.on_receive_vto_event = on_receive_vto_event
        self._loop = asyncio.get_event_loop()
        self.disconnected: asyncio.Future[bool] = self._loop.create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        _LOGGER.debug("VTO connection established")

        try:
            self.transport = transport  # type: ignore[assignment]
            self.pre_login()

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            assert exc_tb is not None
            _LOGGER.error(
                f"Failed to handle message, error: {ex}, Line: {exc_tb.tb_lineno}"
            )

    def data_received(self, data: bytes) -> None:
        _LOGGER.debug("Event data %s: '%s'", self.host, data)

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

                    message_id: int = message.get("id")  # type: ignore[assignment]

                    handler: Callable[..., None] = self.data_handlers.get(
                        message_id, self.handle_default
                    )
                    handler(message)
            except Exception as ex:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                assert exc_tb is not None
                _LOGGER.error(
                    f"Failed to handle message, error: {ex}, Line: {exc_tb.tb_lineno}"
                )

    def handle_notify_event_stream(self, params: dict[str, Any] | None) -> None:
        try:
            if params is None:
                return
            event_list: list[Any] = params.get("eventList")  # type: ignore[assignment]

            for message in event_list:
                for k in self.dahua_details:
                    if k in DAHUA_ALLOWED_DETAILS:
                        message[k] = self.dahua_details.get(k)

                self.on_receive_vto_event(message)

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            assert exc_tb is not None
            _LOGGER.error(
                f"Failed to handle event, error: {ex}, Line: {exc_tb.tb_lineno}"
            )

    def handle_default(self, message: dict[str, Any]) -> None:
        _LOGGER.info(f"Data received without handler: {message}")

    def eof_received(self) -> None:
        _LOGGER.info("Server sent EOF message")

        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        if not self.disconnected.done():
            self.disconnected.set_result(True)

    def connection_lost(self, exc: Exception | None) -> None:
        _LOGGER.error("server closed the connection")

        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        if not self.disconnected.done():
            self.disconnected.set_result(True)

    def send(
        self,
        action: str,
        handler: Callable[[dict[str, Any] | None], None],
        params: dict[str, Any] | None = None,
    ) -> None:
        if params is None:
            params = {}

        self.request_id += 1

        message_data: dict[str, Any] = {
            "id": self.request_id,
            "session": self.sessionId,
            "magic": "0x1234",
            "method": action,
            "params": params,
        }

        self.data_handlers[self.request_id] = handler

        assert self.transport is not None
        if not self.transport.is_closing():
            message = self.convert_message(message_data)

            self.transport.write(message)

    @staticmethod
    def convert_message(data: dict[str, Any]) -> bytes:
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

    def pre_login(self) -> None:
        _LOGGER.debug("Prepare pre-login message")

        def handle_pre_login(message: dict[str, Any] | None) -> None:
            if message is None:
                return
            error = message.get("error")
            params = message.get("params")

            if error is not None:
                error_message = error.get("message")

                if error_message == "Component error: login challenge!":
                    self.random = params.get("random")  # type: ignore[union-attr]
                    self.realm = params.get("realm")  # type: ignore[union-attr]
                    self.sessionId = message.get("session")  # type: ignore[assignment]

                    self.login()

        request_data: dict[str, Any] = {
            "clientType": "",
            "ipAddr": "(null)",
            "loginType": "Direct",
            "userName": self.username,
            "password": "",
        }

        self.send(DAHUA_GLOBAL_LOGIN, handle_pre_login, request_data)

    def login(self) -> None:
        _LOGGER.debug("Prepare login message")

        def handle_login(message: dict[str, Any] | None) -> None:
            if message is None:
                return
            params: dict[str, Any] = message.get("params")  # type: ignore[assignment]
            keep_alive_interval = params.get("keepAliveInterval")

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

        assert self.random is not None
        assert self.realm is not None
        password = self._get_hashed_password(
            self.random, self.realm, self.username, self.password
        )

        request_data: dict[str, Any] = {
            "clientType": "",
            "ipAddr": "(null)",
            "loginType": "Direct",
            "userName": self.username,
            "password": password,
            "authorityType": "Default",
        }

        self.send(DAHUA_GLOBAL_LOGIN, handle_login, request_data)

    def attach_event_manager(self) -> None:
        _LOGGER.info("Attach event manager")

        def handle_attach_event_manager(message: dict[str, Any] | None) -> None:
            if message is None:
                return
            method = message.get("method")
            params = message.get("params")

            if method == "client.notifyEventStream":
                self.handle_notify_event_stream(params)

        request_data: dict[str, Any] = {"codes": ["All"]}

        self.send(DAHUA_EVENT_MANAGER_ATTACH, handle_attach_event_manager, request_data)

    def load_access_control(self) -> None:
        _LOGGER.info("Get access control configuration")

        def handle_access_control(message: dict[str, Any] | None) -> None:
            if message is None:
                return

            params: dict[str, Any] = message.get("params")  # type: ignore[assignment]
            table = params.get("table")

            if table is not None:
                for item in table:
                    access_control = item.get("AccessProtocol")

                    if access_control == "Local":
                        self.hold_time = item.get("UnlockReloadInterval")

                        _LOGGER.info(f"Hold time: {self.hold_time}")

        request_data: dict[str, Any] = {"name": "AccessControl"}

        self.send(DAHUA_CONFIG_MANAGER_GETCONFIG, handle_access_control, request_data)

    async def cancel_call(self) -> bool:
        _LOGGER.info("Cancelling call on VTO")

        def cancel(message: dict[str, Any] | None) -> None:
            _LOGGER.info(f"Got cancel call response: {message}")

        self.send("console.runCmd", cancel, {"command": "hc"})
        return True

    def load_version(self) -> None:
        _LOGGER.info("Get version")

        def handle_version(message: dict[str, Any] | None) -> None:
            if message is None:
                return

            params: dict[str, Any] = message.get("params")  # type: ignore[assignment]
            version_details = params.get("version", {})
            build_date = version_details.get("BuildDate")
            version = version_details.get("Version")

            self.dahua_details[DAHUA_VERSION] = version
            self.dahua_details[DAHUA_BUILD_DATE] = build_date

            _LOGGER.info(f"Version: {version}, Build Date: {build_date}")

        self.send(DAHUA_MAGICBOX_GETSOFTWAREVERSION, handle_version)

    def load_device_type(self) -> None:
        _LOGGER.info("Get device type")

        def handle_device_type(message: dict[str, Any] | None) -> None:
            if message is None:
                return

            params: dict[str, Any] = message.get("params")  # type: ignore[assignment]
            device_type = params.get("type")

            self.dahua_details[DAHUA_DEVICE_TYPE] = device_type

            _LOGGER.info(f"Device Type: {device_type}")

        self.send(DAHUA_MAGICBOX_GETDEVICETYPE, handle_device_type)

    def load_serial_number(self) -> None:
        _LOGGER.info("Get serial number")

        def handle_serial_number(message: dict[str, Any] | None) -> None:
            if message is None:
                return

            params: dict[str, Any] = message.get("params")  # type: ignore[assignment]
            table = params.get("table", {})
            serial_number = table.get("UUID")

            self.dahua_details[DAHUA_SERIAL_NUMBER] = serial_number

            _LOGGER.info(f"Serial Number: {serial_number}")

        request_data: dict[str, Any] = {"name": "T2UServer"}

        self.send(DAHUA_CONFIG_MANAGER_GETCONFIG, handle_serial_number, request_data)

    def keep_alive(self) -> None:
        _LOGGER.debug("Keep alive")

        def handle_keep_alive(message: dict[str, Any] | None) -> None:
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

        request_data: dict[str, Any] = {
            "timeout": self.keep_alive_interval,
            "action": True,
        }

        self.send(DAHUA_GLOBAL_KEEPALIVE, handle_keep_alive, request_data)

    @staticmethod
    def parse_response(response: bytes | bytearray) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        try:
            # Messages can look like like the following, note, this was shorted with ...
            # Note that there can 0 or more events per line. Typically it's 1 event, but sometimes 2 events will arrive.
            # This example shows 2 events
            # \x00\x00\x00DHIP*Q\xa8f\x08\x00\x00\x00m\x04\x00\x00\x00\x00\x00\x00m\x04\x00\x00\x00\x00\x00\x00{"id":8,"method":"client.notifyEventStream","params":{"SID":513,"eventList":[{"Action":"Start","Code":"CrossRegionDetection"...},"session":1722306858}\n \x00\x00\x00DHIP*Q\xa8f\x08\x00\x00\x00\xe8\x00\x00\x00\x00\x00\x00\x00\xe8\x00\x00\x00\x00\x00\x00\x00{"id":8,"method":"client.notifyEventStream","params":{"SID":513,"eventList":[{"Action":"Pulse","Code":"IntelliFrame",..."session":1722306858}\n'
            # Another example
            # \x00\x00\x00DHIP\x8c-\x96{\x08\x00\x00\x00{\x01\x00\x00\x00\x00\x00\x00{\x01\x00\x00\x00\x00\x00\x00{"id":8,"method":"client.notifyEventStream","params":{"SID":513,"eventList":[{"Action":"State","Code":"VideoMotionInfo","Data":[{"Id":0,"Region":[4194303,4194303,4128767,3997695,3801087,3801087,3932159,3407871,3932159,3932158,3932156,3735548,3678204,2101244,2047,2097663,3146239,524799],"RegionName":"Region1","State":"Active","Threshold":54}],"Index":0}]},"session":1722306858}\n

            data = str(response)

            jsons = DahuaVTOClient.extract_json_objects(data)
            for j in jsons:
                result.append(j)
            return result
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            assert exc_tb is not None
            _LOGGER.error(
                f"Failed to read data: {response!r}, error: {e}, Line: {exc_tb.tb_lineno}"
            )

        return result

    @staticmethod
    def extract_json_objects(
        text: str, decoder: json.JSONDecoder = JSONDecoder()
    ) -> Generator[dict[str, Any], None, None]:
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
    def _get_hashed_password(
        random: str, realm: str, username: str, password: str
    ) -> str:
        password_str = f"{username}:{realm}:{password}"
        password_bytes = password_str.encode("utf-8")
        password_hash = hashlib.md5(password_bytes).hexdigest().upper()

        random_str = f"{username}:{random}:{password_hash}"
        random_bytes = random_str.encode("utf-8")
        random_hash = hashlib.md5(random_bytes).hexdigest().upper()

        return random_hash
