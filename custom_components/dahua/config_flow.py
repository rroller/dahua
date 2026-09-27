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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector

from . import dahua_utils
from . import _async_probe_tcp
from .client import DahuaClient
from .discovery import async_probe as async_probe_identity
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
    CONF_ALL_CHANNELS,
    CONF_AREA,
    DEFAULT_EVENTS,
    CONF_EXTRA_CHANNELS,
    CONF_USE_RPC2,
    CONF_USE_HTTPS,
    CONF_SCAN_INTERVAL,
    CONF_NVR_ACTIVE_DETERRENCE,
    CONF_MANUAL_SIREN,
    CONF_MANUAL_SECURITY_LIGHT,
    CONF_DISABLE_BACKCHANNEL,
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

# How long the whole look for a recorder's other channels may take.
# Bounds the probe fan-out, which is otherwise one request timeout per
# channel, two at a time. Running out means nothing is offered, which is
# the same outcome as a device that has no channels to offer.
DISCOVERY_TIMEOUT_SECONDS = 30

# Dahua's own protocols: DHIP on 5000 and the private SDK port on 37777. A device
# answering either of these while refusing HTTP is switched on, reachable, and has
# its web/CGI service turned off. Measured on three cameras here which serve 5000
# and 37777 and nothing at all across 27 scanned HTTP-ish ports.
DAHUA_PRIVATE_PORTS = (37777, 5000)

# Only spent on a path that has already failed, so no successful setup waits on it.
FAILURE_PROBE_TIMEOUT_SECONDS = 3.0

_LOGGER: logging.Logger = logging.getLogger(__package__)

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
              "FaceRecognition",
              "HumanTrait",
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


def fallback_device_name(address: str, channel) -> str:
    """A name a person can read, for a device that would not tell us its own.

    get_machine_name falls back to md5(address_rtspport_username_password) when a
    device has no magicBox.cgi, and the flow offered that hash as the prefilled
    default of the final step. Pressing Submit produced a device called
    `4f3a9c8e...`, and because no platform sets _attr_has_entity_name, that hash
    went into every entity_id for good.

    The devices this hits are the ones the codebase already documents: the same
    fallback path names #583, #728 and #767.

    The channel is included from 1 upwards, and numbered the way the recorder shows
    it, so several fallen-back channels of one NVR do not all arrive with the same
    name.
    """
    try:
        index = int(channel)
    except (TypeError, ValueError):
        index = 0
    if index > 0:
        return "Dahua camera at {0} channel {1}".format(address, index + 1)
    return "Dahua camera at {0}".format(address)


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
            # Reachable only because get_machine_name and async_get_system_info
            # re-raise a 401 rather than synthesising an id from the refused
            # credentials. If either goes back to swallowing it, a wrong
            # password silently adds a camera again and this line goes dead.
            return "auth"
        if exception.status == 404:
            # Something is serving HTTP and does not have magicBox.cgi. On Dahua
            # that is the CGI service being switched off, which is the highest
            # value per issue of any device-side cause in the tracker: #145, #417
            # and #465 were all a checkbox, and #465 asked for exactly this hint.
            # A non-Dahua web server also lands here, so the message covers both.
            return "cgi_disabled"
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


# Errors that belong on the channel field rather than at the top of the form.
CHANNEL_ERRORS = ("channel_not_on_device", "channel_disabled", "channel_is_onvif")

# Failures that prove the transport already worked: the device answered and said
# something specific about itself. Showing somebody the port and HTTPS fields after
# one of these would point them away from the actual problem.
TRANSPORT_WORKED = ("auth",) + CHANNEL_ERRORS


async def async_channel_refusal(client, channel):
    """Why this channel cannot work, when the device says so outright.

    The channel was accepted unchecked until now: _test_credentials takes it and
    never uses it, and DahuaClient has no channel parameter at all. So typing the
    number the recorder displays -- 4 instead of 3, or 16 on a sixteen channel box
    -- produced a green tick and an entry whose every entity was dead.

    Only a recorder's own RemoteDevice table can answer this, and only for what it
    states plainly. Everything else is deliberately left alone:

      - no table, or an unreadable one, claims nothing. A standalone camera has
        none, and a multi-lens camera serves several channels without one, so
        refusing "channel > 0 with no table" would break those.
      - channel 0 is never checked. It is a standalone camera at least as often as
        a recorder's first slot, and no table is needed to know it is plausible.
      - a slot that exists, is enabled and is not Onvif is accepted even though it
        may still be empty. A camera removed from the recorder leaves a stale
        enabled slot, and only a snapshot probe tells the difference. That probe is
        what async_step_discover spends on the *other* channels; spending it here
        would mean refusing a channel on a guess.

    Every refusal below is the recorder's own statement about its own hardware.
    """
    try:
        index = int(channel)
    except (TypeError, ValueError):
        return None
    if index == 0:
        return None
    try:
        devices = dahua_utils.parse_remote_devices(
            await client.async_get_remote_devices())
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("No RemoteDevice table to check channel %s against", index,
                      exc_info=True)
        return None
    if not devices:
        return None

    slot = devices.get(index)
    if slot is None:
        return "channel_not_on_device"
    if not slot.get("enabled"):
        return "channel_disabled"
    if slot.get("protocol") == "onvif":
        # The recorder does not serve such a channel on its own Dahua paths, so
        # snapshot answers 400 and RTSP times out (#710).
        return "channel_is_onvif"
    return None
async def async_refine_connection_failure(address: str, reason: str) -> str:
    """Turn "nothing answered" into what the device is actually doing.

    `cannot_connect` says the HTTP request did not land. It does not say why, and
    the message it produces guesses wrong in the two most common cases:

        "Nothing answered at that address and port. Check the camera is on, that
         the IP and port are right, and that Home Assistant can reach it."

    A device serving HTTPS on 443, or one whose web service is off while Dahua's
    own protocols still answer, is on, correctly addressed and perfectly
    reachable. Every clause of that sentence sends the user somewhere useless.

    Both are answerable without credentials, and cheaply, because this only runs
    after a failure. Nothing is probed on a successful setup.
    """
    if reason != "cannot_connect":
        return reason

    # Checked first because it has a concrete action attached, and because the
    # repair card that already detects this (http_dead_https_available) only
    # exists for an entry that has been created.
    if await _async_probe_tcp(address, 443, FAILURE_PROBE_TIMEOUT_SECONDS):
        return "https_available"

    for port in DAHUA_PRIVATE_PORTS:
        if await _async_probe_tcp(address, port, FAILURE_PROBE_TIMEOUT_SECONDS):
            return "http_service_off"

    return reason


class DahuaFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Dahua Camera API."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Initialize."""
        self.dahua_config = {}
        self._errors = {}
        self.init_info = None
        # index -> label, for the other channels of a recorder
        self._found_channels = {}
        # What a DHCP announcement, and the device itself, told us before we asked
        # the user anything. Empty for a manual add.
        self._discovered = {}
        self._extra_channels = []
        # Which area each extra channel was given, by channel index, and the
        # form field each of those answers arrives under.
        self._channel_areas = {}
        self._area_fields = {}
        self._discovery_task = None

    async def async_step_dhcp(self, discovery_info):
        """A Dahua device appeared on the network.

        Two signals bring us here and they cover different devices: the OUI catches
        one whose hostname has been changed, and the factory hostname (literally
        `zhejiang.dahua.technology.co.ltd` on the doorbell measured here) catches
        one on an OUI we do not list.

        This only ever offers. Nothing is created, no credentials exist yet, and the
        user fills in the same form as before, with the parts already known filled
        in for them.
        """
        address = discovery_info.ip

        # Dedupe repeated announcements while a flow for this device is open.
        # Provisional on purpose: the entry's unique_id is the device's serial, and
        # that is set later by the step that actually logs in. This only stops three
        # announcements becoming three identical cards.
        await self.async_set_unique_id(dr.format_mac(discovery_info.macaddress))

        # Unauthenticated, and the reason the rest of this is worth doing: a device
        # that answers hands over its serial, model and HTTP port. Only one of the
        # five devices this was written against answers, so nothing below may depend
        # on it.
        info = await async_probe_identity(address)
        serial = str(info.get("SerialNo") or "").strip()

        if serial and self._async_entries_for_serial(serial):
            return self.async_abort(reason="already_configured")

        # No serial, or one we have not seen: an entry on this address is still
        # reason enough not to nag.
        self._async_abort_entries_match({CONF_ADDRESS: address})

        self._discovered = {CONF_ADDRESS: address}
        port = info.get("HttpPort")
        if port:
            self._discovered[CONF_PORT] = str(port)

        name = (info.get("DeviceType") or info.get("MachineName")
                or discovery_info.hostname or "Dahua device")
        # Shown on the discovery card in the integrations list.
        self.context["title_placeholders"] = {"name": name, "address": address}
        return await self.async_step_user()

    def _async_entries_for_serial(self, serial: str) -> list:
        """Every entry for one device, including a recorder's other channels.

        The unique_id is the bare serial for channel 0 and `serial_N` above it, so a
        prefix match is what covers a whole recorder. Without it, somebody who added
        only channel 3 would have that recorder announce itself as undiscovered for
        ever.
        """
        prefix = serial + "_"
        return [
            entry for entry in self._async_current_entries()
            if (entry.unique_id or "") == serial
            or (entry.unique_id or "").startswith(prefix)
        ]

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user to add a camera."""
        self._errors = {}

        # Uncomment the next 2 lines if only a single instance of the integration is allowed:
        # if self._async_current_entries():
        #     return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            # The transport fields are not on the form until something fails, so fill
            # in what is known: a discovery's HttpPort when it gave one, the ordinary
            # defaults otherwise. Stored either way, because entry.data is what the
            # coordinator reads.
            for key, fallback in ((CONF_PORT, "80"), (CONF_RTSP_PORT, "554")):
                if not user_input.get(key):
                    user_input[key] = self._discovered.get(key, fallback)

            data, error = await self._test_credentials(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_ADDRESS],
                user_input[CONF_PORT],
                user_input[CONF_RTSP_PORT],
                user_input[CONF_CHANNEL],
                True if user_input.get(CONF_USE_HTTPS) else None,
                check_channel=True,
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
                return await self.async_step_discover()
            else:
                # A channel the recorder disowns is a problem with that field, not
                # with the connection, so it is reported there.
                field = CONF_CHANNEL if error in CHANNEL_ERRORS else "base"
                self._errors[field] = error or "auth"

        return await self._show_config_form_user(user_input)

    async def async_step_discover(self, user_input=None):
        """Look for the recorder's other channels, with the wait on screen.

        Probing fifteen channels two at a time is slow enough that holding it
        inside the previous step gives a form that looks hung, with nothing
        saying why. As a progress step the dialog says what is happening and
        roughly how long it can take, and DISCOVERY_TIMEOUT_SECONDS still
        bounds it.

        A standalone camera has no such table and refuses the first read, so
        the dialog is brief rather than absent. That is the honest thing to
        show: the search did happen.
        """
        if self._discovery_task is None:
            self._discovery_task = self.hass.async_create_task(
                self._async_discover_channels(
                    self.init_info, int(self.init_info[CONF_CHANNEL])))

        # Whether the search has finished, not whether it has been started.
        # A step showing progress is re-entered for reasons other than the task
        # completing: asking the flow for its current state does it, and so
        # does reopening the dialog. Branching on the task merely existing gave
        # up on the first of those, read the result of a task still running,
        # and offered nothing at all.
        if not self._discovery_task.done():
            return self.async_show_progress(
                step_id="discover",
                progress_action="discover",
                description_placeholders={
                    "seconds": str(DISCOVERY_TIMEOUT_SECONDS)},
                progress_task=self._discovery_task,
            )

        try:
            self._found_channels = self._discovery_task.result()
        except (Exception, asyncio.CancelledError):  # pylint: disable=broad-except
            # _async_discover_channels swallows its own failures, so this is
            # the flow being abandoned mid-search. Nothing to offer, and the
            # camera the user actually asked for is still added.
            _LOGGER.debug("The channel search did not finish", exc_info=True)
            self._found_channels = {}

        return self.async_show_progress_done(
            next_step_id="channels" if self._found_channels else "name")

    async def _async_discover_channels(self, user_input, exclude) -> dict:
        """Which other channels of this recorder have a live camera on them.

        Three things have to agree, and the first two are not enough.

        The slot has to be enabled, and it must not be reached over Onvif: such
        a channel exists and this integration cannot drive it, because the
        recorder does not serve it on its own Dahua paths (#710).

        And it has to answer. A camera removed from the recorder leaves its slot
        enabled with a stale serial. Measured on a DHI-NVR5464-16P-EI, two such
        slots read exactly like live ones and returned 400 to a snapshot.
        Offering those would create entries that can never work, which is the
        failure this step exists to avoid. async_probe_snapshot asks without
        fetching the image.

        Any failure here means nothing is offered, never a failed setup. Adding
        one camera must not start depending on a recorder-only table.
        """
        session = ClientSession(
            connector=TCPConnector(ssl=SSL_CONTEXT))
        try:
            client = DahuaClient(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD],
                user_input[CONF_ADDRESS], user_input[CONF_PORT],
                user_input[CONF_RTSP_PORT], session,
                True if user_input.get(CONF_USE_HTTPS) else None)
            try:
                devices = dahua_utils.parse_remote_devices(
                    await client.async_get_remote_devices())
            except Exception:  # pylint: disable=broad-except
                # A standalone camera has no such table. Nothing to offer is an
                # ordinary answer rather than a failure, so this is not a
                # warning. It is logged because the alternative is a feature
                # that can do nothing at all and leave no trace of why.
                _LOGGER.debug(
                    "No RemoteDevice table on %s, so no channels to offer",
                    user_input[CONF_ADDRESS], exc_info=True)
                return {}

            candidates = [
                index for index in dahua_utils.channels_worth_offering(devices)
                if index != exclude
            ]
            _LOGGER.debug(
                "%s: %d slots, %s worth offering, %d after excluding channel %s",
                user_input[CONF_ADDRESS], len(devices),
                dahua_utils.channels_worth_offering(devices), len(candidates),
                exclude)
            if not candidates:
                return {}

            try:
                titles = dahua_utils.parse_channel_titles(
                    await client.async_get_config("ChannelTitle"))
            except Exception:  # pylint: disable=broad-except
                titles = {}

            async def live(index):
                try:
                    await client.async_probe_snapshot(index + 1)
                    return index
                except Exception:  # pylint: disable=broad-except
                    return None

            # The client's own per-host limit holds this to two at a time, so
            # gathering does not turn setup into a burst the recorder has to
            # absorb. That limit is also why the whole thing needs a ceiling:
            # sixteen channels, two at a time, each able to spend
            # TIMEOUT_SECONDS before giving up, is long enough that a recorder
            # which has stopped answering would leave the form looking frozen
            # for minutes. Nothing here is worth that -- discovery is a
            # convenience, and not offering anything is a fine outcome.
            answered = await asyncio.wait_for(
                asyncio.gather(*[live(i) for i in candidates]),
                DISCOVERY_TIMEOUT_SECONDS)
            found = {
                index: titles.get(index) or "Channel {0}".format(index + 1)
                for index in answered if index is not None
            }
            _LOGGER.debug("%s: %d of %d candidates answered a snapshot: %s",
                          user_input[CONF_ADDRESS], len(found), len(candidates),
                          sorted(found))
            return found
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Could not look for other channels", exc_info=True)
            return {}
        finally:
            await session.close()

    async def async_step_channels(self, user_input=None):
        """Offer the recorder's other live channels.

        A sixteen channel recorder means sixteen boxes to tick, and somebody
        adding a recorder usually wants all of it. `all_channels` takes the
        whole list in one click and is checked first, so a user who ticks it
        does not also have to clear the individual boxes.
        """
        if user_input is not None:
            if user_input.get(CONF_ALL_CHANNELS):
                self._extra_channels = sorted(self._found_channels)
            else:
                self._extra_channels = [
                    int(index) for index in user_input.get(CONF_EXTRA_CHANNELS, [])
                ]
            if self._extra_channels:
                return await self.async_step_areas()
            return await self._show_config_form_name(self.init_info)

        return self.async_show_form(
            step_id="channels",
            data_schema=vol.Schema({
                vol.Optional(CONF_ALL_CHANNELS, default=False): bool,
                vol.Optional(CONF_EXTRA_CHANNELS, default=[]):
                    cv.multi_select({
                        str(index): "Channel {0}: {1}".format(index + 1, name)
                        for index, name in sorted(self._found_channels.items())
                    }),
            }),
            description_placeholders={
                "count": str(len(self._found_channels))},
            errors=self._errors,
        )

    def _area_form_fields(self) -> dict:
        """The label each device being added should be asked about, by channel.

        The label is the schema key, because Home Assistant renders a key
        verbatim when no translation string exists. That is what makes a form
        whose fields depend on what the recorder reported readable, without
        inventing a translation key per channel. The channel number is in every
        label, so two channels that share a title cannot collide.
        """
        primary = int(self.init_info[CONF_CHANNEL])
        fields = {
            "{0} (this device)".format(self.init_info[CONF_NAME]): primary,
        }
        for index in self._extra_channels:
            # The number the recorder itself shows, which is one more than the
            # index this integration uses. A channel the recorder gave no title
            # is just its number, rather than "Channel 7: Channel 7".
            title = self._found_channels.get(index)
            label = ("Channel {0}: {1}".format(index + 1, title) if title
                     else "Channel {0}".format(index + 1))
            fields[label] = index
        return fields

    async def async_step_areas(self, user_input=None):
        """Ask which area each device being added belongs in.

        Adding ten channels used to produce ten devices with no area, leaving
        the user to file them one at a time in Settings -- right after being
        shown a list that already named every one of them.

        Only reached when extra channels were chosen. A single camera gains no
        step; the options flow covers that one, and Home Assistant's own device
        page is a click away.
        """
        if user_input is not None:
            for label, index in self._area_fields.items():
                area = user_input.get(label)
                if not area:
                    continue
                if index == int(self.init_info[CONF_CHANNEL]):
                    self.init_info[CONF_AREA] = area
                else:
                    self._channel_areas[index] = area
            return await self._show_config_form_name(self.init_info)

        self._area_fields = self._area_form_fields()
        return self.async_show_form(
            step_id="areas",
            data_schema=vol.Schema({
                # Optional, and a blank answer means no area rather than an
                # error: somebody who has not made their areas yet must still
                # be able to finish adding their cameras.
                vol.Optional(label): selector.AreaSelector()
                for label in self._area_fields
            }),
            errors=self._errors,
        )

    async def async_step_import(self, import_data):
        """Add one channel without asking anything.

        Where the extra channels chosen on the channels step arrive. They
        answered a probe a moment ago, so this validates and creates rather than
        prompting. One that has since stopped answering aborts on its own and
        the others are unaffected.
        """
        data, error = await self._test_credentials(
            import_data[CONF_USERNAME], import_data[CONF_PASSWORD],
            import_data[CONF_ADDRESS], import_data[CONF_PORT],
            import_data[CONF_RTSP_PORT], import_data[CONF_CHANNEL],
            True if import_data.get(CONF_USE_HTTPS) else None)
        if data is None:
            return self.async_abort(reason=error or "auth")

        serial = data.get("serialNumber")
        if serial:
            channel = int(import_data[CONF_CHANNEL])
            unique_id = serial if channel == 0 else "{0}_{1}".format(serial, channel)
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=import_data[CONF_NAME], data=import_data)

    async def async_step_name(self, user_input=None):
        """Handle a flow to configure the camera name."""
        self._errors = {}

        if user_input is not None:
            if self.init_info is not None:
                self.init_info.update(user_input)
                self._queue_extra_channels()
                return self.async_create_entry(
                    title=self.init_info["name"],
                    data=self.init_info,
                )

        return await self._show_config_form_name(user_input or self.init_info)

    def _queue_extra_channels(self) -> None:
        """Start a flow for each additional channel the user ticked.

        Creating this entry ends this flow, so the rest go through their own.
        Each sets its own unique_id and aborts if that channel is already
        configured, so this cannot add the same channel twice.
        """
        for index in self._extra_channels:
            data = dict(self.init_info)
            data[CONF_CHANNEL] = index
            data[CONF_NAME] = self._found_channels.get(
                index, "Channel {0}".format(index + 1))
            # Every extra channel inherits init_info, which carries the
            # *primary's* area. Without the pop, ticking one area for the
            # recorder itself would silently file every other channel there too.
            area = self._channel_areas.get(index)
            if area:
                data[CONF_AREA] = area
            else:
                data.pop(CONF_AREA, None)
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": "import"}, data=data))

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
                check_channel=True,
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
                    vol.Required(CONF_PORT,
                                 default=str(current.get(CONF_PORT, "80"))): vol.All(cv.port, vol.Coerce(str)),
                    vol.Required(CONF_RTSP_PORT,
                                 default=str(current.get(CONF_RTSP_PORT, "554"))): vol.All(cv.port, vol.Coerce(str)),
                    vol.Required(CONF_CHANNEL,
                                 default=int(current.get(CONF_CHANNEL, 0))): vol.All(
                                     vol.Coerce(int), vol.Range(min=0)),
                    vol.Optional(CONF_USE_HTTPS, default=bool(current.get(CONF_USE_HTTPS, False))): bool,
                }
            ),
            errors=self._errors,
        )

    @callback
    def _user_schema(self, reveal_transport=False):
        """The add form's fields.

        Four questions, not eight. Username, password and address are the ones only
        the user can answer; the channel stays because hiding it would remove the
        ability to add one channel of a recorder on its own, which is a capability
        rather than a detail.

        The port, the RTSP port and the HTTPS box appear **only after something has
        failed**. They are transport details the integration can work out: 80 and 554
        are right almost always, HTTPS follows from the port, and a discovery often
        supplies the real one. Asking all three up front makes every user answer for
        the few whose device is unusual, and #794 now names the unusual cases as they
        happen ("it is listening on 443, tick HTTPS").

        The events list is gone from here entirely. It is not needed to connect, so
        the Bronze config-flow rule puts it in options, and it silently decided the
        entity count: 42 codes, most of which do nothing on most cameras. An entry
        created without it gets DEFAULT_EVENTS, which get_configured_events and the
        options form both already handle.
        """
        fields = {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Required(CONF_ADDRESS): str,
            vol.Required(CONF_CHANNEL, default=0): vol.All(
                vol.Coerce(int), vol.Range(min=0)),
        }
        if reveal_transport:
            # Checked as ports and stored as strings, which is what every existing
            # entry holds: cv.port alone would store an int and leave two shapes
            # mixed across entries for no gain. int() also strips whitespace, so a
            # trailing space stops becoming "http://ip: 80", a yarl InvalidURL, and
            # the message "the log has the reason".
            #
            # vol.Coerce(str) and not a bare str: in voluptuous a type is a *check*,
            # not a conversion, so vol.All(cv.port, str) asserts that the int cv.port
            # just produced is a string and fails every time.
            fields[vol.Required(CONF_PORT, default="80")] = vol.All(
                cv.port, vol.Coerce(str))
            fields[vol.Required(CONF_RTSP_PORT, default="554")] = vol.All(
                cv.port, vol.Coerce(str))
            fields[vol.Optional(CONF_USE_HTTPS, default=False)] = bool
        return vol.Schema(fields)

    async def _show_config_form_user(self, user_input):
        """Show the add form, prefilled with anything already known.

        Two sources, and the order matters. What the user just typed wins, because a
        submit that failed used to come back empty and a mistyped port meant
        entering everything again. Behind that sits whatever a discovery worked out.

        Suggested values rather than defaults, so validation is untouched: a
        required field with an empty default is not the same thing as a required
        field. The password is never prefilled from either source.
        """
        known = dict(self._discovered)
        known.update({key: value for key, value in (user_input or {}).items()
                      if key != CONF_PASSWORD and value not in (None, "")})

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                self._user_schema(
                    reveal_transport=any(
                        reason not in TRANSPORT_WORKED
                        for reason in self._errors.values())),
                known),
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

    async def _test_credentials(self, username, password, address, port, rtsp_port,
                                channel, use_https=None, check_channel=False):
        """Return (data, error) -- the device's name and serial, or why not.

        The error is a translation key, because every failure used to arrive as
        "Username, Password, or Address is wrong". A device that refuses the
        connection, one on the wrong port, one that wants HTTPS and one that is
        simply switched off all produced that same sentence, so people checked
        their password repeatedly while the log quietly said something else --
        #690 is that, with the ConnectionRefusedError traceback attached.

        A 401 reaches here only because the identity calls re-raise it; every
        other status still falls back to a synthesised id, so devices with no
        magicBox.cgi are added exactly as before.
        """
        # Self signed certs are used over HTTPS so we'll disable SSL verification
        connector = TCPConnector(ssl=SSL_CONTEXT)
        session = ClientSession(connector=connector)
        try:
            client = DahuaClient(username, password, address, port, rtsp_port, session, use_https)
            data = await client.get_machine_name()
            # True only if get_machine_name itself fell back: the flag starts
            # False and this is the first call to use it. Reading it after the
            # system-info call would also catch that one falling back, which says
            # nothing about whether the *name* is a hash.
            name_is_a_hash = client.identity_derived_from_credentials
            serial = await client.async_get_system_info(strict_auth=True)
            data.update(serial)
            if "name" in data:
                # Asked for by the two steps where somebody types a channel. The
                # import step adds channels that came out of the recorder's own
                # table a moment earlier, so re-reading it once per channel would be
                # sixteen pointless requests on a sixteen channel recorder, and
                # reauth is not the place to start refusing an existing entry.
                if check_channel:
                    refusal = await async_channel_refusal(client, channel)
                    if refusal:
                        return None, refusal
                if name_is_a_hash:
                    # The unique_id deliberately keeps the hashed serial, because
                    # changing it would orphan every entry that already has one.
                    # Only what the user is shown changes.
                    data["name"] = fallback_device_name(address, channel)
                return data, None
            # It answered, but not with anything recognisable.
            return None, "unexpected_reply"
        except Exception as exception:  # pylint: disable=broad-except
            reason = describe_setup_failure(exception)
            _LOGGER.error(
                "Could not connect to Dahua device at %s (%s). For iMou devices "
                "see https://github.com/rroller/dahua/issues/6",
                address, reason, exc_info=exception)
            return None, await async_refine_connection_failure(address, reason)
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
            await self._async_move_device(user_input.get(CONF_AREA))
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
            vol.Required(
                CONF_MANUAL_SIREN,
                default=self.options.get(CONF_MANUAL_SIREN, False),
            )
        ] = bool
        schema[
            vol.Required(
                CONF_MANUAL_SECURITY_LIGHT,
                default=self.options.get(CONF_MANUAL_SECURITY_LIGHT, False),
            )
        ] = bool
        schema[
            vol.Required(
                CONF_DISABLE_BACKCHANNEL,
                default=self.options.get(CONF_DISABLE_BACKCHANNEL, False),
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
        schema[
            vol.Optional(
                CONF_AREA,
                default=self.options.get(
                    CONF_AREA, self.config_entry.data.get(CONF_AREA, "")),
            )
        ] = selector.AreaSelector()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
        )

    async def _async_move_device(self, area_id):
        """Put this entry's device in `area_id`, if that is a change.

        The config flow uses `suggested_area`, which Home Assistant honours only
        when it *creates* a device. This entry's device already exists, so that
        would be silently ignored and the option would look broken. Moving it
        through the device registry is the supported way round.

        A no-op when the value has not changed, so opening options and pressing
        submit does not re-file a device somebody has moved by hand.
        """
        stored = self.options.get(
            CONF_AREA, self.config_entry.data.get(CONF_AREA))
        if not area_id or area_id == stored:
            return
        coordinator = self.hass.data.get(DOMAIN, {}).get(
            self.config_entry.entry_id)
        if coordinator is None:
            # Not loaded, so there is no device to move yet. The option is still
            # stored, and the config flow's suggested_area applies whenever the
            # device is next created.
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            identifiers={(DOMAIN, coordinator.get_serial_number())})
        if device is not None:
            registry.async_update_device(device.id, area_id=area_id)

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(
            title=self.config_entry.data.get(CONF_USERNAME), data=self.options
        )
