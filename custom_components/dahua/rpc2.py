"""
Dahua RPC2 API Client

Taken and modified and added to, from https://gist.github.com/gxfxyz/48072a72be3a169bc43549e676713201
"""
import hashlib
import json
import logging
import sys
import socket

import aiohttp

_LOGGER: logging.Logger = logging.getLogger(__package__)

if sys.version_info > (3, 0):
    unicode = str


class DahuaRpc2Client:
    def __init__(
            self,
            username: str,
            password: str,
            address: str,
            port: int,
            rtsp_port: int,
            session: aiohttp.ClientSession
    ) -> None:
        self._username = username
        self._password = password
        self._session = session
        self._rtsp_port = rtsp_port
        self._address = address
        self._base = "http://{0}:{1}".format(address, port)
        self._session_id = None
        self._id = 0

    async def request(self, method, params=None, object_id=None, extra=None, url=None, verify_result=True):
        """Make a RPC request."""
        self._id += 1
        data = {'method': method, 'id': self._id}
        if params is not None:
            data['params'] = params
        if object_id:
            data['object'] = object_id
        if extra is not None:
            data.update(extra)
        if self._session_id:
            data['session'] = self._session_id
        if not url:
            url = "{0}/RPC2".format(self._base)

        use_socket = True
        if use_socket:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.connect((self._address, 37777))
            s.sendall(b'Hello, world')
            data = s.recv(1024)
            _LOGGER.error("data=%s", data)
            s.close()
            # self._tcp_server.setblocking(0)
            # self._tcp_server.bind(('127.0.0.1', EventOutServerPort))
            # self._tcp_server.listen(10)

        resp = await self._session.post(url, data=json.dumps(data))
        resp_json = json.loads(await resp.text())

        if verify_result and resp_json['result'] is False:
            raise ConnectionError(str(resp))

        return resp_json

    async def login(self):
        """Dahua RPC login.
        Reversed from rpcCore.js (login, getAuth & getAuthByType functions).
        Also referenced:
        https://gist.github.com/avelardi/1338d9d7be0344ab7f4280618930cd0d
        """

        # login1: get session, realm & random for real login
        url = '{0}/RPC2_Login'.format(self._base)
        method = "global.login"
        params = {'userName': self._username,
                  'password': "",
                  'clientType': "Dahua3.0-Web3.0"}
        _LOGGER.debug("Attempting login with URL %s", url)
        r = await self.request(method=method, params=params, url=url, verify_result=False)
        _LOGGER.debug("Got response: %s", r)

        self._session_id = r['session']
        realm = r['params']['realm']
        random = r['params']['random']

        # Password encryption algorithm. Reversed from rpcCore.getAuthByType
        pwd_phrase = self._username + ":" + realm + ":" + self._password
        if isinstance(pwd_phrase, unicode):
            pwd_phrase = pwd_phrase.encode('utf-8')
        pwd_hash = hashlib.md5(pwd_phrase).hexdigest().upper()
        pass_phrase = self._username + ':' + random + ':' + pwd_hash
        if isinstance(pass_phrase, unicode):
            pass_phrase = pass_phrase.encode('utf-8')
        pass_hash = hashlib.md5(pass_phrase).hexdigest().upper()

        # login2: the real login
        params = {'userName': self._username,
                  'password': pass_hash,
                  'clientType': "Dahua3.0-Web3.0",
                  'authorityType': "Default",
                  'passwordType': "Default"}
        _LOGGER.debug("Login phase 2: %s", params)
        r = await self.request(method=method, params=params, url=url)
        _LOGGER.debug("Got response 2: %s", r)
        return r

    async def logout(self) -> bool:
        """Logs out of the current session. Returns true if the logout was successful"""
        try:
            response = await self.request(method="global.logout")
            if response['result'] is True:
                _LOGGER.debug("Logged out of Dahua device %s", self._base)
                return True
            else:
                _LOGGER.debug("Failed to log out of Dahua device %s", self._base)
                return False
        except Exception as exception:
            return False

    async def current_time(self):
        """Get the current time on the device."""
        response = await self.request(method="global.getCurrentTime")
        return response['params']['time']

    async def get_serial_number(self) -> str:
        """Gets the serial number of the device."""
        response = await self.request(method="magicBox.getSerialNo")
        return response['params']['sn']

    async def get_config(self, params):
        """Gets config for the supplied params """
        method = "configManager.getConfig"
        response = await self.request(method=method, params=params)
        return response['params']

    async def get_device_name(self) -> str:
        """Get the device name"""
        data = await self.get_config({"name": "General"})
        return data["table"]["MachineName"]
