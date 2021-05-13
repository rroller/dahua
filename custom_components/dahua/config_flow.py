"""Adds config flow (UI flow) for Dahua IP cameras."""
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers import config_validation as cv

from .client import DahuaClient
from .const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_ADDRESS,
    CONF_RTSP_PORT,
    CONF_PORT,
    CONF_STREAMS,
    CONF_EVENTS,
    STREAM_MAIN,
    STREAM_SUB,
    STREAM_BOTH,
    DOMAIN,
    PLATFORMS,
)

STREAMS = [STREAM_MAIN, STREAM_SUB, STREAM_BOTH]

DEFUALT_EVENTS = ["VideoMotion", "CrossLineDetection", "AlarmLocal", "VideoLoss", "VideoBlind"]

ALL_EVENTS = ["VideoMotion",
              "VideoLoss",
              "VideoBlind",
              "AlarmLocal",
              "CrossLineDetection",
              "VideoMotionInfo",
              "NewFile",
              "SmartMotionHuman",
              "SmartMotionVehicle",
              "IntelliFrame",
              "CrossRegionDetection",
              "LeftDetection",
              "TakenAwayDetection",
              "VideoAbnormalDetection",
              "FaceDetection",
              "AudioMutation",
              "AudioAnomaly",
              "VideoUnFocus",
              "WanderDetection",
              "RioterDetection",
              "ParkingDetection",
              "MoveDetection",
              "StorageNotExist",
              "StorageFailure",
              "StorageLowSpace",
              "AlarmOutput",
              "InterVideoAccess",
              "NTPAdjustTime",
              "TimeChange",
              "MDResult",
              "HeatImagingTemper",
              "CrowdDetection",
              "FireWarning",
              "FireWarningInfo",
              ]

"""
https://developers.home-assistant.io/docs/data_entry_flow_index
"""


class DahuaFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Dahua Camera API."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialize."""
        self.dahua_config = {}
        self._errors = {}

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        self._errors = {}

        # Uncomment the next 2 lines if only a single instance of the integration is allowed:
        # if self._async_current_entries():
        #     return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            data = await self._test_credentials(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_ADDRESS],
                user_input[CONF_PORT],
                user_input[CONF_RTSP_PORT],
            )
            if data is not None:
                user_input.update(data)
                return self.async_create_entry(
                    title=data["name"],
                    data=user_input,
                )
            else:
                self._errors["base"] = "auth"

            return await self._show_config_form(user_input)

        return await self._show_config_form(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return DahuaOptionsFlowHandler(config_entry)

    async def _show_config_form(self, user_input):  # pylint: disable=unused-argument
        """Show the configuration form to edit location data."""
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default="admin"): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_ADDRESS, default="192.168.1.213"): str,
                    vol.Required(CONF_PORT, default="80"): str,
                    vol.Required(CONF_RTSP_PORT, default="544"): str,
                    vol.Required(CONF_STREAMS, default=STREAMS[0]): vol.In(STREAMS),
                    vol.Optional(CONF_EVENTS, default=DEFUALT_EVENTS): cv.multi_select(ALL_EVENTS),
                }
            ),
            errors=self._errors,
        )

    async def _test_credentials(self, username, password, address, port, rtsp_port):
        """Return true if credentials is valid."""
        try:
            session = async_create_clientsession(self.hass)
            client = DahuaClient(
                username, password, address, port, rtsp_port, session
            )
            data = await client.get_machine_name()
            if "name" in data:
                return data
        except Exception:  # pylint: disable=broad-except
            pass
        return None


class DahuaOptionsFlowHandler(config_entries.OptionsFlow):
    """Dahua config flow options handler."""

    def __init__(self, config_entry):
        """Initialize HACS options flow."""
        self.config_entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):  # pylint: disable=unused-argument
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(x, default=self.options.get(x, True)): bool
                    for x in sorted(PLATFORMS)
                }
            ),
        )

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(
            title=self.config_entry.data.get(CONF_USERNAME), data=self.options
        )
