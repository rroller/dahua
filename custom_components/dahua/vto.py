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
from threading import Timer
from typing import Optional, Callable
from requests.auth import HTTPDigestAuth

PROTOCOLS = {
    True: "https",
    False: "http"
}

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

DAHUA_ALLOWED_DETAILS = [
    DAHUA_DEVICE_TYPE,
    DAHUA_SERIAL_NUMBER
]


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
    auth: HTTPDigestAuth
    data_handlers: {}

    def __init__(self, host: str, username: str, password: str, is_ssl: bool, on_receive_vto_event):
        self.dahua_details = {}
        self.host = host
        self.username = username
        self.password = password
        self.is_ssl = is_ssl
        self.base_url = f"{PROTOCOLS[self.is_ssl]}://{self.host}/cgi-bin/"
        self.auth = HTTPDigestAuth(self.username, self.password)
        self.realm = None
        self.random = None
        self.request_id = 1
        self.sessionId = 0
        self.keep_alive_interval = 0
        self.transport = None
        self.hold_time = 0
        self.lock_status = {}
        self.data_handlers = {}

        # This is the hook back into HA
        self.on_receive_vto_event = on_receive_vto_event
        self._loop = asyncio.get_event_loop()

    def connection_made(self, transport):
        _LOGGER.debug("VTO connection established")

        try:
            self.transport = transport
            self.pre_login()

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to handle message, error: {ex}, Line: {exc_tb.tb_lineno}")

    def data_received(self, data):
        _LOGGER.debug(f"Event data {self.host}: '{data}'")
        try:
            messages = self.parse_response(data)
            for message in messages:
                message_id = message.get("id")

                handler: Callable = self.data_handlers.get(message_id, self.handle_default)
                handler(message)
        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to handle message, error: {ex}, Line: {exc_tb.tb_lineno}")

    def handle_notify_event_stream(self, params):
        try:
            event_list = params.get("eventList")

            for message in event_list:
                for k in self.dahua_details:
                    if k in DAHUA_ALLOWED_DETAILS:
                        message[k] = self.dahua_details.get(k)

                self.on_receive_vto_event(message)

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to handle event, error: {ex}, Line: {exc_tb.tb_lineno}")

    def handle_default(self, message):
        _LOGGER.info(f"Data received without handler: {message}")

    def eof_received(self):
        _LOGGER.info('Server sent EOF message')

        self._loop.stop()

    def connection_lost(self, exc):
        _LOGGER.error('server closed the connection')

        self._loop.stop()

    def send(self, action, handler, params=None):
        if params is None:
            params = {}

        self.request_id += 1

        message_data = {
            "id": self.request_id,
            "session": self.sessionId,
            "magic": "0x1234",
            "method": action,
            "params": params
        }

        self.data_handlers[self.request_id] = handler

        if not self.transport.is_closing():
            message = self.convert_message(message_data)

            self.transport.write(message)

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
            error = message.get("error")
            params = message.get("params")

            if error is not None:
                error_message = error.get("message")

                if error_message == "Component error: login challenge!":
                    self.random = params.get("random")
                    self.realm = params.get("realm")
                    self.sessionId = message.get("session")

                    self.login()

        request_data = {
            "clientType": "",
            "ipAddr": "(null)",
            "loginType": "Direct",
            "userName": self.username,
            "password": ""
        }

        self.send(DAHUA_GLOBAL_LOGIN, handle_pre_login, request_data)

    def login(self):
        _LOGGER.debug("Prepare login message")

        def handle_login(message):
            params = message.get("params")
            keep_alive_interval = params.get("keepAliveInterval")

            if keep_alive_interval is not None:
                self.keep_alive_interval = keep_alive_interval - 5

                self.load_access_control()
                self.load_version()
                self.load_serial_number()
                self.load_device_type()
                self.attach_event_manager()

                Timer(self.keep_alive_interval, self.keep_alive).start()

        password = self._get_hashed_password(self.random, self.realm, self.username, self.password)

        request_data = {
            "clientType": "",
            "ipAddr": "(null)",
            "loginType": "Direct",
            "userName": self.username,
            "password": password,
            "authorityType": "Default"
        }

        self.send(DAHUA_GLOBAL_LOGIN, handle_login, request_data)

    def attach_event_manager(self):
        _LOGGER.info("Attach event manager")

        def handle_attach_event_manager(message):
            method = message.get("method")
            params = message.get("params")

            if method == "client.notifyEventStream":
                self.handle_notify_event_stream(params)

        request_data = {
            "codes": ['All']
        }

        self.send(DAHUA_EVENT_MANAGER_ATTACH, handle_attach_event_manager, request_data)

    def load_access_control(self):
        _LOGGER.info("Get access control configuration")

        def handle_access_control(message):
            params = message.get("params")
            table = params.get("table")

            if table is not None:
                for item in table:
                    access_control = item.get('AccessProtocol')

                    if access_control == 'Local':
                        self.hold_time = item.get('UnlockReloadInterval')

                        _LOGGER.info(f"Hold time: {self.hold_time}")

        request_data = {
            "name": "AccessControl"
        }

        self.send(DAHUA_CONFIG_MANAGER_GETCONFIG, handle_access_control, request_data)

    async def cancel_call(self):
        _LOGGER.info("Cancelling call on VTO")

        def cancel(message):
            _LOGGER.info(f"Got cancel call response: {message}")

        self.send("console.runCmd", cancel, {"command": "hc"})
        return True

    def load_version(self):
        _LOGGER.info("Get version")

        def handle_version(message):
            params = message.get("params")
            version_details = params.get("version", {})
            build_date = version_details.get("BuildDate")
            version = version_details.get("Version")

            self.dahua_details[DAHUA_VERSION] = version
            self.dahua_details[DAHUA_BUILD_DATE] = build_date

            _LOGGER.info(f"Version: {version}, Build Date: {build_date}")

        self.send(DAHUA_MAGICBOX_GETSOFTWAREVERSION, handle_version)

    def load_device_type(self):
        _LOGGER.info("Get device type")

        def handle_device_type(message):
            params = message.get("params")
            device_type = params.get("type")

            self.dahua_details[DAHUA_DEVICE_TYPE] = device_type

            _LOGGER.info(f"Device Type: {device_type}")

        self.send(DAHUA_MAGICBOX_GETDEVICETYPE, handle_device_type)

    def load_serial_number(self):
        _LOGGER.info("Get serial number")

        def handle_serial_number(message):
            params = message.get("params")
            table = params.get("table", {})
            serial_number = table.get("UUID")

            self.dahua_details[DAHUA_SERIAL_NUMBER] = serial_number

            _LOGGER.info(f"Serial Number: {serial_number}")

        request_data = {
            "name": "T2UServer"
        }

        self.send(DAHUA_CONFIG_MANAGER_GETCONFIG, handle_serial_number, request_data)

    def keep_alive(self):
        _LOGGER.debug("Keep alive")

        def handle_keep_alive(message):
            Timer(self.keep_alive_interval, self.keep_alive).start()

        request_data = {
            "timeout": self.keep_alive_interval,
            "action": True
        }

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

            data = str(response)

            jsons = DahuaVTOClient.extract_json_objects(data)
            for j in jsons:
                result.append(j)
            return result
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            _LOGGER.error(f"Failed to read data: {response}, error: {e}, Line: {exc_tb.tb_lineno}")

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
            match = text.find('{', pos)
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
        password_bytes = password_str.encode('utf-8')
        password_hash = hashlib.md5(password_bytes).hexdigest().upper()

        random_str = f"{username}:{random}:{password_hash}"
        random_bytes = random_str.encode('utf-8')
        random_hash = hashlib.md5(random_bytes).hexdigest().upper()

        return random_hash
