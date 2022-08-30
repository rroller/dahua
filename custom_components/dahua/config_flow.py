"""Adds config flow (UI flow) for Dahua IP cameras."""
import logging
import ssl

import voluptuous as vol

from aiohttp import ClientSession, TCPConnector

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
    CONF_EVENTS,
    CONF_NAME,
    DOMAIN,
    PLATFORMS,
    CONF_CHANNEL,
)

"""
https://developers.home-assistant.io/docs/config_entries_config_flow_handler
https://developers.home-assistant.io/docs/data_entry_flow_index/
"""

_LOGGER: logging.Logger = logging.getLogger(__package__)

DEFAULT_EVENTS = ["VideoMotion", "CrossLineDetection", "AlarmLocal", "VideoLoss", "VideoBlind", "AudioMutation",
                  "CrossRegionDetection", "SmartMotionHuman", "SmartMotionVehicle"]

ALL_EVENTS = ["VideoMotion",
              "VideoLoss",
              "AlarmLocal",
              "CrossLineDetection",
              "CrossRegionDetection",
              "AudioMutation",
              "SmartMotionHuman",
              "SmartMotionVehicle",
              "VideoBlind",
              "AudioAnomaly",
              "VideoMotionInfo",
              "NewFile",
              "IntelliFrame",
              "LeftDetection",
              "TakenAwayDetection",
              "VideoAbnormalDetection",
              "FaceDetection",
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
              "ObjectPlacementDetection",
              "ObjectRemovalDetection",
              ]

"""
https://developers.home-assistant.io/docs/data_entry_flow_index
"""


class DahuaFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Dahua Camera API."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Initialize."""
        self.dahua_config = {}
        self._errors = {}
        self.init_info = None

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user to add a camera."""
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
                user_input[CONF_CHANNEL],
            )
            if data is not None:
                # Only allow a camera to be setup once
                if "serialNumber" in data and data["serialNumber"] is not None:
                    channel = int(user_input[CONF_CHANNEL])
                    unique_id = data["serialNumber"]
                    if channel > 0:
                        unique_id = unique_id + "_" + str(channel)
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()

                user_input[CONF_NAME] = data["name"]
                self.init_info = user_input
                return await self._show_config_form_name(user_input)
            else:
                self._errors["base"] = "auth"

        return await self._show_config_form_user(user_input)

    async def async_step_name(self, user_input=None):
        """Handle a flow to configure the camera name."""
        self._errors = {}

        if user_input is not None:
            if self.init_info is not None:
                self.init_info.update(user_input)
                return self.async_create_entry(
                    title=self.init_info["name"],
                    data=self.init_info,
                )

        return await self._show_config_form_name(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return DahuaOptionsFlowHandler(config_entry)

    async def _show_config_form_user(self, user_input):  # pylint: disable=unused-argument
        """Show the configuration form to edit camera name."""
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_ADDRESS): str,
                    vol.Required(CONF_PORT, default="80"): str,
                    vol.Required(CONF_RTSP_PORT, default="554"): str,
                    vol.Required(CONF_CHANNEL, default=0): int,
                    vol.Optional(CONF_EVENTS, default=DEFAULT_EVENTS): cv.multi_select(ALL_EVENTS),
                }
            ),
            errors=self._errors,
        )

    async def _show_config_form_name(self, user_input):  # pylint: disable=unused-argument
        """Show the configuration form to edit location data."""
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=user_input[CONF_NAME]): str,
                }
            ),
            errors=self._errors,
        )

    async def _test_credentials(self, username, password, address, port, rtsp_port, channel):
        """Return name and serialNumber if credentials is valid."""
        # Self signed certs are used over HTTPS so we'll disable SSL verification
        ssl_context = ssl.create_default_context()
        ssl_context.set_ciphers("DEFAULT")
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connector = TCPConnector(enable_cleanup_closed=True, ssl=ssl_context)
        session = ClientSession(connector=connector)
        try:
            client = DahuaClient(username, password, address, port, rtsp_port, session)
            data = await client.get_machine_name()
            serial = await client.async_get_system_info()
            data.update(serial)
            if "name" in data:
                return data
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.error("Could not connect to Dahua device. For iMou devices see " +
                            "https://github.com/rroller/dahua/issues/6", exc_info=exception)


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
