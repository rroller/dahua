"""Adds config flow (UI flow) for Dahua IP cameras."""
import asyncio
import logging
import ssl

import voluptuous as vol

from aiohttp import ClientConnectorError, ClientResponseError, ClientSession, TCPConnector

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
    CONF_AUTO_DETECT_CHANNEL,
    CONF_USE_RPC2,
    CONF_USE_HTTPS,
    CONF_SCAN_INTERVAL,
    CONF_NVR_ACTIVE_DETERRENCE,
    CONF_AUTHORIZED_PLATES,
    CONF_AUTHORIZED_HOLD_TIME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_AUTHORIZED_HOLD_TIME,
    MIN_SCAN_INTERVAL,
)

"""
https://developers.home-assistant.io/docs/config_entries_config_flow_handler
https://developers.home-assistant.io/docs/data_entry_flow_index/
"""

SSL_CONTEXT = ssl.create_default_context()
#SSL_CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2
SSL_CONTEXT.set_ciphers("DEFAULT")
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

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
              "All",
              "Traffic",
              "TrafficJunction",
              "TrafficSnapshot",
              ]

"""
https://developers.home-assistant.io/docs/data_entry_flow_index
"""


def describe_setup_failure(exception: BaseException) -> str:
    """Which translation key explains why a device could not be added.

    The form previously said "Username, Password, or Address is wrong" whatever
    happened, which is true of exactly one of these and actively misleading for
    the rest. A person told their password is wrong checks their password.

    Only 401 and 403 are credentials. Everything else is the device not being
    where, or not being what, we were told.
    """
    if isinstance(exception, ClientResponseError):
        if exception.status in (401, 403):
            # Correct, but not currently reachable from setup: get_machine_name
            # and async_get_system_info both swallow ClientResponseError and
            # synthesise an id, so a wrong password adds a broken camera rather
            # than being refused here. Left in because it is what the key means
            # and because that swallowing should be fixed; do not read a passing
            # test of this line as proof that a 401 ever arrives.
            return "auth"
        return "unexpected_reply"
    if isinstance(exception, ClientConnectorError):
        return "cannot_connect"
    # Order matters here and is not stylistic: TimeoutError and ssl.SSLError are
    # both subclasses of OSError, so the generic connection case has to come
    # last or it swallows them and every failure becomes "cannot connect".
    if isinstance(exception, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exception, ssl.SSLError):
        return "ssl_error"
    if isinstance(exception, OSError):
        # ConnectionRefusedError and friends, when they arrive unwrapped.
        return "cannot_connect"
    return "unknown"


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
            data, error = await self._test_credentials(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_ADDRESS],
                user_input[CONF_PORT],
                user_input[CONF_RTSP_PORT],
                user_input[CONF_CHANNEL],
                True if user_input.get(CONF_USE_HTTPS) else None,
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
                self._errors["base"] = error or "auth"

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

    async def async_step_reauth(self, entry_data):
        """Handle reauthentication when credentials become invalid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self._show_reauth_form()

    async def async_step_reauth_confirm(self, user_input=None):
        """Handle user input for reauthentication."""
        self._errors = {}

        if user_input is not None:
            entry = self._reauth_entry
            data, error = await self._test_credentials(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                entry.data[CONF_ADDRESS],
                entry.data[CONF_PORT],
                entry.data[CONF_RTSP_PORT],
                entry.data.get(CONF_CHANNEL, 0),
            )
            if data is not None:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, **user_input},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            self._errors["base"] = error or "auth"

        return await self._show_reauth_form()

    async def _show_reauth_form(self):
        """Show the reauthentication form."""
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=self._errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return DahuaOptionsFlowHandler()

    async def async_step_reconfigure(self, user_input=None):
        """Change an existing entry's connection settings.

        Credentials are left alone - those are what async_step_reauth is for.
        """
        self._errors = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            data, error = await self._test_credentials(
                entry.data[CONF_USERNAME],
                entry.data[CONF_PASSWORD],
                user_input[CONF_ADDRESS],
                user_input[CONF_PORT],
                user_input[CONF_RTSP_PORT],
                user_input[CONF_CHANNEL],
                True if user_input.get(CONF_USE_HTTPS) else None,
            )
            if data is not None:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
            self._errors["base"] = error or "auth"

        current = {**entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default=current.get(CONF_ADDRESS, "")): str,
                    vol.Required(CONF_PORT, default=str(current.get(CONF_PORT, "80"))): str,
                    vol.Required(CONF_RTSP_PORT, default=str(current.get(CONF_RTSP_PORT, "554"))): str,
                    vol.Required(CONF_CHANNEL, default=int(current.get(CONF_CHANNEL, 0))): int,
                    vol.Optional(CONF_USE_HTTPS, default=bool(current.get(CONF_USE_HTTPS, False))): bool,
                }
            ),
            errors=self._errors,
        )

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
                    vol.Optional(CONF_USE_HTTPS, default=False): bool,
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

    async def _test_credentials(self, username, password, address, port, rtsp_port, channel, use_https=None):
        """Return (data, error) -- the device's name and serial, or why not.

        The error is a translation key, because every failure used to arrive as
        "Username, Password, or Address is wrong". A device that refuses the
        connection, one on the wrong port, one that wants HTTPS and one that is
        simply switched off all produced that same sentence, so people checked
        their password repeatedly while the log quietly said something else --
        #690 is that, with the ConnectionRefusedError traceback attached.

        Note what this cannot be: get_machine_name and async_get_system_info
        both swallow ClientResponseError and synthesise an id, so a device that
        answers 401 to both is *added*, not refused. Whatever reaches here, it
        is not the camera rejecting the password.
        """
        # Self signed certs are used over HTTPS so we'll disable SSL verification
        connector = TCPConnector(enable_cleanup_closed=True, ssl=SSL_CONTEXT)
        session = ClientSession(connector=connector)
        try:
            client = DahuaClient(username, password, address, port, rtsp_port, session, use_https)
            data = await client.get_machine_name()
            serial = await client.async_get_system_info()
            data.update(serial)
            if "name" in data:
                return data, None
            # It answered, but not with anything recognisable.
            return None, "unexpected_reply"
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.error("Could not connect to Dahua device. For iMou devices see " +
                            "https://github.com/rroller/dahua/issues/6", exc_info=exception)
            return None, describe_setup_failure(exception)
        finally:
            await session.close()


class DahuaOptionsFlowHandler(config_entries.OptionsFlow):
    """Dahua config flow options handler."""

    async def async_step_init(self, user_input=None):  # pylint: disable=unused-argument
        """Manage the options."""
        self.options = dict(self.config_entry.options)
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        schema = {
            vol.Required(x, default=self.options.get(x, True)): bool
            for x in sorted(PLATFORMS)
        }
        schema[
            vol.Required(
                CONF_AUTO_DETECT_CHANNEL,
                default=self.options.get(CONF_AUTO_DETECT_CHANNEL, True),
            )
        ] = bool
        schema[
            vol.Required(
                CONF_USE_RPC2,
                default=self.options.get(CONF_USE_RPC2, False),
            )
        ] = bool
        schema[
            vol.Optional(
                CONF_EVENTS,
                default=self.options.get(
                    CONF_EVENTS, self.config_entry.data.get(CONF_EVENTS, DEFAULT_EVENTS)
                ),
            )
        ] = cv.multi_select(ALL_EVENTS)
        schema[
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=self.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL))
        schema[
            vol.Required(
                CONF_NVR_ACTIVE_DETERRENCE,
                default=self.options.get(CONF_NVR_ACTIVE_DETERRENCE, False),
            )
        ] = bool
        schema[
            vol.Optional(
                CONF_AUTHORIZED_PLATES,
                default=self.options.get(
                    CONF_AUTHORIZED_PLATES,
                    self.config_entry.data.get(CONF_AUTHORIZED_PLATES, ""),
                ),
            )
        ] = str
        schema[
            vol.Optional(
                CONF_AUTHORIZED_HOLD_TIME,
                default=self.options.get(
                    CONF_AUTHORIZED_HOLD_TIME,
                    self.config_entry.data.get(
                        CONF_AUTHORIZED_HOLD_TIME, DEFAULT_AUTHORIZED_HOLD_TIME
                    ),
                ),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=1, max=3600))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
        )

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(
            title=self.config_entry.data.get(CONF_USERNAME), data=self.options
        )
