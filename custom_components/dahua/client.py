"""Dahua API Client."""
import logging
import socket
import asyncio
import aiohttp
import async_timeout

from .digest import DigestAuth
from hashlib import md5

_LOGGER: logging.Logger = logging.getLogger(__package__)

TIMEOUT_SECONDS = 20
SECURITY_LIGHT_TYPE = 1
SIREN_TYPE = 2


class DahuaClient:
    """
    DahuaClient is the client for accessing Dahua IP Cameras. The APIs were discovered from the "API of HTTP Protocol Specification" V2.76 2019-07-25 document
    and from inspecting the camera's HTTP UI request/responses.

    events is the list of events used to monitor on the camera (For example, motion detection)
    """

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
        self._address = address
        self._session = session
        self._port = port
        self._rtsp_port = rtsp_port

        protocol = "https" if int(port) == 443 else "http"
        self._base = "{0}://{1}:{2}".format(protocol, address, port)

    def get_rtsp_stream_url(self, channel: int, subtype: int) -> str:
        """
        Returns the RTSP url for the supplied subtype (subtype is 0=Main stream, 1=Sub stream)
        """
        url = "rtsp://{0}:{1}@{2}:{3}/cam/realmonitor?channel={4}&subtype={5}".format(
            self._username,
            self._password,
            self._address,
            self._rtsp_port,
            channel,
            subtype,
        )
        if subtype == 3:
            url = "rtsp://{0}:{1}@{2}".format(
                self._username,
                self._password,
                self._address,
            )

        return url

    async def async_get_snapshot(self, channel_number: int) -> bytes:
        """
        Takes a snapshot of the camera and returns the binary jpeg data
        NOTE: channel_number is not the channel_index. channel_number is the index + 1
        so channel index 0 is channel number 1. Except for some older firmwares where channel
        and channel number are the same!
        """
        url = "/cgi-bin/snapshot.cgi?channel={0}".format(channel_number)
        return await self.get_bytes(url)

    async def async_get_system_info(self) -> dict:
        """
        Get system info data from the getSystemInfo API. Example response:

        appAutoStart=true
        deviceType=IPC-HDW5831R-ZE
        hardwareVersion=1.00
        processor=S3LM
        serialNumber=4X7C5A1ZAG21L3F
        updateSerial=IPC-HDW5830R-Z
        updateSerialCloudUpgrade=IPC-HDW5830R-Z:07:01:08:70:52:00:09:0E:03:00:04:8F0:00:00:00:00:00:02:00:00:600
        """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getSystemInfo")
        except aiohttp.ClientResponseError as e:
            not_hashed_id = "{0}_{1}_{2}_{3}".format(self._address, self._rtsp_port, self._username, self._password)
            unique_cam_id = md5(not_hashed_id.encode('UTF-8')).hexdigest()
            return {"serialNumber": unique_cam_id}

    async def get_device_type(self) -> dict:
        """
        getDeviceType returns the device type. Example response:
        type=IPC-HDW5831R-ZE
        ...
        Some cams might return...
        type=IP Camera
        """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getDeviceType")
        except aiohttp.ClientResponseError as e:
            return {"type": "Generic RTSP"}

    async def get_software_version(self) -> dict:
        """
        get_software_version returns the device software version (also known as the firmware version). Example response:
        version=2.800.0000016.0.R,build:2020-06-05
        """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getSoftwareVersion")
        except aiohttp.ClientResponseError as e:
            return {"version": "1.0"}

    async def get_machine_name(self) -> dict:
        """ get_machine_name returns the device name. Example response: name=FrontDoorCam """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getMachineName")
        except aiohttp.ClientResponseError as e:
            not_hashed_id = "{0}_{1}_{2}_{3}".format(self._address, self._rtsp_port, self._username, self._password)
            unique_cam_id = md5(not_hashed_id.encode('UTF-8')).hexdigest()
            return {"name": unique_cam_id}

    async def get_vendor(self) -> dict:
        """ get_vendor returns the vendor. Example response: vendor=Dahua """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getVendor")
        except aiohttp.ClientResponseError as e:
            return {"vendor": "Generic RTSP"}

    async def reboot(self) -> dict:
        """ Reboots the device """
        return await self.get("/cgi-bin/magicBox.cgi?action=reboot")

    async def get_max_extra_streams(self) -> int:
        """ get_max_extra_streams returns the max number of sub streams supported by the camera """
        try:
            result = await self.get("/cgi-bin/magicBox.cgi?action=getProductDefinition&name=MaxExtraStream")
            return int(result.get("table.MaxExtraStreams", "2"))
        except aiohttp.ClientResponseError as e:
            pass
        # If we can't fetch, just assume 2 since that's pretty standard
        return 3

    async def async_get_coaxial_control_io_status(self) -> dict:
        """
        async_get_coaxial_control_io_status returns the the current state of the speaker and white light.
        Note that the "white light" here seems to also work for cameras that have the red/blue flashing alarm light
        like the IPC-HDW3849HP-AS-PV.

        Example response:

        status.status.Speaker=Off
        status.status.WhiteLight=Off
        """
        url = "/cgi-bin/coaxialControlIO.cgi?action=getStatus&channel=1"
        return await self.get(url)

    async def async_get_lighting_v2(self) -> dict:
        """
        async_get_lighting_v2 will fetch the status of the camera light (also known as the illuminator)
        NOTE: this is not the same as the infrared (IR) light. This is the white visible light on the camera
        Not all cameras have this feature.

        Example response:
        table.Lighting_V2[0][2][0].Correction=50
        table.Lighting_V2[0][2][0].LightType=WhiteLight
        table.Lighting_V2[0][2][0].MiddleLight[0].Angle=50
        table.Lighting_V2[0][2][0].MiddleLight[0].Light=100
        table.Lighting_V2[0][2][0].Mode=Manual
        table.Lighting_V2[0][2][0].PercentOfMaxBrightness=100
        table.Lighting_V2[0][2][0].Sensitive=3
        """
        url = "/cgi-bin/configManager.cgi?action=getConfig&name=Lighting_V2"
        return await self.get(url)

    async def async_get_machine_name(self) -> dict:
        """
        async_get_lighting_v1 will fetch the status of the IR light (InfraRed light)

        Example response:
        table.General.MachineName=Cam4
        """
        url = "/cgi-bin/configManager.cgi?action=getConfig&name=General.MachineName"
        try:
            return await self.get(url)
        except aiohttp.ClientResponseError as e:
            not_hashed_id = "{0}_{1}_{2}_{3}".format(self._address, self._rtsp_port, self._username, self._password)
            unique_cam_id = md5(not_hashed_id.encode('UTF-8')).hexdigest()
            return {"table.General.MachineName": unique_cam_id}

    async def async_get_config(self, name) -> dict:
        """ async_get_config gets a config by name """
        # example name=Lighting[0][0]
        url = "/cgi-bin/configManager.cgi?action=getConfig&name={0}".format(name)
        try:
            return await self.get(url)
        except aiohttp.ClientResponseError as e:
            return {}

    async def async_get_config_lighting(self, channel: int, profile_mode) -> dict:
        """
        async_get_config_lighting will fetch the status of the IR light (InfraRed light)
        profile_mode: = 0=day, 1=night, 2=normal scene

        Example response:
        table.Lighting[0][0].Correction=50
        table.Lighting[0][0].MiddleLight[0].Angle=50
        table.Lighting[0][0].MiddleLight[0].Light=50
        table.Lighting[0][0].Mode=Auto
        table.Lighting[0][0].Sensitive=3
        """
        try:
            return await self.async_get_config("Lighting[{0}][{1}]".format(channel, profile_mode))
        except aiohttp.ClientResponseError as e:
            if e.status == 400:
                # Some cams/dvrs/nvrs might not support this option.
                # We'll just return an empty response to not break the integration.
                return {}
            raise e

    async def async_get_config_motion_detection(self) -> dict:
        """
        async_get_config_motion_detection will fetch the motion detection status (enabled or not)
        Example response:
        table.MotionDetect[0].DetectVersion=V3.0
        table.MotionDetect[0].Enable=true
        """
        try:
            return await self.async_get_config("MotionDetect")
        except aiohttp.ClientResponseError as e:
            return {"table.MotionDetect[0].Enable": "false"}

    async def async_get_video_analyse_rules_for_amcrest(self):
        """
        returns the VideoAnalyseRule and if they are enabled or not.
        Example output:
          table.VideoAnalyseRule[0][0].Enable=false
        """
        try:
            return await self.async_get_config("VideoAnalyseRule[0][0].Enable")
        except aiohttp.ClientResponseError as e:
            return {"table.VideoAnalyseRule[0][0].Enable": "false"}

    async def async_get_ivs_rules(self):
        """
        returns the IVS rules and if they are enabled or not. [0][1] means channel 0, rule 1
        table.VideoAnalyseRule[0][1].Enable=true
        table.VideoAnalyseRule[0][1].Name=IVS-1
        """
        return await self.async_get_config("VideoAnalyseRule")

    async def async_set_all_ivs_rules(self, channel: int, enabled: bool):
        """
        Sets all IVS rules to enabled or disabled
        """
        rules = await self.async_get_ivs_rules()
        # Supporting up to a max of 11 rules. Just because 11 seems like a high enough number
        rules_set = []
        for index in range(10):
            rule = "table.VideoAnalyseRule[{0}][{1}].Enable".format(channel, index)
            if rule in rules:
                rules_set.append("VideoAnalyseRule[{0}][{1}].Enable={2}".format(channel, index, str(enabled).lower()))

        if len(rules_set) > 0:
            url = "/cgi-bin/configManager.cgi?action=setConfig&" + "&".join(rules_set)
            return await self.get(url, True)

    async def async_set_ivs_rule(self, channel: int, index: int, enabled: bool):
        """ Sets and IVS rules to enabled or disabled. This also works for Amcrest smart motion detection"""
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoAnalyseRule[{0}][{1}].Enable={2}".format(
            channel, index, str(enabled).lower()
        )
        return await self.get(url, True)

    async def async_enabled_smart_motion_detection(self, enabled: bool):
        """ Enables or disabled smart motion detection for Dahua devices (doesn't work for Amcrest)"""
        url = "/cgi-bin/configManager.cgi?action=setConfig&SmartMotionDetect[0].Enable={0}".format(str(enabled).lower())
        return await self.get(url, True)

    async def async_set_light_global_enabled(self, enabled: bool):
        """ Turns the blue ring light on/off for Amcrest doorbells """
        url = "/cgi-bin/configManager.cgi?action=setConfig&LightGlobal[0].Enable={0}".format(str(enabled).lower())
        return await self.get(url, True)

    async def async_get_smart_motion_detection(self) -> dict:
        """
        Gets the status of smart motion detection. Example output:
        table.SmartMotionDetect[0].Enable=true
        table.SmartMotionDetect[0].ObjectTypes.Human=true
        table.SmartMotionDetect[0].ObjectTypes.Vehicle=false
        table.SmartMotionDetect[0].Sensitivity=Middle
        """
        url = "/cgi-bin/configManager.cgi?action=getConfig&name=SmartMotionDetect"
        return await self.get(url)

    async def async_get_light_global_enabled(self) -> dict:
        """
        Returns the state of the Amcrest blue ring light (if it's on or off)
        Example output:
        table.LightGlobal[0].Enable=true
        """
        url = "/cgi-bin/configManager.cgi?action=getConfig&name=LightGlobal[0].Enable"
        return await self.get(url)

    async def async_set_lighting_v1(self, channel: int, enabled: bool, brightness: int) -> dict:
        """ async_get_lighting_v1 will turn the IR light (InfraRed light) on or off """
        # on = Manual, off = Off
        mode = "Manual"
        if not enabled:
            mode = "Off"
        return await self.async_set_lighting_v1_mode(channel, mode, brightness)

    async def async_set_lighting_v1_mode(self, channel: int, mode: str, brightness: int) -> dict:
        """
        async_set_lighting_v1_mode will set IR light (InfraRed light) mode and brightness
        Mode should be one of: Manual, Off, or Auto
        Brightness should be between 0 and 100 inclusive. 100 being the brightest
        """

        if mode.lower() == "on":
            mode = "Manual"
        # Dahua api expects the first char to be capital
        mode = mode.capitalize()

        url = "/cgi-bin/configManager.cgi?action=setConfig&Lighting[{channel}][0].Mode={mode}&Lighting[{channel}][0].MiddleLight[0].Light={brightness}".format(
            channel=channel, mode=mode, brightness=brightness
        )
        return await self.get(url)

    async def async_set_video_profile_mode(self, channel: int, mode: str):
        """
        async_set_video_profile_mode will set camera's profile mode to day or night
        Mode should be one of: Day or Night
        """

        if mode.lower() == "night":
            mode = "1"
        else:
            # Default to "day", which is 0
            mode = "0"

        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoInMode[{0}].Config[0]={1}".format(channel, mode)
        return await self.get(url, True)

    async def async_set_night_switch_mode(self, channel: int, mode: str):
        """
        async_set_night_switch_mode is the same as async_set_video_profile_mode when accessing the camera
        through a lorex NVR
        Mode should be one of: Day or Night
        """

        if mode.lower() == "night":
            mode = "3"
        else:
            # Default to "day", which is 0
            mode = "0"

        url = f"/cgi-bin/configManager.cgi?action=setConfig&VideoInOptions[{channel}].NightOptions.SwitchMode={mode}"
        _LOGGER.debug("Switching night mode: %s", url)
        return await self.get(url, True)

    async def async_enable_channel_title(self, channel: int, enabled: bool, ):
        """ async_set_enable_channel_title will enable or disables the camera's channel title overlay """
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[{0}].ChannelTitle.EncodeBlend={1}".format(
            channel, str(enabled).lower()
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could enable/disable channel title")

    async def async_enable_time_overlay(self, channel: int, enabled: bool):
        """ async_set_enable_time_overlay will enable or disables the camera's time overlay """
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[{0}].TimeTitle.EncodeBlend={1}".format(
            channel, str(enabled).lower()
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not enable/disable time overlay")

    async def async_enable_text_overlay(self, channel: int, group: int, enabled: bool):
        """ async_set_enable_text_overlay will enable or disables the camera's text overlay """
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[{0}].CustomTitle[{1}].EncodeBlend={2}".format(
            channel, group, str(enabled).lower()
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not enable/disable text overlay")

    async def async_enable_custom_overlay(self, channel: int, group: int, enabled: bool):
        """ async_set_enable_custom_overlay will enable or disables the camera's custom overlay """
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[{0}].UserDefinedTitle[{1}].EncodeBlend={2}".format(
            channel, group, str(enabled).lower()
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not enable/disable customer overlay")

    async def async_set_service_set_channel_title(self, channel: int, text1: str, text2: str):
        """ async_set_service_set_channel_title sets the channel title """
        text = '|'.join(filter(None, [text1, text2]))
        url = "/cgi-bin/configManager.cgi?action=setConfig&ChannelTitle[{0}].Name={1}".format(
            channel, text
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not set text")

    async def async_set_service_set_text_overlay(self, channel: int, group: int, text1: str, text2: str, text3: str,
                                                 text4: str):
        """ async_set_service_set_text_overlay sets the video text overlay """
        text = '|'.join(filter(None, [text1, text2, text3, text4]))
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[{0}].CustomTitle[{1}].Text={2}".format(
            channel, group, text
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not set text")

    async def async_set_service_set_custom_overlay(self, channel: int, group: int, text1: str, text2: str):
        """ async_set_service_set_custom_overlay sets the customer overlay on the video"""
        text = '|'.join(filter(None, [text1, text2]))
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[{0}].UserDefinedTitle[{1}].Text={2}".format(
            channel, group, text
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not set text")

    async def async_set_lighting_v2(self, channel: int, enabled: bool, brightness: int, profile_mode: str) -> dict:
        """
        async_set_lighting_v2 will turn on or off the white light on the camera. If turning on, the brightness will be used.
        brightness is in the range of 0 to 100 inclusive where 100 is the brightest.
        NOTE: this is not the same as the infrared (IR) light. This is the white visible light on the camera

        profile_mode: 0=day, 1=night, 2=scene
        """

        # on = Manual, off = Off
        mode = "Manual"
        if not enabled:
            mode = "Off"
        url = "/cgi-bin/configManager.cgi?action=setConfig&Lighting_V2[{channel}][{profile_mode}][0].Mode={mode}&Lighting_V2[{channel}][{profile_mode}][0].MiddleLight[0].Light={brightness}".format(
            channel=channel, profile_mode=profile_mode, mode=mode, brightness=brightness
        )
        _LOGGER.debug("Turning light on: %s", url)
        return await self.get(url)

    # async def async_set_lighting_v2_for_amcrest_flood_lights(self, channel: int, enabled: bool, brightness: int, profile_mode: str) -> dict:
    async def async_set_lighting_v2_for_amcrest_flood_lights(self, channel: int, enabled: bool, profile_mode: str) -> dict:
        """
        async_set_lighting_v2_for_amcrest_floodlights will turn on or off the flood light on the camera. If turning on, the brightness will be used.
        brightness is in the range of 0 to 100 inclusive where 100 is the brightest.
        NOTE: While the flood lights do support an auto or "smart" mode, the api does not handle this change properly.
              If one wishes to make the change back to auto, it must be done in the 'Amcrest Smart Home' smartphone app.

        profile_mode: 0=day, 1=night, 2=scene
        """

        # on = Manual, off = Off
        mode = "Manual"
        if not enabled:
            mode = "Off"
        url_base = "/cgi-bin/configManager.cgi?action=setConfig"
        mode_cmnd = f'Lighting_V2[{channel}][{profile_mode}][1].Mode={mode}'
        # brightness_cmnd = f'Lighting_V2[{channel}][{profile_mode}][1].MiddleLight[0].Light={brightness}'
        # url = f'{url_base}&{mode_cmnd}&{brightness_cmnd}'
        url = f'{url_base}&{mode_cmnd}'
        _LOGGER.debug("Switching light: %s", url)
        return await self.get(url)

    async def async_set_lighting_v2_for_amcrest_doorbells(self, mode: str) -> dict:
        """
        async_set_lighting_v2_for_amcrest_doorbells will turn on or off the white light on Amcrest doorbells
        mode: On, Off, Flicker
        """
        mode = mode.lower()
        cmd = "Off"
        if mode == "on":
            cmd = "ForceOn&Lighting_V2[0][0][1].State=On"
        elif mode in ('strobe', 'flicker'):
            cmd = "ForceOn&Lighting_V2[0][0][1].State=Flicker"

        url = "/cgi-bin/configManager.cgi?action=setConfig&Lighting_V2[0][0][1].Mode={cmd}".format(cmd=cmd)
        _LOGGER.debug("Turning doorbell light on: %s", url)
        return await self.get(url)

    async def async_set_video_in_day_night_mode(self, channel: int, config_type: str, mode: str):
        """
        async_set_video_in_day_night_mode will set the video dan/night config. For example to see it to Color or Black
        and white.

        config_type is one of  "general", "day", or "night"
        mode is one of: "Color", "Brightness", or "BlackWhite". Note Brightness is also known as "Auto"
        """

        # Map the input to the Dahua required integer: 0=day, 1=night, 2=general
        if config_type == "day":
            config_no = 0
        elif config_type == "night":
            config_no = 1
        else:
            # general
            config_no = 2

        # Map the mode
        if mode is None or mode.lower() == "auto" or mode.lower() == "brightness":
            mode = "Brightness"
        elif mode.lower() == "color":
            mode = "Color"
        elif mode.lower() == "blackwhite":
            mode = "BlackWhite"

        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoInDayNight[{0}][{1}].Mode={2}".format(
            channel, str(config_no), mode
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not set Day/Night mode")

    async def async_get_video_in_mode(self) -> dict:
        """
        async_get_video_in_mode will return the profile mode (day/night)
        0 means config for day,
        1 means config for night, and
        2 means config for normal scene.

        table.VideoInMode[0].Config[0]=2
        table.VideoInMode[0].Mode=0
        table.VideoInMode[0].TimeSection[0][0]=0 00:00:00-24:00:00
        """

        url = "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInMode"
        return await self.get(url)

    async def async_set_coaxial_control_state(self, channel: int, dahua_type: int, enabled: bool) -> dict:
        """
        async_set_lighting_v2 will turn on or off the white light on the camera.

        Type=1 -> white light on the camera. this is not the same as the infrared (IR) light. This is the white visible light on the camera
        Type=2 -> siren. The siren will trigger for 10 seconds or so and then turn off. I don't know how to get the siren to play forever
        NOTE: this is not the same as the infrared (IR) light. This is the white visible light on the camera
        """

        # on = 1, off = 0
        io = "1"
        if not enabled:
            io = "2"

        url = "/cgi-bin/coaxialControlIO.cgi?action=control&channel={channel}&info[0].Type={dahua_type}&info[0].IO={io}".format(
            channel=channel, dahua_type=dahua_type, io=io)
        _LOGGER.debug("Setting coaxial control state to %s: %s", io, url)
        return await self.get(url)

    async def async_set_disarming_linkage(self, channel: int, enabled: bool) -> dict:
        """
        async_set_disarming_linkage will set the camera's disarming linkage (Event -> Disarming in the UI)
        """

        value = "false"
        if enabled:
            value = "true"

        url = "/cgi-bin/configManager.cgi?action=setConfig&DisableLinkage[{0}].Enable={1}".format(channel, value)
        return await self.get(url)

    async def async_set_record_mode(self, channel: int, mode: str) -> dict:
        """
        async_set_record_mode sets the record mode.
        mode should be one of: auto, manual, or off
        """

        if mode.lower() == "auto":
            mode = "0"
        elif mode.lower() == "manual" or mode.lower() == "on":
            mode = "1"
        elif mode.lower() == "off":
            mode = "2"
        url = "/cgi-bin/configManager.cgi?action=setConfig&RecordMode[{0}].Mode={1}".format(channel, mode)
        _LOGGER.debug("Setting record mode: %s", url)
        return await self.get(url)

    async def async_get_disarming_linkage(self) -> dict:
        """
        async_get_disarming_linkage will return true if the disarming linkage (Event -> Disarming in the UI) is enabled

        returns
        table.DisableLinkage.Enable=false
        """

        url = "/cgi-bin/configManager.cgi?action=getConfig&name=DisableLinkage"
        try:
            return await self.get(url)
        except aiohttp.ClientResponseError as e:
            return {"table.DisableLinkage.Enable": "false"}

    async def async_access_control_open_door(self, door_id: int = 1) -> dict:
        """
        async_access_control_open_door opens a door via a VTO
        """
        url = "/cgi-bin/accessControl.cgi?action=openDoor&UserID=101&Type=Remote&channel={0}".format(door_id)
        return await self.get(url)

    async def enable_motion_detection(self, channel: int, enabled: bool) -> dict:
        """
        enable_motion_detection will either enable/disable motion detection on the camera depending on the value
        """
        url = "/cgi-bin/configManager.cgi?action=setConfig&MotionDetect[{channel}].Enable={enabled}&MotionDetect[{channel}].DetectVersion=V3.0".format(
            channel=channel, enabled=str(enabled).lower())
        response = await self.get(url)

        if "OK" in response:
            return response

        # Some older cameras do not support the above API, so try this one
        url = "/cgi-bin/configManager.cgi?action=setConfig&MotionDetect[{0}].Enable={1}".format(channel,
                                                                                                str(enabled).lower())
        return await self.get(url)

    async def stream_events(self, on_receive, events: list, channel: int):
        """
        enable_motion_detection will either enable or disable motion detection on the camera depending on the supplied value

        All: Use the literal word "All" to get back all events.. or pick and choose from the ones below
        VideoMotion: motion detection event
        VideoMotionInfo: fires when there's motion. Not really sure what it is for
        NewFile:
        SmartMotionHuman: human smart motion detection
        SmartMotionVehicle：Vehicle smart motion detection
        IntelliFrame: I don't know what this is
        VideoLoss: video loss detection event
        VideoBlind: video blind detection event.
        AlarmLocal: alarm detection event.
        CrossLineDetection: tripwire event
        CrossRegionDetection: intrusion event
        LeftDetection: abandoned object detection
        TakenAwayDetection: missing object detection
        VideoAbnormalDetection: scene change event
        FaceDetection: face detect event
        AudioMutation: intensity change
        AudioAnomaly: input abnormal
        VideoUnFocus: defocus detect event
        WanderDetection: loitering detection event
        RioterDetection: People Gathering event
        ParkingDetection: parking detection event
        MoveDetection: fast moving event
        StorageNotExist: storage not exist event.
        StorageFailure: storage failure event.
        StorageLowSpace: storage low space event.
        AlarmOutput: alarm output event.
        InterVideoAccess: I don't know what this is
        NTPAdjustTime: NTP time updates?
        TimeChange: Some event for time changes, related to NTPAdjustTime
        MDResult: motion detection data reporting event. The motion detect window contains 18 rows and 22 columns. The event info contains motion detect data with mask of every row.
        HeatImagingTemper: temperature alarm event
        CrowdDetection: crowd density overrun event
        FireWarning: fire warning event
        FireWarningInfo: fire warning specific data info

        In the example, you can see most event info is like "Code=eventcode; action=Start;
        index=0", but for some specific events, they will contain an another parameter named
        "data", the event info is like "Code=eventcode; action=Start; index=0; data=datainfo",
        the datainfo's fomat is JSON(JavaScript Object Notation). The detail information about
        the specific events and datainfo are listed in the appendix below this table.

        Heartbeat: integer, range is [1,60],unit is second.If the URL contains this parameter,
        and the value is 5, it means every 5 seconds the device should send the heartbeat
        message to the client,the heartbeat message are "Heartbeat".
        Note: Heartbeat message must be sent before heartbeat timeout
        """
        # Use codes=[All] for all codes
        codes = ",".join(events)
        url = "{0}/cgi-bin/eventManager.cgi?action=attach&codes=[{1}]&heartbeat=5".format(self._base, codes)
        if self._username is not None and self._password is not None:
            response = None

            try:
                auth = DigestAuth(self._username, self._password, self._session)
                response = await auth.request("GET", url)
                response.raise_for_status()

                # https://docs.aiohttp.org/en/stable/streams.html
                async for data, _ in response.content.iter_chunks():
                    on_receive(data, channel)
            except Exception as exception:
                pass
            finally:
                if response is not None:
                    response.close()

    @staticmethod
    async def parse_dahua_api_response(data: str) -> dict:
        """
        Dahua APIs return back text that looks like this:

        key1=value1
        key2=value2

        We'll convert that to a dictionary like {"key1":"value1", "key2":"value2"}
        """
        lines = data.splitlines()
        data_dict = {}
        for line in lines:
            parts = line.split("=", 1)
            if len(parts) == 2:
                data_dict[parts[0]] = parts[1]
            else:
                # We didn't get a key=value. We just got a key. Just stick it in the dictionary and move on
                data_dict[parts[0]] = line
        return data_dict

    async def get_bytes(self, url: str) -> bytes:
        """Get information from the API. This will return the raw response and not process it"""
        async with async_timeout.timeout(TIMEOUT_SECONDS):
            response = None
            try:
                auth = DigestAuth(self._username, self._password, self._session)
                response = await auth.request("GET", self._base + url)
                response.raise_for_status()

                return await response.read()
            finally:
                if response is not None:
                    response.close()

    async def get(self, url: str, verify_ok=False) -> dict:
        """Get information from the API."""
        url = self._base + url
        try:
            async with async_timeout.timeout(TIMEOUT_SECONDS):
                response = None
                try:
                    auth = DigestAuth(self._username, self._password, self._session)
                    response = await auth.request("GET", url)
                    response.raise_for_status()
                    data = await response.text()
                    if verify_ok:
                        if data.lower().strip() != "ok":
                            raise Exception(data)
                    return await self.parse_dahua_api_response(data)
                finally:
                    if response is not None:
                        response.close()
        except asyncio.TimeoutError as exception:
            _LOGGER.warning("TimeoutError fetching information from %s", url)
            raise exception
        except (KeyError, TypeError) as exception:
            _LOGGER.warning("TypeError fetching information from %s", url)
            raise exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            _LOGGER.debug("ClientError fetching information from %s", url)
            raise exception
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.warning("Exception fetching information from %s", url)
            raise exception

    @staticmethod
    def to_stream_name(subtype: int) -> str:
        """ Given the subtype (aka, stream index), returns the stream name (Main or Sub) """
        if subtype == 0:
            return "Main"
        elif subtype == 1:
            # We originally didn't support more than 1 sub-stream and it we just called it "Sub". To keep backwards
            # compatibility we'll keep the name "Sub" for the first sub-stream. Others will follow the pattern below
            return "Sub"
        else:
            return "Sub_{0}".format(subtype)
