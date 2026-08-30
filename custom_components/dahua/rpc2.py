"""
Dahua RPC2 API Client

Auth taken and modified and added to, from https://gist.github.com/gxfxyz/48072a72be3a169bc43549e676713201
"""
import hashlib
import json
import logging
import sys

import aiohttp
from custom_components.dahua.models import CoaxialControlIOStatus

_LOGGER: logging.Logger = logging.getLogger(__package__)
_PARAMS_UNSET = object()

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
        self._session_id = None
        self._ptz_objects: dict[int, int] = {}
        self._id = 0
        protocol = "https" if int(port) == 443 else "http"
        self._base = "{0}://{1}:{2}".format(protocol, address, port)

    async def request(self, method, params=_PARAMS_UNSET, object_id=None, extra=None, url=None, verify_result=True):
        """Make an RPC request."""
        self._id += 1
        data = {'method': method, 'id': self._id}
        if params is not _PARAMS_UNSET:
            data['params'] = params
        if object_id:
            data['object'] = object_id
        if extra is not None:
            data.update(extra)
        if self._session_id:
            data['session'] = self._session_id
        if not url:
            url = "{0}/RPC2".format(self._base)

        resp = await self._session.post(url, json=data)
        resp_json = json.loads(await resp.text())

        if verify_result and resp_json['result'] is False:
            error = resp_json.get("error")
            details = []
            if isinstance(error, dict):
                if error.get("code") is not None:
                    details.append("code={0}".format(error["code"]))
                if isinstance(error.get("message"), str):
                    message = error["message"].replace("\r", " ").replace("\n", " ")
                    details.append("message={0}".format(message[:200]))
            suffix = " ({0})".format(", ".join(details)) if details else ""
            raise ConnectionError(
                "Dahua RPC2 method {0} returned result=false{1}".format(
                    method, suffix
                )
            )

        return resp_json

    async def login(self):
        """Dahua RPC login.
        Reversed from rpcCore.js (login, getAuth & getAuthByType functions).
        Also referenced:
        https://gist.github.com/avelardi/1338d9d7be0344ab7f4280618930cd0d
        """

        # login1: get session, realm & random for real login
        self._session_id = None
        self._ptz_objects.clear()
        self._id = 0
        url = '{0}/RPC2_Login'.format(self._base)
        method = "global.login"
        params = {'userName': self._username,
                  'password': "",
                  'clientType': "Web5.0"}
        r = await self.request(
            method=method, params=params, url=url, verify_result=False
        )

        self._session_id = r['session']
        realm = r['params']['realm']
        random = r['params']['random']
        authority_type = r['params'].get('encryption') or "Default"

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
                  'clientType': "Web5.0",
                  'realm': realm,
                  'random': random,
                  'passwordType': "Default",
                  'authorityType': authority_type}
        response = await self.request(method=method, params=params, url=url)
        authenticated_session = response.get('session')
        if not isinstance(authenticated_session, str) or not authenticated_session:
            raise ConnectionError(
                "Dahua RPC2 authenticated login response is missing session"
            )
        self._session_id = authenticated_session
        _LOGGER.debug("RPC2 login succeeded")
        return response

    async def logout(self) -> bool:
        """Logs out of the current session. Returns true if the logout was successful"""
        if not self._session_id:
            self._ptz_objects.clear()
            return True
        try:
            response = await self.request(method="global.logout")
            if response['result'] is True:
                _LOGGER.debug("RPC2 logout succeeded")
                return True
            _LOGGER.debug("RPC2 logout reported result=false")
            return False
        except Exception:
            _LOGGER.debug("RPC2 logout failed", exc_info=True)
            return False
        finally:
            self._session_id = None
            self._ptz_objects.clear()

    async def async_get_ptz_object(self, channel: int) -> int:
        """Return the session-scoped PTZ object for one logical channel."""
        if channel in self._ptz_objects:
            return self._ptz_objects[channel]
        if not self._session_id:
            await self.login()
        response = await self.request(
            method="ptz.factory.instance",
            params={"channel": channel},
        )
        object_id = response.get("result")
        if (
            isinstance(object_id, bool)
            or not isinstance(object_id, int)
            or object_id <= 0
        ):
            raise ConnectionError("Dahua RPC2 returned an invalid PTZ object")
        self._ptz_objects[channel] = object_id
        _LOGGER.debug("RPC2 ptz.factory.instance succeeded channel=%d", channel)
        return object_id

    async def async_goto_preset_position(self, channel: int, position: int) -> dict:
        """Move to a preset using the exact Dahua RPC2 GotoPreset contract."""
        object_id = await self.async_get_ptz_object(channel)
        response = await self.request(
            method="ptz.start",
            object_id=object_id,
            params={
                "code": "GotoPreset",
                "arg1": position,
                "arg2": 0,
                "arg3": 0,
            },
        )
        _LOGGER.debug(
            "RPC2 GotoPreset succeeded channel=%d preset_id=%d",
            channel,
            position,
        )
        return response

    async def async_get_ptz_presets(self, channel: int) -> list[dict]:
        """Return the firmware's real presets for one dynamic PTZ object."""
        object_id = await self.async_get_ptz_object(channel)
        response = await self.request(
            method="ptz.getPresets",
            object_id=object_id,
            params=None,
        )
        params = response.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("presets"), list):
            raise ValueError("Dahua RPC2 response is missing params.presets")
        presets = params["presets"]
        _LOGGER.debug(
            "RPC2 ptz.getPresets succeeded channel=%d preset_count=%d",
            channel,
            len(presets),
        )
        return presets

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
        response = await self.request(method="configManager.getConfig", params=params)
        return response['params']

    async def get_device_name(self) -> str:
        """Get the device name"""
        data = await self.get_config({"name": "General"})
        return data["table"]["MachineName"]

    async def get_coaxial_control_io_status(self, channel: int) -> CoaxialControlIOStatus:
        """ async_get_coaxial_control_io_status returns the the current state of the speaker and white light. """
        response = await self.request(method="CoaxialControlIO.getStatus", params={"channel": channel})
        return CoaxialControlIOStatus(response)
