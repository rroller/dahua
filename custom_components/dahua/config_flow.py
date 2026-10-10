"""Adds config flow (UI flow) for Dahua IP cameras."""

import asyncio
import logging
import ssl

import voluptuous as vol

from aiohttp import (
    ClientConnectorError,
    ClientResponseError,
    ClientSession,
    ClientSSLError,
    TCPConnector,
)

from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.data_entry_flow import section
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector

from . import dahua_utils
from . import (
    ISSUE_CHANNEL_NOT_ADDED,
    _async_probe_tcp,
    entry_coordinators,
    is_synthesised_identity,
)
from .client import DahuaClient
from .discovery import async_probe as async_probe_identity
from .dhip import DhipLoginRefused, async_dhip_session
from .migrate import CHANNEL_SUBENTRY
from .flow_preview import async_drop_preview, async_store_preview, preview_url
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
    CONF_POLL_VIDEO_MOTION,
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

# SSL_CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2
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

ALL_EVENTS = [
    "VideoMotion",
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


def channel_unique_id(serial: str, channel) -> str:
    """The entry id for one channel of one device.

    The bare serial for channel 0 and `serial_N` above it. This was written out
    separately in the add step and the import step, and **not at all** in the
    reconfigure step, which is how reconfiguring a channel came to leave the id
    behind. One function so the three cannot drift again.
    """
    index = int(channel or 0)
    return serial if index == 0 else "{0}_{1}".format(serial, index)


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


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def redirected_to_https(exception: BaseException) -> bool:
    """Whether this failure arrived only after the device redirected us to HTTPS.

    A 401 at the end of a redirect chain is not a refused credential. With HTTPS
    switched on, a DHI-NVR4108-8P-4KS2 answers the CGI endpoint on port 80 with

        HTTP/1.1 302 Moved Temporarily
        Location: https://192.168.178.54:443/cgi-bin/magicBox.cgi?action=getMachineName

    aiohttp follows that, the digest exchange does not survive the change of scheme
    and port, and the device answers the second request unauthenticated. The user is
    then told the camera rejected their username and password, and because "auth" is
    in TRANSPORT_WORKED the port and HTTPS fields stay hidden, which is the one place
    they would have fixed it (#947).

    Read from the exception rather than probed, because aiohttp already has it:
    `raise_for_status` passes the response's redirect history, and `request_info`
    carries the URL the last request actually went to.
    """
    history = getattr(exception, "history", None) or ()
    if not any(
        getattr(response, "status", None) in REDIRECT_STATUSES for response in history
    ):
        return False
    url = getattr(getattr(exception, "request_info", None), "url", None)
    return getattr(url, "scheme", None) == "https"


def describe_setup_failure(exception: BaseException) -> str:
    """Which translation key explains why a device could not be added.

    The form previously said "Username, Password, or Address is wrong" whatever
    happened, which is true of exactly one of these and actively misleading for
    the rest. A person told their password is wrong checks their password.

    Only 401 and 403 are credentials. Everything else is the device not being
    where, or not being what, we were told.
    """
    if isinstance(exception, ClientResponseError):
        # 401 only, and not 403. _is_login_refused draws that line deliberately: a
        # 403 means the login was accepted and this account is not allowed that
        # endpoint, which a restricted Dahua user really can hit, so it keeps the
        # identity fallback instead of raising. That means a 403 cannot reach here at
        # all, and listing it as a credentials failure said the opposite of what the
        # other function documents.
        if exception.status == 401:
            # Checked before "auth" because a 401 that only arrived after a
            # redirect is not evidence about the credentials at all, and naming it
            # auth is what hides the two fields that fix it.
            if redirected_to_https(exception):
                return "https_redirect"
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
    # Order matters here and is not stylistic, and getting it wrong is what made
    # ssl_error unreachable. aiohttp's TLS failures are *connection* errors:
    #
    #   ClientOSError(ClientConnectionError, OSError)
    #   ClientConnectorError(ClientOSError)
    #   ClientSSLError(ClientConnectorError)
    #   ClientConnectorSSLError(ClientSSLError, ssl.SSLError)
    #
    # so a ClientConnectorError test placed first matches every TLS failure a
    # request can raise, and the ssl.SSLError branch below it can never run. The
    # TLS test has to come first. The bare ssl.SSLError arm is kept for a failure
    # raised outside a request, which is the only way that type arrives alone.
    if isinstance(exception, (ClientSSLError, ssl.SSLError)):
        return "ssl_error"
    if isinstance(exception, ClientConnectorError):
        return "cannot_connect"
    # TimeoutError is also an OSError subclass, so the generic connection case
    # has to stay last or it swallows that too.
    if isinstance(exception, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
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
            await client.async_get_remote_devices()
        )
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug(
            "No RemoteDevice table to check channel %s against", index, exc_info=True
        )
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


async def async_explain_http_service_off(
    address: str, username: str, password: str, reason: str
) -> str:
    """Say which of two very different devices answered only on Dahua's own ports.

    `http_service_off` tells the user to switch HTTP and CGI on. For a camera
    with its web service off that is right. For an indoor monitor that serves no
    HTTP at all it is a switch that does not exist: #949 measured a VTH5221D
    (3.000.0012000.0.R) with only 5000 and 37777 open, where the login completes
    over DHIP and nothing else does.

    So the typed credentials are tried once over DHIP, which is the only login
    this device can be given:

    - refused: the credentials are wrong, which the HTTP failure could not show.
    - accepted, and the device says it is a VTH: say what it is, and that the
      integration cannot add one yet, instead of creating an entry whose setup and
      polling are all HTTP and would never load.
    - anything else: the original reason, unchanged.

    One attempt only, and only on this path, where HTTP never got as far as a
    login, so it is the first login these credentials have been offered and adds
    nothing towards the device's lockout.
    """
    if reason != "http_service_off":
        return reason
    try:
        async with async_dhip_session(address, username, password) as session:
            reply = await session.call("magicBox.getDeviceClass")
            device_class = (
                str((reply.get("params") or {}).get("type") or "").strip().upper()
            )
            if not device_class:
                # getDeviceClass is what a VTH2421F-P answers over RPC2; on the
                # VTH5221D's 3.000 firmware only getDeviceType has been measured
                # over DHIP ("VTH5221D"). Asked only when the class did not come
                # back, so a device that says what it is is never second-guessed.
                kind = await session.call("magicBox.getDeviceType")
                model = (
                    str((kind.get("params") or {}).get("type") or "").strip().upper()
                )
                if model.startswith("VTH"):
                    device_class = "VTH"
    except DhipLoginRefused:
        return "auth"
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug(
            "No DHIP login at %s to explain the missing HTTP service",
            address,
            exc_info=True,
        )
        return reason
    if device_class == "VTH":
        return "vth_without_http"
    return reason


# camera.py already records that these devices refuse a snapshot under load, and a
# recorder refuses more readily than a camera. So a still that has not arrived quickly
# is one to do without rather than wait for: the form it decorates is useful without
# it, and the add is already slow enough on a sixteen channel recorder.
PREVIEW_TIMEOUT_SECONDS = 5

# Only ever seen if the image fails to load, which is why it says where it came from
# rather than describing the picture. Not translated: it lives in the markdown handed
# to the dialog as a placeholder value, and a placeholder cannot carry a translation.
PREVIEW_ALT_TEXT = "Snapshot from this camera"


async def async_fetch_preview(
    username, password, address, port, rtsp_port, channel, use_https=None
):
    """Return one still from a channel, or None. Never raises.

    A picture is a nicety, so every way of not getting one -- no snapshot endpoint, a
    device that refuses under load, an ONVIF channel on a recorder that answers 400,
    a timeout -- has to end with the flow carrying on unchanged.
    """
    connector = TCPConnector(ssl=SSL_CONTEXT)
    session = ClientSession(connector=connector)
    try:
        client = DahuaClient(
            username, password, address, port, rtsp_port, session, use_https
        )
        return await asyncio.wait_for(
            client.async_get_snapshot(int(channel)), PREVIEW_TIMEOUT_SECONDS
        )
    except Exception as exception:  # pylint: disable=broad-except
        # Debug, not warning: this failing is not a fault and the user is about to be
        # shown a working form. The connection itself has already been proven.
        _LOGGER.debug(
            "No preview still for channel %s at %s (%s)", channel, address, exception
        )
        return None
    finally:
        await session.close()


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
        # The markdown for the still shown on the naming step, and the token holding
        # the bytes behind it. None means not asked for yet; "" means asked and the
        # device had nothing to give, which is remembered so it is asked only once.
        self._preview_markdown = None
        self._preview_token = None

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

        name = (
            info.get("DeviceType")
            or info.get("MachineName")
            or discovery_info.hostname
            or "Dahua device"
        )
        # Shown on the discovery card in the integrations list.
        self.context["title_placeholders"] = {"name": name, "address": address}
        return await self.async_step_user()

    @callback
    def _async_id_taken_by_another(self, entry, unique_id: str) -> bool:
        """Is some other entry already this channel?

        Moving an entry onto an id another one holds would leave two entries reading
        one camera, with duplicate entities and no way for Home Assistant to tell
        them apart.
        """
        return any(
            other.entry_id != entry.entry_id and other.unique_id == unique_id
            for other in self._async_current_entries()
        )

    @callback
    def _async_report_channel_not_added(self, import_data, reason) -> None:
        """Say which channel could not be added, somewhere the user will see it.

        A repair rather than a log line, because the log is not where somebody looks
        after ticking boxes on a form. Not fixable: what went wrong is on the device
        or the network, and retrying it for them would just fail again.

        Persistent, because this arrives while Home Assistant is still starting the
        other channels and a card that vanishes on the next restart is no use to
        somebody reading it afterwards.

        The channel is named the way the recorder names it, one higher than the index
        stored here, since that is the number the user ticked.
        """
        address = import_data.get(CONF_ADDRESS)
        index = int(import_data.get(CONF_CHANNEL) or 0)
        _LOGGER.warning(
            "Channel %s on %s could not be added (%s), so it has been skipped",
            index + 1,
            address,
            reason or "unknown",
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_CHANNEL_NOT_ADDED.format(address, index),
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="channel_not_added",
            translation_placeholders={
                "address": str(address),
                "channel": str(index + 1),
                "reason": str(reason or "unknown"),
            },
        )

    @callback
    def _async_heal_siblings(self, unique_id: str, address: str) -> None:
        """Move a recorder's other channels to the address just confirmed.

        `_abort_if_unique_id_configured(updates=...)` heals the entry whose id
        matches, and a recorder is one entry per channel. Healing only the matched
        one leaves the other fifteen pointing at an address the device no longer has,
        so they stay broken and each has to be reconfigured by hand.

        Same serial is the same physical device, so this is not a guess, and it is
        what the Gold discovery-update-info rule asks for. Entries already on the
        right address are left alone so nothing is rewritten for no reason, and the
        matched entry is left to `updates=`.
        """
        head, _sep, tail = unique_id.rpartition("_")
        serial = head if head and tail.isdigit() else unique_id
        for entry in self._async_entries_for_serial(serial):
            if entry.unique_id == unique_id:
                continue
            if entry.data.get(CONF_ADDRESS) == address:
                continue
            _LOGGER.debug(
                "Moving %s from %s to %s, same serial",
                entry.unique_id,
                entry.data.get(CONF_ADDRESS),
                address,
            )
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_ADDRESS: address}
            )

    def _async_entries_for_serial(self, serial: str) -> list:
        """Every entry for one device, including a recorder's other channels.

        The unique_id is the bare serial for channel 0 and `serial_N` above it, so a
        prefix match is what covers a whole recorder. Without it, somebody who added
        only channel 3 would have that recorder announce itself as undiscovered for
        ever.
        """
        prefix = serial + "_"
        return [
            entry
            for entry in self._async_current_entries()
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
                    unique_id = channel_unique_id(
                        data["serialNumber"], user_input[CONF_CHANNEL]
                    )
                    await self.async_set_unique_id(unique_id)
                    # Heal the siblings first, because the call below raises.
                    self._async_heal_siblings(unique_id, user_input[CONF_ADDRESS])
                    # With no updates= this aborted and left the old address in
                    # place, so somebody whose camera changed IP re-added it, was
                    # told "already configured", and still had a broken entry with
                    # nothing pointing at Reconfigure. Same serial is the same
                    # device, so the address we have just talked to is the right one.
                    self._abort_if_unique_id_configured(
                        updates={CONF_ADDRESS: user_input[CONF_ADDRESS]}
                    )

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
        roughly how long it can take, and DISCOVERY_TIMEOUT_SECONDS bounds the
        whole search -- which it did not until #1000. The ceiling was around
        the snapshot probes alone, and the two config reads before them bound
        themselves at TIMEOUT_SECONDS each, so the dialog named 30 seconds
        while the step could take 70. Somebody still watching it past the
        number on screen had no way to tell a slow search from a frozen one.

        A standalone camera has no such table and refuses the first read, so
        the dialog is brief rather than absent. That is the honest thing to
        show: the search did happen.
        """
        if self._discovery_task is None:
            self._discovery_task = self.hass.async_create_task(
                self._async_discover_channels(
                    self.init_info, int(self.init_info[CONF_CHANNEL])
                )
            )

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
                description_placeholders={"seconds": str(DISCOVERY_TIMEOUT_SECONDS)},
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
            next_step_id="channels" if self._found_channels else "name"
        )

    async def _async_discover_channels(self, user_input, exclude) -> dict:
        """The search, inside the ceiling the dialog puts on screen.

        The ceiling lives here rather than around the one call site, so every
        caller gets it. It was a wrapper above this method for one revision,
        and `test_a_recorder_that_probes_forever_gives_up` -- which calls this
        directly -- went from asserting the ceiling to hanging on it, which is
        a fair description of what a caller stepping past it would do in
        production too.

        Expiry is an ordinary outcome: nothing offered, and the camera the user
        asked for still added, which is what every other failure in here does.
        """
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
                return await self._async_search_channels(user_input, exclude)
        except (Exception, asyncio.CancelledError):  # pylint: disable=broad-except
            _LOGGER.debug(
                "The channel search did not finish inside %ss",
                DISCOVERY_TIMEOUT_SECONDS,
                exc_info=True,
            )
            return {}

    async def _async_search_channels(self, user_input, exclude) -> dict:
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

        The time this may take is bounded by its caller, which is the only
        caller: `_async_discover_channels` holds the whole of it inside
        DISCOVERY_TIMEOUT_SECONDS, which is the number the dialog shows.
        """
        session = ClientSession(connector=TCPConnector(ssl=SSL_CONTEXT))
        try:
            client = DahuaClient(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_ADDRESS],
                user_input[CONF_PORT],
                user_input[CONF_RTSP_PORT],
                session,
                True if user_input.get(CONF_USE_HTTPS) else None,
            )
            try:
                devices = dahua_utils.parse_remote_devices(
                    await client.async_get_remote_devices()
                )
            except Exception:  # pylint: disable=broad-except
                # A standalone camera has no such table. Nothing to offer is an
                # ordinary answer rather than a failure, so this is not a
                # warning. It is logged because the alternative is a feature
                # that can do nothing at all and leave no trace of why.
                _LOGGER.debug(
                    "No RemoteDevice table on %s, so no channels to offer",
                    user_input[CONF_ADDRESS],
                    exc_info=True,
                )
                return {}

            candidates = [
                index
                for index in dahua_utils.channels_worth_offering(devices)
                if index != exclude
            ]
            _LOGGER.debug(
                "%s: %d slots, %s worth offering, %d after excluding channel %s",
                user_input[CONF_ADDRESS],
                len(devices),
                dahua_utils.channels_worth_offering(devices),
                len(candidates),
                exclude,
            )
            if not candidates:
                return {}

            try:
                titles = dahua_utils.parse_channel_titles(
                    await client.async_get_config("ChannelTitle")
                )
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
            #
            # The ceiling itself is in _async_discover_channels now, around
            # every read rather than only these. A wait_for here as well would
            # name the same number twice and still not be the real bound.
            answered = await asyncio.gather(*[live(i) for i in candidates])
            found = {
                index: titles.get(index) or "Channel {0}".format(index + 1)
                for index in answered
                if index is not None
            }
            _LOGGER.debug(
                "%s: %d of %d candidates answered a snapshot: %s",
                user_input[CONF_ADDRESS],
                len(found),
                len(candidates),
                sorted(found),
            )
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
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ALL_CHANNELS, default=False): bool,
                    vol.Optional(CONF_EXTRA_CHANNELS, default=[]): cv.multi_select(
                        {
                            str(index): "Channel {0}: {1}".format(index + 1, name)
                            for index, name in sorted(self._found_channels.items())
                        }
                    ),
                }
            ),
            description_placeholders={"count": str(len(self._found_channels))},
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
            label = (
                "Channel {0}: {1}".format(index + 1, title)
                if title
                else "Channel {0}".format(index + 1)
            )
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
            data_schema=vol.Schema(
                {
                    # Optional, and a blank answer means no area rather than an
                    # error: somebody who has not made their areas yet must still
                    # be able to finish adding their cameras.
                    vol.Optional(label): selector.AreaSelector()
                    for label in self._area_fields
                }
            ),
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
            import_data[CONF_USERNAME],
            import_data[CONF_PASSWORD],
            import_data[CONF_ADDRESS],
            import_data[CONF_PORT],
            import_data[CONF_RTSP_PORT],
            import_data[CONF_CHANNEL],
            True if import_data.get(CONF_USE_HTTPS) else None,
        )
        if data is None:
            # An import-sourced flow renders no card, so aborting here used to be
            # completely silent: no error, no card, no repair, and the abort reason
            # was an *error* key with no string behind it anyway. The user ticked
            # sixteen channels, got twelve, and nothing said which or why.
            self._async_report_channel_not_added(import_data, error)
            return self.async_abort(reason="channel_not_added")

        serial = data.get("serialNumber")
        if serial:
            unique_id = channel_unique_id(serial, import_data[CONF_CHANNEL])
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

        return self.async_create_entry(title=import_data[CONF_NAME], data=import_data)

    async def async_step_name(self, user_input=None):
        """Handle a flow to configure the camera name."""
        self._errors = {}

        if user_input is not None:
            if self.init_info is not None:
                self.init_info.update(user_input)
                return self.async_create_entry(
                    title=self.init_info["name"],
                    data=self.init_info,
                    subentries=self._channel_subentries(),
                )

        return await self._show_config_form_name(user_input or self.init_info)

    def _channel_subentries(self) -> list:
        """One subentry per channel the user chose, primary included -- or none.

        This used to start a separate config flow per extra channel, so a 64
        channel recorder became 64 config entries and removing it meant 64
        deletions (#827). One entry with a subentry each is the shape Home
        Assistant expects of a hub, and the shape its own delete button
        understands.

        When there *are* extra channels the primary is a subentry too, rather
        than living only in the entry's data. Setup reads channels from the
        subentries whenever there are any, so leaving the primary out would bring
        up every channel except the one the user started from.

        **One channel gets no subentries at all**, which is the single-camera
        shape rather than a hub with one member. Reported by @roalvesrj on #830:
        a standalone IMOU came up with a `channel` subentry and its device nested
        underneath, reading as a recorder that happens to have one channel. That
        nesting is Home Assistant's own rendering of subentry ownership, so the
        only way out of it is not to have the subentry.

        Nothing is lost by it. `channel_configs()` already treats an entry with
        no subentries as one channel described by its own `data`,
        `events_for_channel()` has the matching branch, and every per-channel
        setting the subentry flow edits is also on the entry's own Configure
        form, which is what a single camera uses.

        And the flat shape is what single cameras already have: the #827 merge
        only groups entries that share a host (`if len(group) > 1`), so a lone
        camera upgrading from 0.9.x was never touched. Before this, the same
        camera looked different depending on when it was added.
        """
        if not self._extra_channels:
            return []
        subentries = []
        for index in [self.init_info[CONF_CHANNEL]] + list(self._extra_channels):
            data = dict(self.init_info)
            data[CONF_CHANNEL] = index
            if index != self.init_info[CONF_CHANNEL]:
                data[CONF_NAME] = self._found_channels.get(
                    index, "Channel {0}".format(index + 1)
                )
                # Every channel inherits init_info, which carries the *primary's*
                # area. Without this, ticking one area for the recorder itself
                # would silently file every other channel there too.
                area = self._channel_areas.get(index)
                if area:
                    data[CONF_AREA] = area
                else:
                    data.pop(CONF_AREA, None)
            subentries.append(
                ConfigSubentryData(
                    data=data,
                    subentry_type=CHANNEL_SUBENTRY,
                    title=data[CONF_NAME],
                    unique_id="%s_%s" % (data[CONF_ADDRESS], index),
                )
            )
        return subentries

    @callback
    def _set_flow_title(self, entry) -> None:
        """Fill the placeholders `config.flow_title` needs, from an existing entry.

        `flow_title` is "{name} ({address})", and Home Assistant renders it on the card
        that appears in the integrations list when a flow wants attention. It is filled
        from `context["title_placeholders"]`, which only the DHCP discovery step was
        setting.

        A flow started from an existing entry therefore rendered the title with nothing
        to substitute, and the frontend showed
        `Translation [formatjs Error: MISSING_VALUE ...]` where the device name belongs.
        That is the card a user meets at the worst moment: their cameras have just
        stopped and the thing telling them so is an error about an error.
        """
        data = (entry.data if entry is not None else None) or {}
        # `or`, not a .get default: an entry carrying an explicit None would
        # hand that straight through and leave the placeholder unfilled, which
        # is the whole failure this exists to prevent.
        address = data.get(CONF_ADDRESS) or ""
        title = (entry.title if entry is not None else None) or ""
        self.context["title_placeholders"] = {
            "name": title or address or "Dahua",
            "address": address,
        }

    async def async_step_reauth(self, entry_data):
        """Handle reauthentication when credentials become invalid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._set_flow_title(self._reauth_entry)
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
                # The entry's own setting, not the default. Omitting this left
                # use_https as None, which DahuaClient resolves as
                # `int(port) == 443` -- so an entry created with HTTPS on any
                # other port was re-tested over plain HTTP, failed, and told the
                # user nothing answered on a form whose whole premise is that
                # their password went stale. There was no way out of that from
                # the UI. async_step_reconfigure passes it correctly.
                True if entry.data.get(CONF_USE_HTTPS) else None,
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
        """Show the reauthentication form.

        The description says "The credentials for {name} are no longer valid", so
        it needs a placeholder. Without one there is nothing to substitute and the
        sentence renders with a literal `{name}` or a hole in it. Home Assistant
        fills the dialog *heading* from title_placeholders, which is a different
        channel and does not reach the body.

        The username is prefilled because it is usually not what expired. Asking
        somebody to retype it on a form they only ever see when something is
        already broken buys nothing.
        """
        entry = getattr(self, "_reauth_entry", None)
        current = entry.data if entry is not None else {}
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=current.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={
                "name": (
                    entry.title
                    if entry is not None and entry.title
                    else current.get(CONF_ADDRESS, "this device")
                ),
            },
            errors=self._errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return DahuaOptionsFlowHandler()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry) -> dict:
        """Let a channel be reconfigured on its own.

        Without this the settings that belong to one channel would be unreachable
        after it was added, which would be a step backwards from an entry per
        channel: each of those had its own options screen. Home Assistant renders
        this as a Configure button on the channel itself.
        """
        return {CHANNEL_SUBENTRY: DahuaChannelSubentryFlow}

    async def async_step_reconfigure(self, user_input=None):
        """Change an existing entry's connection settings.

        Credentials are left alone - those are what async_step_reauth is for.
        """
        self._errors = {}
        entry = self._get_reconfigure_entry()
        self._set_flow_title(entry)

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
                # The channel is on this form, and changing it used to leave the
                # unique_id behind. Two things went wrong with that: another entry
                # could then be added for the channel this one had moved to, so two
                # entries polled one camera and the duplicate guard could not see it;
                # and the channel this one moved *away* from became unaddable for
                # good, because a fresh add computes the id this entry is still
                # holding and aborts.
                serial = data.get("serialNumber")
                if serial:
                    moved_to = channel_unique_id(serial, user_input[CONF_CHANNEL])
                    if moved_to != entry.unique_id:
                        if self._async_id_taken_by_another(entry, moved_to):
                            self._errors[CONF_CHANNEL] = "already_configured"
                            return await self._show_reconfigure_form(entry, user_input)
                        return self.async_update_reload_and_abort(
                            entry, unique_id=moved_to, data_updates=user_input
                        )
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
            self._errors["base"] = error or "auth"

        return await self._show_reconfigure_form(entry, user_input)

    async def _show_reconfigure_form(self, entry, user_input=None):
        """The reconfigure form, prefilled from the entry and from what was typed.

        Separated out because a channel that collides with another entry has to come
        back to this form with an error on that field, rather than aborting.
        """
        current = {**entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADDRESS, default=current.get(CONF_ADDRESS, "")
                    ): str,
                    vol.Required(
                        CONF_PORT, default=str(current.get(CONF_PORT, "80"))
                    ): vol.All(cv.port, vol.Coerce(str)),
                    vol.Required(
                        CONF_RTSP_PORT, default=str(current.get(CONF_RTSP_PORT, "554"))
                    ): vol.All(cv.port, vol.Coerce(str)),
                    vol.Required(
                        CONF_CHANNEL, default=int(current.get(CONF_CHANNEL, 0))
                    ): vol.All(vol.Coerce(int), vol.Range(min=0)),
                    vol.Optional(
                        CONF_USE_HTTPS, default=bool(current.get(CONF_USE_HTTPS, False))
                    ): bool,
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
                vol.Coerce(int), vol.Range(min=0)
            ),
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
                cv.port, vol.Coerce(str)
            )
            fields[vol.Required(CONF_RTSP_PORT, default="554")] = vol.All(
                cv.port, vol.Coerce(str)
            )
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
        known.update(
            {
                key: value
                for key, value in (user_input or {}).items()
                if key != CONF_PASSWORD and value not in (None, "")
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                self._user_schema(
                    reveal_transport=any(
                        reason not in TRANSPORT_WORKED
                        for reason in self._errors.values()
                    )
                ),
                known,
            ),
            errors=self._errors,
        )

    async def _show_config_form_name(
        self, user_input
    ):  # pylint: disable=unused-argument
        """Show the configuration form to edit location data."""
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=user_input[CONF_NAME]): str,
                }
            ),
            description_placeholders={"preview": await self._async_preview_markdown()},
            errors=self._errors,
        )

    async def _async_preview_markdown(self) -> str:
        """A still from the channel being added, as markdown, or "" if there is none.

        This is the last step before the entry exists and the only one that shows what
        was actually configured, which is why the picture goes here rather than on a
        step of its own: a wrong channel number is caught by looking, and the picture
        is also what tells somebody what to call the camera.

        Asked for once. This form is re-shown whenever the name comes back empty, and a
        device that has already handed over an image should not be asked again -- nor
        should one that refused, which is why a failure is remembered as "" rather than
        retried.
        """
        if self._preview_markdown is not None:
            return self._preview_markdown

        self._preview_markdown = ""
        info = self.init_info or {}
        image = await async_fetch_preview(
            info.get(CONF_USERNAME),
            info.get(CONF_PASSWORD),
            info.get(CONF_ADDRESS),
            info.get(CONF_PORT),
            info.get(CONF_RTSP_PORT),
            info.get(CONF_CHANNEL),
            True if info.get(CONF_USE_HTTPS) else None,
        )
        if image:
            self._preview_token = async_store_preview(self.hass, image)
            self._preview_markdown = "![{0}]({1})".format(
                PREVIEW_ALT_TEXT, preview_url(self._preview_token)
            )
        return self._preview_markdown

    @callback
    def async_remove(self) -> None:
        """Drop the held still, whichever way this flow ended.

        Called for a created entry, an abort and a cancelled dialog alike, so this is
        the one place that covers all three. The store expires entries on its own as
        well, because this cannot run for a flow the process did not live to finish.
        """
        if self._preview_token:
            async_drop_preview(self.hass, self._preview_token)
            self._preview_token = None

    async def _test_credentials(
        self,
        username,
        password,
        address,
        port,
        rtsp_port,
        channel,
        use_https=None,
        check_channel=False,
    ):
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
            client = DahuaClient(
                username, password, address, port, rtsp_port, session, use_https
            )
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
                if is_synthesised_identity(data.get("serialNumber")):
                    # The identity is a hash of the credentials, so it changes whenever
                    # the password does and the same camera comes back as a new device
                    # (#805, #320). DHDiscover needs no credentials and the devices that
                    # land here are the ones with no magicBox.cgi, which is an HTTP
                    # service setting and does not touch the SDK port. Measured: the
                    # probe's SerialNo is byte for byte what magicBox.cgi returns.
                    found = await async_probe_identity(address)
                    from_network = (found or {}).get("SerialNo")
                    if from_network:
                        _LOGGER.debug(
                            "%s would not identify itself over HTTP, so the network "
                            "probe's serial is used instead of a hash",
                            address,
                        )
                        data["serialNumber"] = from_network
                if name_is_a_hash:
                    # Only what the user is shown. The identity above is separate: a
                    # device can give a usable serial and still have no readable name.
                    data["name"] = fallback_device_name(address, channel)
                return data, None
            # It answered, but not with anything recognisable.
            return None, "unexpected_reply"
        except Exception as exception:  # pylint: disable=broad-except
            reason = describe_setup_failure(exception)
            _LOGGER.error(
                "Could not connect to Dahua device at %s (%s). For iMou devices "
                "see https://github.com/rroller/dahua/issues/6",
                address,
                reason,
                exc_info=exception,
            )
            reason = await async_refine_connection_failure(address, reason)
            return None, await async_explain_http_service_off(
                address, username, password, reason
            )
        finally:
            await session.close()


# The collapsed group the platform toggles live in on the options form. Named here
# because both the schema and the flattening need the same string.
OPTIONS_SECTION_PLATFORMS = "platforms"


def _flatten_sections(user_input: dict) -> dict:
    """Lift a section's values back to the top level.

    Home Assistant returns a section as a nested dict, and everything that reads
    these options does `entry.options.get("binary_sensor")`. Flattening on the way in
    keeps the stored shape exactly as it was, so this is a change to the form and not
    to the data.

    Only the sections this form declares are lifted. Flattening anything that happens
    to be a dict would eventually swallow an option whose value is legitimately one.
    """
    flat = dict(user_input)
    for name in (OPTIONS_SECTION_PLATFORMS,):
        nested = flat.pop(name, None)
        if isinstance(nested, dict):
            flat.update(nested)
    return flat


class DahuaOptionsFlowHandler(config_entries.OptionsFlow):
    """Dahua config flow options handler."""

    async def async_step_init(self, user_input=None):  # pylint: disable=unused-argument
        """Manage the options."""
        self.options = dict(self.config_entry.options)
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        if user_input is not None:
            flat = _flatten_sections(user_input)
            await self._async_move_device(flat.get(CONF_AREA))
            self.options.update(flat)
            return await self._update_options()

        # Eight "<platform> enabled" toggles used to be the first eight fields of a
        # nineteen field form, so somebody opening Configure to change scan_interval
        # scrolled past all of them. They are the least likely thing anybody came
        # here to change, so they go in a collapsed section. Home Assistant nests a
        # section's values in what it hands back, and every reader of these does
        # `entry.options.get("binary_sensor")`, so _flatten_sections lifts them out
        # again and the stored shape is unchanged.
        schema = {
            vol.Required(OPTIONS_SECTION_PLATFORMS): section(
                vol.Schema(
                    {
                        vol.Required(x, default=self.options.get(x, True)): bool
                        for x in sorted(PLATFORMS)
                    }
                ),
                {"collapsed": True},
            )
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
            vol.Required(
                CONF_POLL_VIDEO_MOTION,
                default=self.options.get(CONF_POLL_VIDEO_MOTION, False),
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
                    CONF_AREA, self.config_entry.data.get(CONF_AREA, "")
                ),
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
        stored = self.options.get(CONF_AREA, self.config_entry.data.get(CONF_AREA))
        if not area_id or area_id == stored:
            return
        channels = entry_coordinators(self.config_entry)
        coordinator = next(iter(channels.values()), None)
        if coordinator is None:
            # Not loaded, so there is no device to move yet. The option is still
            # stored, and the config flow's suggested_area applies whenever the
            # device is next created.
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            identifiers={(DOMAIN, coordinator.get_serial_number())}
        )
        if device is not None:
            registry.async_update_device(device.id, area_id=area_id)

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(
            title=self.config_entry.data.get(CONF_USERNAME), data=self.options
        )


class DahuaChannelSubentryFlow(config_entries.ConfigSubentryFlow):
    """Change the settings that belong to one channel of a recorder.

    These used to live in the channel's own config entry options, because a
    channel *was* an entry. #827 merged the entries, so they live on the channel's
    subentry and this is how they are edited.

    Only the genuinely per channel ones are here. Anything host wide -- the poll
    interval, whether to use RPC2, which platforms to create -- stays on the entry
    and is edited from its Configure button, because asking the same question once
    per channel on a 64 channel recorder would be its own kind of unusable.
    """

    async def async_step_user(self, user_input=None):
        """Add a channel to an existing recorder.

        `async_get_supported_subentry_types` registers this flow, so Home Assistant
        shows an "Add a channel" button on a recorder, but the flow only implemented
        reconfigure -- pressing the button raised "Handler DahuaChannelSubentryFlow
        doesn't support step user" (#947). This adds the step.

        A new channel is the same shape setup builds for an extra channel in
        `_channel_subentries`: the recorder's connection and host-wide settings are
        inherited from the parent entry, and only the channel index, the name, the
        area and the event list are per channel. The channel index is 0-based, as
        the add form itself asks for it. A channel that already has a subentry -- or
        the primary channel on a single-camera entry that has none -- is refused
        rather than duplicated.
        """
        entry = self._get_entry()
        base = dict(entry.data)

        taken = {sub.data.get(CONF_CHANNEL) for sub in entry.subentries.values()}
        if not entry.subentries:
            # A flat entry has no subentries; its one channel lives in the entry
            # data, and adding a second is what turns it into a hub.
            taken.add(base.get(CONF_CHANNEL, 0))
        next_free = next(i for i in range(0, 256) if i not in taken)

        errors: dict[str, str] = {}
        if user_input is not None:
            channel = user_input[CONF_CHANNEL]
            if channel in taken:
                errors[CONF_CHANNEL] = "channel_already_added"
            else:
                data = {
                    **base,
                    CONF_CHANNEL: channel,
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_EVENTS: user_input.get(CONF_EVENTS, DEFAULT_EVENTS),
                }
                # "" means no area, which is a real answer; anything else moves the
                # new channel's device into it rather than inheriting the primary's.
                if user_input.get(CONF_AREA):
                    data[CONF_AREA] = user_input[CONF_AREA]
                else:
                    data.pop(CONF_AREA, None)
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=data,
                    unique_id="%s_%s" % (base.get(CONF_ADDRESS), channel),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CHANNEL, default=next_free): vol.All(
                        vol.Coerce(int), vol.Range(min=0)
                    ),
                    vol.Required(
                        CONF_NAME, default="Channel {0}".format(next_free + 1)
                    ): str,
                    vol.Optional(CONF_AREA, default=""): selector.AreaSelector(),
                    vol.Optional(CONF_EVENTS, default=DEFAULT_EVENTS): cv.multi_select(
                        ALL_EVENTS
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        """Show and save one channel's settings."""
        subentry = self._get_reconfigure_subentry()
        data = dict(subentry.data)

        if user_input is not None:
            # An area of "" means "no area", which is a real answer and different
            # from not having been asked, so it is stored rather than dropped.
            merged = {**data, **user_input}
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data=merged,
                title=merged.get(CONF_NAME) or subentry.title,
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME, default=data.get(CONF_NAME, subentry.title)
                    ): str,
                    vol.Optional(
                        CONF_AREA, default=data.get(CONF_AREA) or ""
                    ): selector.AreaSelector(),
                    vol.Optional(
                        CONF_EVENTS, default=data.get(CONF_EVENTS, DEFAULT_EVENTS)
                    ): cv.multi_select(ALL_EVENTS),
                    vol.Required(
                        CONF_AUTO_DETECT_CHANNEL,
                        default=data.get(CONF_AUTO_DETECT_CHANNEL, True),
                    ): bool,
                    vol.Required(
                        CONF_NVR_ACTIVE_DETERRENCE,
                        default=data.get(CONF_NVR_ACTIVE_DETERRENCE, False),
                    ): bool,
                    vol.Required(
                        CONF_MANUAL_SIREN, default=data.get(CONF_MANUAL_SIREN, False)
                    ): bool,
                    vol.Required(
                        CONF_MANUAL_SECURITY_LIGHT,
                        default=data.get(CONF_MANUAL_SECURITY_LIGHT, False),
                    ): bool,
                    vol.Required(
                        CONF_DISABLE_BACKCHANNEL,
                        default=data.get(CONF_DISABLE_BACKCHANNEL, False),
                    ): bool,
                }
            ),
            description_placeholders={
                "channel": str(data.get(CONF_CHANNEL, 0)),
                "name": subentry.title,
            },
        )
