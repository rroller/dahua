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
from custom_components.dahua.ivs import ivs_rule_index

_LOGGER: logging.Logger = logging.getLogger(__package__)
_PARAMS_UNSET = object()

if sys.version_info > (3, 0):
    unicode = str


class Rpc2MethodRefused(ConnectionError):
    """The device answered an RPC2 call with result=false.

    A ConnectionError subclass so existing handlers keep working, but a
    distinct type because it means something quite different: the transport
    reached the device and the device declined this particular method or
    config table. That is evidence RPC2 *works* here, not that it does not.
    """

    def __init__(self, description: str, code=None, message=None):
        super().__init__(description)
        self.code = code
        self.message = message


def refusal_reason(response: dict) -> tuple:
    """Where a device put its reason for answering `result: false`.

    Two shapes are in use and only one was being read. Most refusals carry a
    nested object, which is where `code` and `message` were taken from:

        {"result": false, "error": {"code": 268959743, "message": "Unknown error!"}}

    Some carry the reason at the top level instead, with no `error` object at
    all. Measured on a DHI-NVR5464 answering a 17KB `configManager.setConfig`:

        {"result": false, "errCode": 287638033, "message": "Request length error!"}

    Reading only the nested shape discarded that, so a refusal that explained
    itself precisely was reported as "returned result=false" and nothing more,
    and the reason had to be recovered by printing the raw response by hand.

    Nested is preferred when present, so nothing about the first shape changes.
    """
    error = response.get("error")
    if isinstance(error, dict):
        return error.get("code"), error.get("message")
    return response.get("errCode"), response.get("message")


class DahuaRpc2Client:
    def __init__(
        self,
        username: str,
        password: str,
        address: str,
        port: int,
        rtsp_port: int,
        session: aiohttp.ClientSession,
        use_https: bool = None,
    ) -> None:
        self._username = username
        self._password = password
        self._session = session
        self._rtsp_port = rtsp_port
        self._session_id = None
        self._ptz_objects: dict[int, int] = {}
        self._id = 0
        if use_https is None:
            use_https = int(port) == 443
        self._use_https = use_https
        protocol = "https" if use_https else "http"
        self._base = "{0}://{1}:{2}".format(protocol, address, port)

    async def request(
        self,
        method,
        params=_PARAMS_UNSET,
        object_id=None,
        extra=None,
        url=None,
        verify_result=True,
    ):
        """Make an RPC request."""
        self._id += 1
        data = {"method": method, "id": self._id}
        if params is not _PARAMS_UNSET:
            data["params"] = params
        if object_id:
            data["object"] = object_id
        if extra is not None:
            data.update(extra)
        if self._session_id:
            data["session"] = self._session_id
        if not url:
            url = "{0}/RPC2".format(self._base)

        resp = await self._session.post(url, json=data)
        try:
            resp_json = json.loads(await resp.text())
        except ValueError as error:
            # An HTML error page, a 503 body or a truncated answer is this read
            # not coming back, not "this device does not speak RPC2". Raised as
            # a connection error so rpc2_failure_is_permanent does not write the
            # transport off for the host after two of them.
            raise aiohttp.ClientConnectionError(
                "Dahua RPC2 answered with a body that is not JSON"
            ) from error

        if verify_result and resp_json["result"] is False:
            code, message = refusal_reason(resp_json)
            details = []
            if code is not None:
                details.append("code={0}".format(code))
            if isinstance(message, str) and message:
                display_message = message.replace("\r", " ").replace("\n", " ")
                details.append("message={0}".format(display_message[:200]))
            suffix = " ({0})".format(", ".join(details)) if details else ""
            raise Rpc2MethodRefused(
                "Dahua RPC2 method {0} returned result=false{1}".format(method, suffix),
                code=code,
                message=message,
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
        url = "{0}/RPC2_Login".format(self._base)
        method = "global.login"
        params = {"userName": self._username, "password": "", "clientType": "Web5.0"}
        r = await self.request(
            method=method, params=params, url=url, verify_result=False
        )

        self._session_id = r["session"]
        realm = r["params"]["realm"]
        random = r["params"]["random"]
        authority_type = r["params"].get("encryption") or "Default"

        # Password encryption algorithm. Reversed from rpcCore.getAuthByType
        pwd_phrase = self._username + ":" + realm + ":" + self._password
        if isinstance(pwd_phrase, unicode):
            pwd_phrase = pwd_phrase.encode("utf-8")
        pwd_hash = hashlib.md5(pwd_phrase).hexdigest().upper()
        pass_phrase = self._username + ":" + random + ":" + pwd_hash
        if isinstance(pass_phrase, unicode):
            pass_phrase = pass_phrase.encode("utf-8")
        pass_hash = hashlib.md5(pass_phrase).hexdigest().upper()

        # login2: the real login
        params = {
            "userName": self._username,
            "password": pass_hash,
            "clientType": "Web5.0",
            "realm": realm,
            "random": random,
            "passwordType": "Default",
            "authorityType": authority_type,
        }
        response = await self.request(method=method, params=params, url=url)
        authenticated_session = response.get("session")
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
            if response["result"] is True:
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
        return response["params"]["time"]

    async def get_serial_number(self) -> str:
        """Gets the serial number of the device."""
        response = await self.request(method="magicBox.getSerialNo")
        return response["params"]["sn"]

    async def get_config(self, params):
        """Gets config for the supplied params"""
        response = await self.request(method="configManager.getConfig", params=params)
        return response["params"]

    async def async_get_remote_ivs_rules(self, channel: int) -> list[dict]:
        """Read the complete rule table for one zero-based NVR channel."""
        if not self._session_id:
            await self.login()
        params = await self.get_config(
            {
                "name": "RemoteVideoAnalyseRule",
                "onlyLocal": False,
                "channel": channel,
            }
        )
        table = params.get("table")
        if not isinstance(table, list) or any(
            not isinstance(row, dict) for row in table
        ):
            raise ValueError(
                "Dahua RPC2 response is missing RemoteVideoAnalyseRule table"
            )
        return table

    async def async_set_remote_ivs_rule_by_id(
        self, channel: int, rule_id: str, enabled: bool
    ) -> None:
        """Resolve the rule from a fresh read, then write that one field.

        Writing the whole table back is refused by at least one recorder. A
        DHI-NVR5464 answers `errCode 287638033, "Request length error!"`, because
        the table for one channel is 9KB to 22KB depending on how many rules it
        holds: each row carries its own `EventHandler` and `TimeSection` and runs
        to 4.6KB on its own. The refusal applies in both directions, and to a
        write that changes nothing, so every switch on such a device was inert.

        Addressing the field instead makes the body 91 bytes. Measured on the
        same recorder: the disable is accepted, reads back `False`, and the
        restore reads back `True` with no other field altered.

        The read stays exactly as it was, and has to. It is what resolves
        `rule_id` to an index, the index is what addresses the row here, and the
        `Id` a rule reports differs between the whole-table and per-channel read
        shapes on this firmware, so substituting a cheaper read would silently
        write to the wrong rule.
        """
        from custom_components.dahua.client import flatten_rpc2_config

        table = await self.async_get_remote_ivs_rules(channel)
        flat = flatten_rpc2_config(
            "RemoteVideoAnalyseRule",
            table,
            f"table.RemoteVideoAnalyseRule[{channel}]",
        )
        index = ivs_rule_index(flat, channel, rule_id, "RemoteVideoAnalyseRule")
        if index is None:
            raise ValueError(
                f"Remote IVS rule {rule_id} is missing or ambiguous on channel {channel}"
            )
        await self.request(
            method="configManager.setConfig",
            params={
                "name": f"RemoteVideoAnalyseRule[{channel}][{index}].Enable",
                "table": enabled,
                "options": [],
                "channel": channel,
            },
        )

    async def set_configs(self, configs: list[tuple[str, list]]) -> dict:
        """Commit complete config tables together through system.multicall."""
        calls = []
        for name, table in configs:
            self._id += 1
            calls.append(
                {
                    "method": "configManager.setConfig",
                    "params": {"name": name, "table": table, "options": []},
                    "id": self._id,
                    "session": self._session_id,
                }
            )
        response = await self.request(method="system.multicall", params=calls)
        results = response.get("params")
        if (
            not isinstance(results, list)
            or len(results) != len(calls)
            or any(
                not isinstance(result, dict) or result.get("result") is not True
                for result in results
            )
        ):
            raise ConnectionError(
                "Dahua RPC2 system.multicall did not confirm every config write"
            )
        return response

    async def get_device_name(self) -> str:
        """Get the device name"""
        data = await self.get_config({"name": "General"})
        return data["table"]["MachineName"]

    async def get_product_definition(
        self, name: str | None = None
    ) -> dict | list | None:
        """Read one optional ProductDefinition block without inferring support."""
        response = await self.request(
            method="magicBox.getProductDefinition",
            params={"name": name} if name is not None else _PARAMS_UNSET,
            verify_result=False,
        )
        if not isinstance(response, dict) or response.get("result") is not True:
            return None
        params = response.get("params")
        definition = params.get("definition") if isinstance(params, dict) else None
        if isinstance(definition, dict):
            return definition
        if name == "LightingControlMulti" and isinstance(definition, list):
            return definition
        return None

    async def get_coaxial_control_io_caps(self, channel: int = 0) -> dict[str, bool]:
        """Read explicit deterrence capabilities; never infer them from status."""
        response = await self.request(
            method="CoaxialControlIO.getCaps", params={"channel": channel}
        )
        params = response.get("params")
        caps = params.get("caps") if isinstance(params, dict) else None
        if not isinstance(caps, dict):
            raise ValueError("Dahua RPC2 response is missing params.caps")
        return {
            key: caps.get(key) in (1, "1")
            for key in ("SupportControlSpeaker", "SupportControlLight")
        }

    async def set_coaxial_control_state(
        self, channel: int, dahua_type: int, enabled: bool, off_io: int = 2
    ) -> dict:
        """Control a directly connected camera's deterrence output."""
        return await self.request(
            method="CoaxialControlIO.control",
            params={
                "channel": channel,
                "info": [
                    {
                        "Type": dahua_type,
                        "IO": 1 if enabled else off_io,
                        "TriggerMode": 2,
                    }
                ],
            },
        )

    async def async_open_door(
        self, channel: int, door_index: int = 0, short_number: str = "HA"
    ) -> dict:
        """Open a door over RPC2, for VTOs with no accessControl CGI endpoint.

        Three calls, the way myhomeiot/DahuaVTO does it: an object from the
        factory, the action on that object, and destroy. The destroy is in a
        finally because the object is the device's, not ours -- leaking one on
        a doorbell is a real cost, and it must happen even when openDoor fails.

        `channel` is 0-based here, matching the factory's own convention.

        The caller, _async_open_door_rpc2, hands this a fresh client of its own,
        and nothing logged it in: the factory was asked without a session, which
        a device refuses. Every other RPC2 action here logs in first when it has
        no session (PTZ, vto_call), so this does too. Read from the code, not
        tried on hardware: trying it opens a door.
        """
        if not self._session_id:
            await self.login()
        made = await self.request(
            method="accessControl.factory.instance", params={"channel": channel}
        )
        object_id = made.get("result")
        if (
            isinstance(object_id, bool)
            or not isinstance(object_id, int)
            or object_id <= 0
        ):
            raise ConnectionError(
                "Dahua RPC2 accessControl.factory.instance returned no object"
            )
        try:
            return await self.request(
                method="accessControl.openDoor",
                object_id=object_id,
                params={"DoorIndex": door_index, "ShortNumber": short_number},
            )
        finally:
            try:
                await self.request(
                    method="accessControl.destroy",
                    object_id=object_id,
                    verify_result=False,
                )
            except Exception:  # pylint: disable=broad-except
                # Losing the door's result to a failed cleanup would be worse
                # than leaking the object, so this never raises.
                _LOGGER.debug("accessControl.destroy failed", exc_info=True)

    async def async_vto_call(self, number: str) -> dict:
        """Ring a room from a VTO, the way the VTO's own web page does.

        Read out of the web interface of a DHI-VTO2211G-WP-S2 on 4.810.0000000.0.R
        (the phone icon under Device Setting) and replayed against it:

            VideoTalkPhone.factory.instance   params null, result is the object
            VideoTalkPhone.beginCall          on that object

        `isTestCall: true` is added by that page to every call it makes. What it
        changes is not known: with it, the main monitor and both extensions of the
        room rang and showed the VTO's camera, the same as a press of the button.
        The object id came back as a plain integer.

        There is deliberately no destroy here, unlike openDoor. The web page
        destroys this object only once the call is over (endCall, then destroy),
        and destroying it straight after beginCall has not been tried: it may
        well hang up the call this exists to start. The caller logs out instead,
        which on that VTO left the call ringing.
        """
        if not self._session_id:
            await self.login()
        made = await self.request(method="VideoTalkPhone.factory.instance", params=None)
        object_id = made.get("result")
        if (
            isinstance(object_id, bool)
            or not isinstance(object_id, int)
            or object_id <= 0
        ):
            raise ConnectionError(
                "Dahua RPC2 VideoTalkPhone.factory.instance returned no object"
            )
        return await self.request(
            method="VideoTalkPhone.beginCall",
            object_id=object_id,
            params={"number": number, "type": "normal", "isTestCall": True},
        )

    async def get_coaxial_control_io_status(
        self, channel: int
    ) -> CoaxialControlIOStatus:
        """async_get_coaxial_control_io_status returns the the current state of the speaker and white light."""
        response = await self.request(
            method="CoaxialControlIO.getStatus", params={"channel": channel}
        )
        return CoaxialControlIOStatus(api_response=response)

    async def _async_get_privacy_mode_table(self) -> list:
        """Read the LeLensMask config table, logging in first if needed."""
        if not self._session_id:
            await self.login()
        params = await self.get_config({"name": "LeLensMask"})
        table = params.get("table")
        if not isinstance(table, list) or not table or not isinstance(table[0], dict):
            raise ValueError("Dahua RPC2 response is missing table for LeLensMask")
        return table

    async def async_get_privacy_mode(self) -> bool:
        """Return True if the lens privacy mask (LeLensMask) is enabled."""
        table = await self._async_get_privacy_mode_table()
        return bool(table[0].get("Enable", False))

    async def async_set_privacy_mode(self, enabled: bool) -> None:
        """Enable or disable the lens privacy mask (LeLensMask).

        The entry is read back and written with only Enable changed so the
        camera keeps its own TimeSection schedule.
        """
        table = await self._async_get_privacy_mode_table()
        entry = dict(table[0])
        entry["Enable"] = enabled
        await self.request(
            method="configManager.setConfig",
            params={"name": "LeLensMask", "table": [entry], "options": []},
        )
        _LOGGER.debug("RPC2 LeLensMask set to Enable=%s", enabled)
