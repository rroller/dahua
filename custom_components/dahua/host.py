"""One host, however many channels of it are configured.

Everything in here is keyed by address rather than by config entry: whether the
host is reachable, how many times it has refused credentials, the shared HTTP
connector, the uptime sample that tells a channel apart from a reboot, whether the
device numbers its channels from zero, and the one event stream every channel of a
host reads from.

Split out of __init__.py, which had reached four thousand lines holding three
separate things: this, the coordinator, and the config entry lifecycle. Nothing
here knows what a coordinator is, which is what lets the split run in this
direction. __init__.py and coordinator.py both import from here; this imports from
neither.
"""

from typing import Any, Dict
import asyncio
import logging
import random
import re
import ssl
import time

from aiohttp import ClientError, ClientResponseError, TCPConnector

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .client import clear_host_cache
from .const import CONF_ADDRESS, CONF_PORT, CONF_USE_HTTPS, DOMAIN
from .dahua_utils import parse_event

_LOGGER: logging.Logger = logging.getLogger(__package__)

# A stream that keeps heartbeating but has stopped reporting events looks
# healthy to a read timeout, so recycle it periodically as well.
EVENT_STREAM_MAX_LIFETIME_SECONDS = 3600

# An NVR gets one config entry per channel, and they all start within a moment
# of each other, so a fixed lifetime makes every channel drop and re-attach in
# the same second, once an hour. Spreading them means the device sees a trickle
# of reconnections instead of a burst.
EVENT_STREAM_JITTER = 0.1

# The same applies after a failure: a device that rejected every channel at once
# would otherwise be retried by every channel at once, sixty seconds later.
EVENT_STREAM_RETRY_SECONDS = 60

# A stream that lived this long was working, so reconnect at once. Anything
# shorter gets backed off, because the fast path used to have no delay at all:
# a device closing the socket at eleven seconds reconnected forever, silently.
EVENT_STREAM_HEALTHY_SECONDS = 60

EVENT_STREAM_SHORT_RETRY_SECONDS = 10

# A stream that dies on contact will keep dying on contact: the device is
# refusing us, not hiccuping. Asking again every sixty seconds forever is how a
# device that ran out of connections stays out of connections, because each
# attempt costs it another one. Back off instead, to this ceiling.
EVENT_STREAM_MAX_RETRY_SECONDS = 600

# A single CGI request returns every recorder channel currently in ordinary
# motion. Two seconds matches the integration's existing RPC2 event poller.
VIDEO_MOTION_POLL_SECONDS = 2

# Camera uptime is host-wide. An NVR may have many config entries, one per
# channel, but they all refer to the same physical recorder uptime.
#
# Keep one shared sample per host so coordinator polling does not multiply
# uptime requests by channel count.
_HOST_UPTIME_STATE: dict[str, dict[str, Any]] = {}

_HOST_UPTIME_LOCKS: dict[str, asyncio.Lock] = {}

# NVR channel coordinators normally poll within a moment of each other.
# Reuse the first host uptime read for the others in that poll burst.
HOST_UPTIME_DEDUPE_SECONDS = 5.0


async def _async_get_host_uptime_generation(coordinator) -> int:
    """Poll one host-wide uptime value and return its reboot generation."""

    address = coordinator._address

    state = _HOST_UPTIME_STATE.setdefault(
        address,
        {
            "uptime": None,
            "generation": 0,
            "last_read": 0.0,
        },
    )

    lock = _HOST_UPTIME_LOCKS.setdefault(
        address,
        asyncio.Lock(),
    )

    async with lock:
        # Another channel may have completed the host read while this
        # coordinator was waiting for the lock.
        now = time.monotonic()

        if now - state["last_read"] < HOST_UPTIME_DEDUPE_SECONDS:
            return int(state["generation"])

        try:
            current = await coordinator.client.async_get_uptime_last()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Cache failed attempts too, otherwise every NVR channel may repeat
            # the same failed uptime request during the same poll burst.
            state["last_read"] = time.monotonic()

            # Uptime is an optional enhancement. A device/transport that does
            # not support it must not make the normal coordinator poll fail.
            _LOGGER.debug(
                "Could not read host uptime for %s",
                address,
                exc_info=True,
            )
            return int(state["generation"])

        previous = state["uptime"]

        if previous is not None and current < previous:
            state["generation"] += 1

            _LOGGER.info(
                "Dahua host %s reboot detected " "(uptime %s -> %s, generation=%s)",
                address,
                previous,
                current,
                state["generation"],
            )

        state["uptime"] = current
        state["last_read"] = time.monotonic()

        return int(state["generation"])


def stream_lifetime(lived_seconds: float, received_data: bool) -> float:
    """How long the stream really lasted, for the purpose of retrying it.

    A socket that stayed open an hour and delivered nothing -- not one event, not
    even the heartbeat the subscription asks for every
    EVENT_STREAM_HEARTBEAT_SECONDS -- did not last an hour in any sense that
    should earn an immediate reconnect. It never worked at all.

    This matters because aiohttp's `sock_read` timeout and the deliberate recycle
    raise the *same* exception: `ServerTimeoutError` is a `TimeoutError`. Duration
    alone therefore cannot tell a stream that worked for an hour from one that sat
    mute until the read timeout fired, and reading the second as the first is how
    a device that reports nothing looks healthy forever.

    Silence is safe to judge on because a stream is only ever started when some
    event is wanted, so there is no correctly-subscribed device with nothing to
    say.
    """
    return lived_seconds if received_data else 0.0


def event_stream_retry_delay(
    lived_seconds: float, consecutive_failures: int = 0, received_data: bool = False
) -> float:
    """How long to wait before re-attaching, given how long the stream lasted.

    `received_data` is whether the device sent anything at all on this attach --
    an event or a heartbeat. It separates the two things a short stream can mean.
    A device that refuses attach and one whose firmware hangs up after eight
    seconds of perfectly good events look identical by duration, and only the
    first should be backed off: the second is working, and backing it off to ten
    minutes is how a camera that still detects motion stops reporting any.
    """
    if lived_seconds < 10 and not received_data:
        # Double per successive instant death, so a device that is refusing
        # attach gets asked less often the longer it keeps refusing.
        doublings = max(0, consecutive_failures - 1)
        backoff = EVENT_STREAM_RETRY_SECONDS * (
            2 ** min(doublings, MAX_BACKOFF_DOUBLINGS)
        )
        return jittered(min(backoff, EVENT_STREAM_MAX_RETRY_SECONDS))
    if lived_seconds < EVENT_STREAM_HEALTHY_SECONDS:
        return jittered(EVENT_STREAM_SHORT_RETRY_SECONDS)
    return 0.0


# Guard on the exponent so the arithmetic stays sane for a device that has been
# failing for a week. The time ceilings below are what actually bind.
MAX_BACKOFF_DOUBLINGS = 6

# How many times a host may refuse these credentials before the integration
# stops offering them.
#
# Not one, because a single 401 is not proof of a wrong password: channels of
# one NVR share a digest challenge, and a nonce that races between them is
# refused exactly like a bad credential. Treating the first one as fatal is
# what #714 was.
#
# Not many either, because a 401 only reaches here after DigestAuth has
# already spent its own MAX_AUTH_ATTEMPTS on it, including two passes through
# the same-nonce refusal path. A 401 that survives all of that is much more
# likely to be real than a raw one, so the budget here can be small.
MAX_AUTH_REFUSALS = 3


def jittered(seconds: float, fraction: float = EVENT_STREAM_JITTER) -> float:
    """Spread a shared interval so simultaneous callers stop being simultaneous."""
    if seconds <= 0 or fraction <= 0:
        return seconds
    spread = seconds * fraction
    return max(1.0, seconds + random.uniform(-spread, spread))


SSL_CONTEXT = ssl.create_default_context()

SSL_CONTEXT.set_ciphers("DEFAULT")

SSL_CONTEXT.check_hostname = False

SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# Every config entry for one NVR used to open its own connection pool, so the
# channels of a single device never reused a connection between them. Share one
# pool per address instead, reference counted so the last entry to unload closes
# it. Sessions stay per entry; only the connector underneath is shared.
_HOST_CONNECTORS: dict = {}

# How many consecutive failed refreshes before we call a host unreachable. At
# the default 30s interval that is about two and a half minutes, long enough to
# ride out a single dropped poll.
UNREACHABLE_AFTER_FAILURES = 5

# A TCP probe is cheap but not free, and a wedged host fails every single poll.
HTTPS_PROBE_MIN_INTERVAL = 600

ISSUE_UNREACHABLE = "unreachable_{0}"

ISSUE_HTTP_DEAD_HTTPS_AVAILABLE = "http_dead_https_available_{0}"

# address -> {"consecutive": int, "since": float, "entry_ids": set, "last_probe": float}
#
# Module level rather than on the coordinator, for two reasons. A failed setup
# never publishes its coordinator to hass.data, because
# async_config_entry_first_refresh raises first, so every retry would build and
# discard a fresh counter. And an NVR has one config entry per channel, so the
# count must be shared or eight channels of one box raise eight separate cards.
_HOST_FAILURES: dict = {}

# Which (device, channel) pairs have already had a capability refusal reported, so a
# recorder that refuses on every poll produces one warning rather than one per poll. It
# refused 949 times in nine hours here, each one a WARNING, which buries everything else.
_CAPABILITY_REFUSALS_REPORTED: set = set()

_HOST_CHANNEL_BASE: dict = {}

_HOST_CHANNEL_BASE_LOCKS: dict = {}

# What the network probe said about a host, and the lock that stops a recorder's eleven
# entries all asking at once. `None` is cached as an answer: a device that did not reply
# will not reply for the next channel either, and re-probing eleven times would spend
# eleven timeouts on it.
_HOST_NETWORK_IDENTITY: dict = {}

_HOST_NETWORK_IDENTITY_LOCKS: dict = {}

# A synthesised identity is md5 hex, and a channel above zero carries its index. Matched
# rather than guessed at, because a real Dahua serial is shorter, upper case and not hex:
# BC0A198PAJ779DF against 4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e.
_SYNTHESISED_UNIQUE_ID = re.compile(r"^[0-9a-f]{32}(?:_\d+)?$")


def normalize_address(address: str) -> str:
    """One device, one key.

    DahuaClient rstrips the address before keying its request limiter, but
    _acquire_connector did not, so a trailing slash could leave a single device
    holding two differently keyed pools. Everything host scoped goes through
    this.
    """
    return (address or "").strip().rstrip("/")


def _entries_for_address(hass: HomeAssistant, address: str) -> list:
    """Every config entry pointing at this host."""
    wanted = normalize_address(address)
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if normalize_address(entry.data.get(CONF_ADDRESS)) == wanted
    ]


async def _async_probe_tcp(address: str, port: int, timeout: float = 5.0) -> bool:
    """Can we open a TCP connection? No HTTP, no credentials, no retry."""
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout
        )
        return True
    except Exception:  # pylint: disable=broad-except
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pylint: disable=broad-except
                pass


async def _async_evaluate_host(hass: HomeAssistant, address: str) -> None:
    """Decide which card, if any, this host has earned.

    Some Dahua firmwares stop serving plain HTTP while HTTPS keeps working. If
    that is what happened we can say so and offer to switch. Otherwise all we
    can honestly report is that the device is not answering.
    """
    address = normalize_address(address)
    unreachable_id = ISSUE_UNREACHABLE.format(address)
    https_id = ISSUE_HTTP_DEAD_HTTPS_AVAILABLE.format(address)

    entries = _entries_for_address(hass, address)
    if not entries:
        return

    # Nothing to offer if this host is already reached over HTTPS.
    already_https = any(
        str(entry.data.get(CONF_PORT)) == "443" or entry.data.get(CONF_USE_HTTPS)
        for entry in entries
    )

    # The other place normalize_address's claim was not enforced. Its one
    # caller normalises before scheduling this, so a raw address here would
    # silently find no state, probe nothing and raise no card.
    state = _HOST_FAILURES.get(normalize_address(address))
    https_is_open = False
    if not already_https and state is not None:
        state["last_probe"] = time.time()
        https_is_open = await _async_probe_tcp(address, 443)

    minutes = 1
    if state:
        minutes = max(1, int((time.time() - state.get("since", time.time())) / 60))

    placeholders = {
        "address": address,
        "entries": str(len(entries)),
        "minutes": str(minutes),
        "port": str(entries[0].data.get(CONF_PORT, "80")),
    }

    if https_is_open:
        ir.async_delete_issue(hass, DOMAIN, unreachable_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            https_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="http_dead_https_available",
            translation_placeholders=placeholders,
            data={"address": address},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, https_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            unreachable_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
            translation_placeholders=placeholders,
            learn_more_url="https://github.com/rroller/dahua#debugging",
        )


@callback
def async_record_host_failure(hass: HomeAssistant, address: str, entry_id: str) -> int:
    """Note a failed refresh, raising a card once it stops looking like a blip.

    Returns how many consecutive failures this host has now had, which is what
    the caller backs off on. Keyed by host rather than by entry: eleven channels
    of one NVR are eleven witnesses to a single outage, not eleven outages.
    """
    address = normalize_address(address)
    state = _HOST_FAILURES.setdefault(
        address,
        {"consecutive": 0, "since": time.time(), "entry_ids": set(), "last_probe": 0},
    )
    state["consecutive"] += 1
    state["entry_ids"].add(entry_id)

    if state["consecutive"] < UNREACHABLE_AFTER_FAILURES:
        return state["consecutive"]
    # Re-evaluate on the threshold, then only as often as the probe interval
    # allows, so a wedged host does not get probed on every poll.
    if state["consecutive"] == UNREACHABLE_AFTER_FAILURES or (
        time.time() - state.get("last_probe", 0) >= HTTPS_PROBE_MIN_INTERVAL
    ):
        hass.async_create_task(_async_evaluate_host(hass, address))
    return state["consecutive"]


@callback
def async_host_is_unreachable(address: str) -> bool:
    """Whether this host has failed enough polls to be called unreachable.

    The same count and threshold the unreachable repair card is raised on, so
    what an entity says about a device and what the card says about it cannot
    disagree. One dropped poll is not an answer to this question.
    """
    state = _HOST_FAILURES.get(normalize_address(address))
    return bool(state and state["consecutive"] >= UNREACHABLE_AFTER_FAILURES)


def is_synthesised_identity(value) -> bool:
    """Whether an identity is a hash of the credentials rather than a device serial.

    A real Dahua serial is shorter, upper case and not hex: `BC0A198PAJ779DF` against
    `4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e`. The optional suffix is the channel index, which a
    unique_id carries and a bare serial does not.
    """
    return bool(value) and bool(_SYNTHESISED_UNIQUE_ID.match(str(value)))


async def async_network_identity(hass, address: str) -> dict:
    """What the device says about itself over DHDiscover, asked once per host.

    Credential free, which is the whole point: the devices that need this are the ones
    that will not answer `magicBox.cgi`, and the identity those get today is
    md5(address_rtspport_username_password). Change the password and the same physical
    camera becomes a different device (#805, and #320 from the user's side).

    UDP on the local subnet, so a camera behind a router answers nothing and keeps the
    hash. An empty answer is cached for exactly that reason: it will be empty for the next
    channel too, and re-probing would spend one timeout per entry on a recorder.
    """
    if address in _HOST_NETWORK_IDENTITY:
        return _HOST_NETWORK_IDENTITY[address]

    lock = _HOST_NETWORK_IDENTITY_LOCKS.get(address)
    if lock is None:
        lock = _HOST_NETWORK_IDENTITY_LOCKS[address] = asyncio.Lock()

    async with lock:
        # Another entry may have settled it while this one waited.
        if address in _HOST_NETWORK_IDENTITY:
            return _HOST_NETWORK_IDENTITY[address]
        # Imported here because discovery imports nothing from us but config_flow does.
        from .discovery import async_probe

        found = await async_probe(address)
        _HOST_NETWORK_IDENTITY[address] = found
        return found


async def async_device_is_zero_indexed(client, device: str):
    """Whether this device numbers its channels from zero.

    Asked once per device and shared, so the entries do not race each other
    to the same answer and do not spend six requests arriving at it.

    Returns True, False, or None when the device did not say. None is the
    point of this function: a timeout or a dropped connection is not the
    device telling us it is one-indexed, it is the device not answering, and
    treating those alike is what renumbered a working channel.

    Only an HTTP status counts as a no. That is the device answering.
    """
    if device in _HOST_CHANNEL_BASE:
        return _HOST_CHANNEL_BASE[device]

    lock = _HOST_CHANNEL_BASE_LOCKS.get(device)
    if lock is None:
        lock = _HOST_CHANNEL_BASE_LOCKS[device] = asyncio.Lock()

    async with lock:
        # Another entry may have settled it while this one waited.
        if device in _HOST_CHANNEL_BASE:
            return _HOST_CHANNEL_BASE[device]
        try:
            await client.async_probe_snapshot(0)
            _HOST_CHANNEL_BASE[device] = True
        except ClientResponseError:
            # The device answered, and the answer was no.
            _HOST_CHANNEL_BASE[device] = False
        except (ClientError, TimeoutError, asyncio.TimeoutError):
            # It did not answer. Decide nothing and leave it for the next
            # entry or the next restart, rather than caching a guess that
            # every other channel would then inherit.
            _LOGGER.debug(
                "%s did not answer the channel numbering probe; leaving it undecided",
                device,
                exc_info=True,
            )
            return None
    return _HOST_CHANNEL_BASE[device]


@callback
def async_record_host_auth_refusal(address: str, source: str = "") -> int:
    """Note that this host refused the credentials, and say how often it has.

    Held per host, because the consequence is host-wide: a Dahua box locks the
    source IP after repeated failed logins, so a recorder's channels are many
    pollers renewing one lock. Once the budget is spent everything on this host
    stops, which is the point.

    Counted per **source**, because that is the difference between a wrong
    password and a bad moment. `source` is the config entry being refused, or
    the event stream. The number returned is the highest any one source has
    reached, so:

    * twelve channels refused once each is **one**, not twelve. That is a
      recorder having a moment, and it recovers on the next poll.
    * one channel refused three times is **three**. Nothing has succeeded in
      between, and a password that is wrong is wrong every time.

    Counting requests instead of sources is what #729's sibling bug looked like
    from a user's chair: a recorder briefly refused during a burst of motion,
    twelve channels incremented one counter inside fourteen milliseconds, and
    Home Assistant demanded a new password for credentials it had never had
    trouble with. Measured on a live twelve channel NVR whose password was
    correct throughout.

    Cleared by async_record_host_success, so this only ever counts refusals
    with nothing succeeding in between.
    """
    address = normalize_address(address)
    state = _HOST_FAILURES.setdefault(
        address,
        {"consecutive": 0, "since": time.time(), "entry_ids": set(), "last_probe": 0},
    )
    by_source = state.setdefault("auth_refusals_by_source", {})
    by_source[source] = by_source.get(source, 0) + 1
    # The host's number is the worst any one source has seen, so the budget is
    # still spent once, host-wide, rather than once per channel.
    state["auth_refusals"] = max(by_source.values())
    return state["auth_refusals"]


@callback
def async_record_host_success(hass: HomeAssistant, address: str) -> None:
    """The device answered, so withdraw anything we said about it.

    Keyed by host: if any channel of an NVR replies, the box is up.
    """
    address = normalize_address(address)
    if _HOST_FAILURES.pop(address, None) is None:
        return
    ir.async_delete_issue(hass, DOMAIN, ISSUE_UNREACHABLE.format(address))
    ir.async_delete_issue(hass, DOMAIN, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE.format(address))


def _acquire_connector(address: str) -> TCPConnector:
    """Returns the shared connector for this address, creating it if needed."""
    address = normalize_address(address)
    holder = _HOST_CONNECTORS.get(address)
    if holder is None or holder[0].closed:
        # enable_cleanup_closed is deliberately not set: aiohttp ignores it on
        # every Python that Home Assistant now runs on, and warns once per
        # connector in the user's log for the trouble.
        holder = [TCPConnector(ssl=SSL_CONTEXT), 0]
        _HOST_CONNECTORS[address] = holder
    holder[1] += 1
    return holder[0]


async def _release_connector(address: str) -> None:
    """Drops a reference, closing the connector once nothing is using it."""
    address = normalize_address(address)
    holder = _HOST_CONNECTORS.get(address)
    if holder is None:
        return
    holder[1] -= 1
    if holder[1] <= 0:
        _HOST_CONNECTORS.pop(address, None)
        clear_host_cache(address)
        await holder[0].close()


# Raw IVS event codes that can become Smart Motion events later in
# DahuaDataUpdateCoordinator.translate_event_code(). The host-level filter runs
# before that translation, so these raw codes must be allowed through whenever
# one of their derived events is selected.
#
# translate_event_code also turns BackKeyLight and PhoneCallDetect into
# DoorbellPressed, and those are deliberately absent. The reason is not that
# doorbells use the VTO listener -- PhoneCallDetect is the Amcrest spelling and
# an Amcrest device does come through this stream. It is that DoorbellPressed is
# not in ALL_EVENTS, so it can never appear in a user's selection and can never
# be the derived code this map exists to rescue. Make it selectable and its two
# raw codes have to be added here; test_shared_event_stream.py asserts that.
DERIVES_INTO = {
    "CrossLineDetection": ("SmartMotionHuman", "SmartMotionVehicle"),
    "CrossRegionDetection": ("SmartMotionHuman", "SmartMotionVehicle"),
}


class DahuaHostEventStream:
    """One event stream for a host, shared by every channel configured on it.

    The device's event stream is not per channel: attaching to it returns every
    channel's events regardless of who asked. An NVR with eleven channels was
    therefore holding eleven identical streams and having ten of them throw each
    event away. This holds one, and hands each event to the channels that want
    it.
    """

    # Declared on the class, not only assigned in __init__, because several
    # tests build a stream with object.__new__ and set just the attributes they
    # are about: test_one_bad_event.py sets three, test_refused_credentials.py
    # six, and neither is about the subscription shape. Both `_async_run` and
    # `on_receive` read this, and on a bare instance the AttributeError from
    # `_async_run` landed inside its retry loop and hung the test rather than
    # failing it. A default here is one line and cannot be half-applied.
    _using_all_events = False
    # Bare instances in focused tests predate this optional sidecar. Keeping a
    # class default makes their old construction contract remain valid.
    _video_motion_poll_task: asyncio.Task | None = None

    # Whether a refusal has already pushed this stream onto codes=[All]. One
    # attempt per stream: a device that refuses the explicit list and then refuses
    # [All] as well has nothing left to try, and retrying both forever would
    # double the requests at a device that is already saying no.
    _tried_all_events = False

    # Which event codes this device's getEventIndexes endpoint has been seen to
    # report as active. Learned, because an empty answer is ambiguous -- see
    # _async_reconcile_latched_events.
    #
    # A None default rather than an empty set: a mutable class attribute would
    # be shared by every host, and this must not be. Created on first use so
    # that an instance built without __init__ -- which the tests here do -- is
    # still correct.
    _codes_the_device_reports: set | None = None

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self._hass = hass
        self._address = address
        # channel index -> coordinators listening on that channel
        self._by_channel: Dict[int, list] = {}
        self._owner = None  # whose client the stream currently borrows
        self._events: frozenset = frozenset()
        # A shared NVR stream can accumulate a much larger explicit code list
        # than any one channel used before #615. Some Dahua firmware accepts
        # codes=[All] but goes silent when given a long multi-code subscription.
        # Track whether this host currently needs the broad subscription so a
        # second channel joining (or the last extra channel leaving) restarts
        # the stream even when the union of requested event names is unchanged.
        self._using_all_events = False
        self._task: asyncio.Task | None = None
        self._video_motion_poll_task: asyncio.Task | None = None
        # Whether the last attach failed, so an outage is reported once.
        self._failing = False
        # How many times running the stream has died on contact, which is what
        # the retry delay backs off on.
        self._consecutive_failures = 0
        # Whether the device sent anything on the current attach. Reset per
        # attempt, so it describes this stream and not the one before it.
        self._received_data = False

    @property
    def coordinators(self) -> list:
        return [c for group in self._by_channel.values() for c in group]

    def _union(self) -> frozenset:
        """Every event any channel on this host asked for.

        Attaching with one channel's list would silently stop delivering the
        codes another channel selected.
        """
        union = set()
        for coordinator in self.coordinators:
            union.update(coordinator.events or [])
        return frozenset(union)

    def register(self, coordinator) -> None:
        self._by_channel.setdefault(coordinator.get_channel(), []).append(coordinator)
        if self._owner is None:
            self._owner = coordinator
        self._restart_if_needed()

    async def unregister(self, coordinator) -> bool:
        """Drop a channel. Returns True when nothing is left on this host."""
        group = self._by_channel.get(coordinator.get_channel(), [])
        if coordinator in group:
            group.remove(coordinator)
        if not group:
            self._by_channel.pop(coordinator.get_channel(), None)

        remaining = self.coordinators
        if not remaining:
            await self.async_stop()
            return True

        # The stream borrows the owner's client, and unloading an entry closes
        # its session, so hand the stream to someone still here.
        if coordinator is self._owner:
            self._owner = remaining[0]
            self._events = frozenset()  # force a restart on the new client
        self._restart_if_needed()
        return False

    def _should_try_all_events(self, exception) -> bool:
        """Whether a refused attach is worth one retry with codes=[All].

        Some firmware serves the event stream perfectly and rejects a long
        explicit code list. Measured by two reporters on two different cameras:
        #728's IPC-HFW4300S-V2 answers 200 to `codes=[VideoMotion]` and 400 to the
        nine-code list, and #832's Hero A1 answers 500 to the same nine.

        The integration already knows how to subscribe with `[All]` and filter
        locally, so the user's selection is unaffected either way. It just decided
        to do that from a heuristic -- whether sharing one stream across channels
        made the request longer than any single channel's list -- which can never
        be true for a single camera, because the union of one list is that list.
        Single cameras are exactly the devices old enough to refuse, which is why
        this reads as "everyone with one camera" rather than as a firmware quirk.

        So the device's own refusal is the signal, rather than a guess about what
        it might refuse. 404 and 501 never arrive here: those mean no CGI event
        path at all and are answered with the RPC2 poller further down.
        """
        if self._tried_all_events or self._using_all_events:
            return False
        if self._received_data:
            # This device took the list: it attached and it talked. Whatever
            # ended the socket afterwards, it was not a refusal of the request
            # shape, and broadening a subscription that demonstrably works
            # would be a change nobody asked for.
            return False
        if not self._events or "All" in self._events:
            return False
        status = getattr(exception, "status", None)
        # Credentials are a different problem with its own budget above.
        return status is not None and status != 401

    def _restart_if_needed(self) -> None:
        wanted = self._union()
        # Before #615, every channel attached with only its own event list.
        # A shared host stream can make that request strictly broader by taking
        # the union across channels. Some Dahua firmware accepts codes=[All]
        # but goes silent on that expanded explicit list. Use All only when
        # sharing actually made the subscription larger than every individual
        # channel's previous request shape; otherwise keep existing behaviour.
        use_all_events = bool(wanted) and len(wanted) > max(
            (len(c.events or ()) for c in self.coordinators), default=0
        )
        # A device that refused an explicit list once will refuse the next one, so
        # what it told us outlives a channel being added or removed. Recomputing
        # the heuristic alone would drop the stream back onto a list already known
        # to fail, and the only sign would be the events stopping again.
        use_all_events = use_all_events or self._tried_all_events
        if (
            self._task is not None
            and not self._task.done()
            and wanted == self._events
            and use_all_events == self._using_all_events
        ):
            self._sync_video_motion_poller()
            return
        self._events = wanted
        self._using_all_events = use_all_events
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if wanted and self._owner is not None:
            self._task = asyncio.create_task(self._async_run())
        self._sync_video_motion_poller()

    def _should_poll_video_motion(self) -> bool:
        return bool(self._events & {"All", "VideoMotion"}) and any(
            getattr(coordinator, "poll_video_motion", False)
            for coordinator in self.coordinators
        )

    def _sync_video_motion_poller(self) -> None:
        wanted = self._should_poll_video_motion()
        running = (
            self._video_motion_poll_task is not None
            and not self._video_motion_poll_task.done()
        )
        if wanted and not running:
            self._video_motion_poll_task = asyncio.create_task(
                self._async_poll_video_motion()
            )
        elif not wanted and running:
            self._video_motion_poll_task.cancel()
            self._video_motion_poll_task = None

    async def _async_reconcile_latched_events(self) -> None:
        """Ask the device about events still showing as on, and close the dead ones.

        Called when the stream is about to re-attach, which is the only moment a
        Stop can have been lost: the socket was down, the device sent it anyway,
        and nothing was listening.

        **Costs nothing in the ordinary case.** Nothing is usually latched, so
        there is nothing to ask about and no request is made. That matters
        because a failing stream retries often, and this must not add load to a
        device that is already struggling.

        The hard part is that `getEventIndexes` cannot be taken at face value.
        Measured on a DHI-NVR5464-16P-EI:

            code=VideoMotion          200  channels[0]=14
            code=CrossLineDetection   200  Error: No Events
            code=NotARealCodeAtAll    200  Error: No Events

        A real code with nothing active and a code the endpoint knows nothing
        about give the **same answer**. So an empty answer is not evidence that
        an event has ended, and clearing on it would switch off a live sensor
        for any code the stream reports and this endpoint does not -- the
        opposite of the bug, and worse.

        So which codes this endpoint actually reports is learned from the device
        rather than assumed, the same way `event_is_momentary` learns what a
        Pulse is. A code is trusted only once it has been seen reported as
        active at least once; until then an empty answer changes nothing.

        That learns itself in the right order. While an event is genuinely on, a
        reconcile sees it reported, records the code as one this endpoint
        covers, and clears nothing. The next time the same code is latched and
        the device does *not* report it, that is a lost Stop and it is closed.
        """
        if self._codes_the_device_reports is None:
            self._codes_the_device_reports = set()

        latched: dict = {}
        for coordinator in self.coordinators:
            events = getattr(coordinator, "latched_events", None)
            if events is None:
                continue
            for code, channel in events():
                latched.setdefault(code, set()).add(channel)
        if not latched:
            return

        for code, channels in sorted(latched.items()):
            try:
                observed = await self._owner.client.async_get_event_indexes_cgi(code)
            except Exception as ex:  # pylint: disable=broad-except
                # A device that will not answer tells us nothing, and nothing is
                # what we do about it. This runs on a reconnect, which is
                # frequently a device in trouble.
                _LOGGER.debug(
                    "Could not ask %s which %s events are active: %s",
                    self._address,
                    code,
                    ex,
                )
                continue

            if observed:
                # Proof that this endpoint reports this code. Recorded before
                # the comparison below, so the first genuine activity teaches
                # us and the lost Stop after it can be closed.
                self._codes_the_device_reports.add(code)

            if code not in self._codes_the_device_reports:
                _LOGGER.debug(
                    "%s reported no active %s events, but has never reported any, "
                    "so this is not evidence the event ended",
                    self._address,
                    code,
                )
                continue

            for channel in sorted(channels - observed):
                _LOGGER.info(
                    "%s says %s is no longer active on channel %s, so its Stop was "
                    "lost while the event stream was down. Closing it",
                    self._address,
                    code,
                    channel,
                )
                self._dispatch_events(
                    "Code={0};action=Stop;index={1}\r\n".format(code, channel).encode()
                )

    async def _async_poll_video_motion(self) -> None:
        """Synthesize VideoMotion edges from one host-wide state request."""
        active: set[int] = set()
        failing = False
        while True:
            started = time.monotonic()
            try:
                observed = await self._owner.client.async_get_event_indexes_cgi(
                    "VideoMotion"
                )
                for index in sorted(observed - active):
                    self._dispatch_events(
                        f"Code=VideoMotion;action=Start;index={index}\r\n".encode(),
                        video_motion_source="poll",
                    )
                for index in sorted(active - observed):
                    self._dispatch_events(
                        f"Code=VideoMotion;action=Stop;index={index}\r\n".encode(),
                        video_motion_source="poll",
                    )
                active = observed
                if failing:
                    _LOGGER.info(
                        "VideoMotion state polling for %s recovered", self._address
                    )
                failing = False
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # pylint: disable=broad-except
                if not failing:
                    _LOGGER.warning(
                        "VideoMotion state polling for %s failed: %s",
                        self._address,
                        ex,
                    )
                else:
                    _LOGGER.debug(
                        "VideoMotion state polling for %s still failing: %s",
                        self._address,
                        ex,
                    )
                failing = True
            await asyncio.sleep(
                max(0.0, VIDEO_MOTION_POLL_SECONDS - (time.monotonic() - started))
            )

    async def async_stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._video_motion_poll_task is not None:
            self._video_motion_poll_task.cancel()
            self._video_motion_poll_task = None
        self._by_channel.clear()
        self._owner = None
        self._events = frozenset()
        self._using_all_events = False

    async def _async_run(self) -> None:
        """Hold the stream open, recycling it the way a single channel used to."""
        while True:
            start_time = time.monotonic()
            self._received_data = False
            try:
                await asyncio.wait_for(
                    self._owner.client.stream_events(
                        self.on_receive,
                        ["All"] if self._using_all_events else sorted(self._events),
                        0,
                    ),
                    timeout=jittered(EVENT_STREAM_MAX_LIFETIME_SECONDS),
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # Two opposite things raise this. The wait_for above fires at
                # EVENT_STREAM_MAX_LIFETIME_SECONDS, which is the deliberate
                # recycle of a stream that has been working. aiohttp's sock_read
                # timeout fires when the socket has delivered nothing for
                # EVENT_STREAM_READ_TIMEOUT_SECONDS -- and ServerTimeoutError is
                # a TimeoutError, so it lands in the same place. Whether anything
                # arrived is what tells them apart.
                if self._received_data:
                    self._failing = False
                    self._consecutive_failures = 0
                    _LOGGER.debug("Recycling event stream for %s", self._address)
                else:
                    self._consecutive_failures += 1
                    if not self._failing:
                        self._failing = True
                        _LOGGER.warning(
                            "Event stream for %s attached but delivered nothing, not "
                            "even the heartbeat it asks for, so no events will arrive "
                            "from it. Some firmware stops matching anything when too "
                            "many event types are subscribed at once: selecting fewer "
                            "event types for this device is the first thing to try.",
                            self._address,
                        )
                    else:
                        _LOGGER.debug("Event stream for %s still silent", self._address)
            except Exception as ex:  # pylint: disable=broad-except
                # Credentials the device is refusing must stop being offered
                # here too, not just on the poll. This stream backs off to at
                # most EVENT_STREAM_MAX_RETRY_SECONDS, which is well inside
                # the half hour a Dahua box locks a source IP for -- so on its
                # own it would keep renewing the lock that #729 is about, and
                # the reauth the coordinator asked for would still be refused.
                #
                # The count is shared with the polls and cleared by any
                # success on this host, so a stream cannot reach the budget
                # while anything here is still authenticating.
                if isinstance(ex, ClientResponseError) and ex.status == 401:
                    refusals = async_record_host_auth_refusal(
                        self._address, "event stream"
                    )
                    if refusals >= MAX_AUTH_REFUSALS:
                        _LOGGER.warning(
                            "Event stream for %s stopped: the device refused these credentials %d times. It will start again once the credentials are re-entered",
                            self._address,
                            refusals,
                        )
                        return

                if self._should_try_all_events(ex):
                    # Straight back round rather than through the backoff: this is
                    # not a device in trouble, it is one that wants the request put
                    # a different way, and it has told us so.
                    self._tried_all_events = True
                    self._using_all_events = True
                    _LOGGER.warning(
                        "Event stream for %s refused a list of %d event codes with HTTP %s. Some firmware will not serve a long explicit list; subscribing to all events instead and filtering locally, which does not change which events reach Home Assistant",
                        self._address,
                        len(self._events),
                        getattr(ex, "status", "?"),
                    )
                    continue

                # Say it once per outage, not once per retry. Silence was the
                # old behaviour and it is why these failures went unreported;
                # a warning every sixty seconds forever is the other extreme.
                if self._received_data:
                    # It attached and it talked; the socket ending is not this
                    # device refusing contact, so the instant-death counter must
                    # not climb on it.
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                if not self._failing:
                    self._failing = True
                    _LOGGER.warning(
                        "Event stream for %s ended unexpectedly: %s", self._address, ex
                    )
                else:
                    _LOGGER.debug(
                        "Event stream for %s still failing: %s", self._address, ex
                    )
            else:
                self._failing = False
                self._consecutive_failures = 0

            # Before going back round. A Stop that arrived while the socket was
            # down is gone, and the sensor it belonged to is still on.
            await self._async_reconcile_latched_events()

            retry_in = event_stream_retry_delay(
                stream_lifetime(time.monotonic() - start_time, self._received_data),
                self._consecutive_failures,
                self._received_data,
            )
            if retry_in:
                _LOGGER.debug(
                    "Reconnecting to event stream for %s in %.0fs",
                    self._address,
                    retry_in,
                )
                await asyncio.sleep(retry_in)
            else:
                _LOGGER.debug("Reconnecting to event stream for %s", self._address)

    def on_receive(self, data_bytes: bytes, _channel: int) -> None:
        """Parse once, then hand each event only to the channels that want it."""
        # Before the parse, deliberately: a heartbeat carries no event but is
        # still the device talking, and that is what the retry delay needs to
        # know. A camera sitting quietly with nothing to report is not a camera
        # refusing to attach.
        self._received_data = True

        self._dispatch_events(data_bytes, video_motion_source="stream")

    def _dispatch_events(
        self, data_bytes: bytes, video_motion_source: str | None = None
    ) -> None:
        """Parse and dispatch events without changing stream health state."""

        events = parse_event(data_bytes.decode("utf-8", errors="ignore"))
        if not events:
            return

        for event in events:
            # A multi-channel host may subscribe with codes=[All] to avoid
            # firmware limits on long explicit code lists. Preserve the user's
            # configured selection locally so that broadening the wire-level
            # subscription does not broaden Home Assistant events or entities.
            #
            # Only when the subscription was actually broadened. Otherwise the
            # device is already filtering to the requested codes and this would
            # be a second, redundant filter on the path every existing host
            # takes -- so a single camera and any host whose union did not grow
            # run exactly the code they ran before, rather than code that merely
            # ought to agree with it. A user who selected "All" themselves is
            # asking for everything and is not filtered either.
            #
            # A bare instance defaults to False from the class attribute, so an
            # incompletely built stream takes the old path rather than raising.
            if self._using_all_events and "All" not in self._events:
                code = event.get("Code")
                derives = set(DERIVES_INTO.get(code, ())) & self._events
                if code not in self._events and not derives:
                    continue

            index = 0
            if "index" in event:
                try:
                    index = int(event["index"])
                except ValueError:
                    index = 0

            # AlarmLocal numbers its `index` from the physical alarm input
            # terminal, not the video channel -- so on a host with a single
            # configured channel it can legitimately be nonzero (e.g. index=1)
            # while that channel is 0. There is no channel to disambiguate
            # when only one is configured, so the index there means something
            # else and must not be used to drop the event. See #231.
            #
            # Guarded on the number of *channels*, not the number of
            # coordinators: two config entries can share one channel (see
            # test_two_entries_on_one_channel_both_get_it), and both must
            # still get the event. Every other event code, and every host
            # with more than one channel configured, keeps the existing
            # per-index filtering below untouched: a channel nobody
            # configured must stay silent.
            if event.get("Code") == "AlarmLocal" and len(self._by_channel) == 1:
                for coordinator in next(iter(self._by_channel.values())):
                    try:
                        coordinator.handle_event(dict(event))
                    except Exception:  # pylint: disable=broad-except
                        # Same reach as the guard below (#706): this stream is
                        # shared by every channel on the host, and
                        # stream_events wraps its call to on_receive in
                        # try/finally with no handler, so an unguarded
                        # exception here would take every camera on the
                        # device down until the retry reconnects, not just
                        # drop this one event.
                        #
                        # The index is deliberately not logged as a channel:
                        # for AlarmLocal it is the alarm input, which is the
                        # whole reason this branch exists.
                        _LOGGER.warning(
                            "Unhandled error while handling a %s event from %s; "
                            "the event is dropped and the stream continues",
                            event.get("Code", "?"),
                            self._address,
                            exc_info=True,
                        )
                continue

            # A channel nobody has configured stays silent, exactly as it did
            # when every coordinator discarded it.
            for coordinator in self._by_channel.get(index, ()):
                if event.get("Code") == "VideoMotion":
                    polling = getattr(coordinator, "poll_video_motion", False)
                    # Polling is authoritative only for coordinators that opted
                    # into it. This prevents a partial pushed Start with no Stop
                    # from leaving their motion sensor on forever, while other
                    # channels retain the original event-stream behaviour.
                    if (video_motion_source == "stream" and polling) or (
                        video_motion_source == "poll" and not polling
                    ):
                        continue
                try:
                    coordinator.handle_event(dict(event))
                except Exception:  # pylint: disable=broad-except
                    # This stream is shared by every channel on the host, and
                    # stream_events wraps its call to on_receive in try/finally
                    # with no handler -- so an exception here does not just lose
                    # this event, it leaves the read loop and takes events for
                    # every camera on the device down until the retry
                    # reconnects. One malformed payload did exactly that (#475).
                    #
                    # Per coordinator rather than per event, so a channel whose
                    # handler fails does not rob the other channels of an event
                    # they could have handled.
                    _LOGGER.warning(
                        "Unhandled error while handling a %s event from %s on channel %s; "
                        "the event is dropped and the stream continues",
                        event.get("Code", "?"),
                        self._address,
                        index,
                        exc_info=True,
                    )


# address -> DahuaHostEventStream
_HOST_STREAMS: Dict[str, DahuaHostEventStream] = {}


def _host_stream(hass: HomeAssistant, address: str) -> DahuaHostEventStream:
    address = normalize_address(address)
    stream = _HOST_STREAMS.get(address)
    if stream is None:
        stream = _HOST_STREAMS[address] = DahuaHostEventStream(hass, address)
    return stream


async def _release_host_stream(coordinator) -> None:
    address = normalize_address(coordinator.get_address())
    stream = _HOST_STREAMS.get(address)
    if stream is None:
        return
    if await stream.unregister(coordinator):
        _HOST_STREAMS.pop(address, None)
