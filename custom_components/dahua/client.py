"""Dahua API Client."""
import logging
import re
import socket
from copy import deepcopy
from contextlib import suppress
import asyncio
import time
import aiohttp

from .digest import DigestAuth
from .rpc2 import DahuaRpc2Client, Rpc2MethodRefused
from hashlib import md5
from urllib.parse import quote

_LOGGER: logging.Logger = logging.getLogger(__package__)

TIMEOUT_SECONDS = 20

# The event stream asks the device to heartbeat at this interval, so a socket
# that has delivered nothing for a comfortable multiple of it has stalled.
EVENT_STREAM_HEARTBEAT_SECONDS = 5
EVENT_STREAM_READ_TIMEOUT_SECONDS = 60

# One NVR carries a config entry per channel, and every entry sets itself up at
# the same moment. Dahua's HTTP server is small: a dozen simultaneous CGI calls
# can wedge it, and because the entries then fail together they retry together,
# so the burst repeats and the device never recovers. Cap concurrent requests
# per host, shared across every client for that address.
MAX_CONCURRENT_REQUESTS_PER_HOST = 2
_HOST_LIMITS: dict = {}


def _device_key(address: str, port) -> str:
    """One device.

    Two Dahua boxes can sit behind one IP on different ports -- a bridge
    forwarding 80/554 to one and 81/555 to another. Everything that identifies a
    device, rather than the network path to it, has to say which one.

    The port is normalised because a config entry stores it as a string while
    callers pass an int, and "192.168.0.175:80" must not be a different device
    from "192.168.0.175:80".
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        pass
    return "{0}:{1}".format(address, port)


def _host_limiter(address: str) -> asyncio.Semaphore:
    """Returns the semaphore shared by every client talking to this address."""
    limiter = _HOST_LIMITS.get(address)
    if limiter is None:
        limiter = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS_PER_HOST)
        _HOST_LIMITS[address] = limiter
    return limiter


# The digest challenge a device hands out is not per connection: any request
# with the right credentials can use it. Eleven config entries for one NVR were
# each taking a 401 to get their own copy of the same challenge, at exactly the
# moment the device is least able to spare the round trips.
#
# Sharing it also fixes the nonce count. Digest asks the client to send a
# strictly increasing nc for a given nonce; eleven clients each counting from
# one against the same nonce is what replay protection exists to reject.
_HOST_DIGEST_STATE: dict = {}


# One RPC2 login, shared by every config entry for a host, because a Dahua box
# keeps a finite session table and writes a line to its own log for every login.
# That log line is the cost this transport exists to remove, so eleven channels
# of one NVR opening eleven sessions would hand most of the saving straight
# back -- the same arithmetic that made the digest challenge worth sharing.
#
# Keyed by user as well as host: a session id is obtained with the password and
# scoped to that user's rights, so it is even less shareable than a challenge.
_HOST_RPC2: dict = {}

# Whether RPC2 has already proven it cannot serve a host. A per-client verdict
# meant eleven channels each rediscovering it, which is eleven failed logins
# against a device that has just said it cannot do this.
_HOST_RPC2_UNAVAILABLE: set = set()

# (rpc2 key, config table) pairs the device answered but declined. Kept apart
# from _HOST_RPC2_UNAVAILABLE on purpose: one table it will not serve says
# nothing about the rest, and writing the host off for it costs a login on
# every later read.
_RPC2_TABLE_UNAVAILABLE: set = set()

# The device states its own keepalive interval in the login reply. Ask slightly
# inside it, the way the VTO keepalive already does.
RPC2_KEEPALIVE_MARGIN_SECONDS = 5
RPC2_KEEPALIVE_FALLBACK_SECONDS = 60


class _SharedRpc2Session:
    """One login, and the keepalive holding it open, for one host and user.

    The login is registered as a task before it is awaited, so eleven channels
    waking together share the one in flight rather than each starting another.
    That is the same move _SharedRead makes, for the same reason.
    """

    __slots__ = ("task", "client", "session", "refs", "keepalive")

    def __init__(self, session: aiohttp.ClientSession, client, task) -> None:
        self.session = session
        self.client = client
        self.task = task
        self.refs = 0
        self.keepalive = None


async def _rpc2_keepalive(holder: "_SharedRpc2Session", interval: float) -> None:
    """Hold one login open, so a quiet poll interval does not cost a new one.

    Measured on a DHI-NVR5464-16P-EI: the session survives 90 seconds idle and
    is gone by 150. A device polled every 150-180s -- which is what users are
    told to set to quieten their NVR log -- would therefore log in on every
    poll, which is most of the saving gone.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await holder.client.request(
                method="global.keepAlive",
                params={"timeout": interval, "active": False},
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            # Drop the login rather than the holder: the next read logs in
            # again, and the entries still hold their references.
            holder.task = None
            _LOGGER.debug("RPC2 keepalive failed, will log in again on the next read")
            return


def keepalive_needs_starting(task) -> bool:
    """Whether this session still needs a keepalive started for it.

    A finished keepalive is not a running one. The guard here used to be
    `task is None`, which is true exactly once -- so when the keepalive loop
    returned, and it returns on any failed keepalive by design, the slot kept a
    completed task forever and no later read ever started another.

    That failure path is meant to be recoverable: it drops the login so the next
    read logs in again. Without this, the session comes back but the keepalive
    never does, and every subsequent idle gap longer than the device's timeout
    costs a fresh login -- quietly, because nothing is broken enough to log.
    """
    return task is None or task.done()


async def _release_rpc2(key) -> None:
    """Give back one entry's share of a host's session.

    At zero: stop the keepalive before logging out, so it cannot fire one more
    request against a session that is being closed. Cancellation is awaited
    here, unlike elsewhere in this integration, because what follows it depends
    on the task having actually stopped.
    """
    holder = _HOST_RPC2.get(key)
    if holder is None:
        return
    holder.refs -= 1
    if holder.refs > 0:
        return
    del _HOST_RPC2[key]

    if holder.keepalive is not None:
        holder.keepalive.cancel()
        with suppress(asyncio.CancelledError):
            await holder.keepalive
        holder.keepalive = None
    try:
        if holder.task is not None:
            await holder.client.logout()
    except Exception:  # pylint: disable=broad-except
        # A device that will not take the logout will time the session out on
        # its own. Losing the socket matters more than losing the courtesy.
        _LOGGER.debug("RPC2 logout failed for %s", key[0], exc_info=True)
    if not holder.session.closed:
        await holder.session.close()


def _digest_state(device: str, username: str) -> dict:
    """The digest state shared by every client for this device and user.

    Keyed by user as well as device because the response digest is built from
    the credentials, and entries for one NVR need not share them. Keyed by
    device rather than address because a nonce is issued by one box and means
    nothing to another that happens to answer on the same IP.
    """
    key = (device, username)
    state = _HOST_DIGEST_STATE.get(key)
    if state is None:
        state = _HOST_DIGEST_STATE[key] = {}
    return state


def _overlay_text(*parts: str) -> str:
    """Join the lines of a title or overlay, each one safe to put in a URL.

    The pipe is Dahua's line separator and has to arrive as a pipe, so the parts
    are escaped and the separator is not.

    Escaping them matters more than it looks. The text went into the URL raw,
    and yarl then encodes the query on the way out -- a space becomes "+", which
    this firmware stores literally. Measured on a DHI-NVR5464-16P-EI:

        ChannelTitle[14].Name=Channel+1    -> stored as "Channel+1"
        ChannelTitle[14].Name=Channel%201  -> stored as "Channel 1"

    So every camera name with a space in it was being written back wrong, and
    the device answers OK either way. A "#" is quieter still: yarl reads the
    rest of the value as a URL fragment and the request line never carries it,
    so "Gate#2" arrives as "Gate".
    """
    return "|".join(quote(part, safe="") for part in parts if part)


# Most of what a coordinator reads every poll carries no channel argument:
# MotionDetect, DisableLinkage, DisableEventNotify, SmartMotionDetect,
# Lighting_V2, VideoInMode, coaxialControlIO and ptz.cgi are all host-wide. An
# NVR with eleven config entries therefore asks eleven times for byte-identical
# answers, every cycle. Share them.
#
# The key is the URL, so nothing has to be classified by hand: a read that does
# carry a channel has a different URL per channel and is never shared. Only
# reads are cached; anything else drops the host's entries, because a write is
# how these values change.

# How long a shared read answers for. Two lifetimes, because the reads fall
# into two kinds.
#
# Status reads change on their own: where a PTZ camera is pointing, whether the
# siren is sounding, which day/night profile the device has switched itself to.
# Those have to be polled, so they get a lifetime short enough to only cover
# one poll's fan-out.
#
# Config reads change only when something writes them -- and a write drops this
# host's entries, so a change made through Home Assistant is reflected at once.
# Re-asking the device every poll buys nothing except load. On hardware
# measured for this, each of those calls costs two TCP connections, two HTTP
# requests and one refused login in the device's own log, because the device
# answers Connection: close and reissues its digest nonce every time. A camera
# with no dashboard open was spending most of its request budget re-reading
# settings nobody had touched.
#
# The cost is that a change made in the Dahua app or web UI, rather than
# through Home Assistant, can take up to this long to appear.
HOST_CACHE_TTL_SECONDS = 5
CONFIG_CACHE_TTL_SECONDS = 300

# getConfig is a settings read. getStatus and the rest report live state.
CONFIG_READ_MARKER = "action=getConfig"
_HOST_CACHE: dict = {}


def _cache_lifetime(url: str) -> int:
    """How long this URL's answer stays good for."""
    return CONFIG_CACHE_TTL_SECONDS if CONFIG_READ_MARKER in url else HOST_CACHE_TTL_SECONDS


# CGI reads are "action=getSomething". Everything else -- setConfig, reboot,
# ptz control, door open -- is a write. Unrecognised is treated as a write,
# which is the safe way round.
READ_ACTION_PREFIX = "action=get"


def _is_read(url: str) -> bool:
    return READ_ACTION_PREFIX in url


def clear_host_cache(scope: str) -> None:
    """Drop the shared reads for one device, or for every device at an address.

    `scope` is either a device key ("10.0.0.1:80") or a bare address
    ("10.0.0.1"), which matches every device behind it. A write is only a reason
    to distrust what *that* device said, so the write path passes its own key;
    the connector teardown has only the address, and dropping everything behind
    it is right there because the connector is going too.
    """
    stale = [k for k in _HOST_CACHE
             if k[0] == scope or k[0].startswith(scope + ":")]
    for key in stale:
        del _HOST_CACHE[key]


class _SharedRead:
    """One read of one URL, shared by every entry on the host that wants it.

    While the request is in flight, later callers wait on the same task rather
    than issuing their own. Once it lands, it answers again for the TTL.
    """

    __slots__ = ("task", "expires_at")

    def __init__(self, task: asyncio.Task) -> None:
        self.task = task
        self.expires_at = None  # stamped when the request lands

    def is_usable(self, now: float) -> bool:
        if not self.task.done():
            return True
        return self.expires_at is not None and now < self.expires_at


class EventStreamClosed(Exception):
    """The device ended the event stream.

    A long poll that returns is a failure, not a result: the connection is
    meant to stay open until we recycle it. A device that refuses
    action=attach by answering 200 and closing would otherwise leave no trace
    anywhere.
    """


def _pop_complete_multipart_part(buffer: bytes, boundary: bytes):
    """Return one complete multipart part and the bytes left after it.

    Dahua includes Content-Length on event-stream parts. Once that many payload
    bytes are buffered, the part is complete and can be delivered immediately;
    waiting for the next boundary adds up to one heartbeat interval of latency.

    Devices that omit Content-Length keep the previous next-boundary fallback.
    """
    start = buffer.find(boundary)
    if start == -1:
        return None, buffer
    if start:
        buffer = buffer[start:]

    header_end = buffer.find(b"\r\n\r\n", len(boundary))
    separator_len = 4
    if header_end == -1:
        header_end = buffer.find(b"\n\n", len(boundary))
        separator_len = 2
    if header_end == -1:
        return None, buffer

    payload_start = header_end + separator_len
    content_length = None
    for line in buffer[len(boundary):header_end].splitlines():
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"content-length":
            try:
                content_length = int(value.strip())
            except ValueError:
                content_length = None
            if content_length is not None and content_length < 0:
                content_length = None
            break

    if content_length is not None:
        part_end = payload_start + content_length
        next_boundary = buffer.find(boundary, payload_start)

        # If the next part starts before the declared payload end, the length
        # was wrong or the payload was truncated. The multipart framing is
        # stronger evidence than a length that would consume into the next part.
        if next_boundary != -1 and next_boundary < part_end:
            return buffer[:next_boundary], buffer[next_boundary:]

        if len(buffer) < part_end:
            return None, buffer

        part = buffer[:part_end]
        remainder = buffer[part_end:]
        # Content-Length excludes the CRLF framing before the next boundary.
        if remainder.startswith(b"\r\n"):
            remainder = remainder[2:]
        elif remainder.startswith(b"\n"):
            remainder = remainder[1:]
        return part, remainder

    next_boundary = buffer.find(boundary, payload_start)
    if next_boundary == -1:
        return None, buffer
    return buffer[:next_boundary], buffer[next_boundary:]


_CONFIG_READ = re.compile(r"configManager\.cgi\?action=getConfig&name=(.+)$")


def flatten_rpc2_config(name: str, node, prefix: str = None) -> dict:
    """Turn RPC2's nested JSON into the flat keys the rest of the code reads.

    Every accessor in this integration reads CGI's shape --
    "table.Lighting_V2[0][1][0].Mode" -- so an RPC2 answer has to arrive
    looking identical or nothing downstream works. Verified against a live
    NVR across seven configs and 4,647 keys: same keys, same values.

    Nulls are dropped. CGI omits an absent entry entirely while RPC2 sends it
    as JSON null, and keeping those would fabricate rows -- a
    SmartMotionDetect row existing for every channel is exactly the phantom
    the capability check in #635 stopped producing.
    """
    out = {}
    if prefix is None:
        prefix = "table." + name
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(flatten_rpc2_config(name, value, "%s.%s" % (prefix, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out.update(flatten_rpc2_config(name, value, "%s[%d]" % (prefix, index)))
    elif node is not None:
        if isinstance(node, bool):
            node = "true" if node else "false"
        out[prefix] = str(node)
    return out


SECURITY_LIGHT_TYPE = 1

# VideoInOptions[channel].DayNightColor, the portable spelling of the Day/Night
# setting. Verified present on a DHI-NVR5464-16P-EI, a VTO, and the
# DHI-VTO2311R-WP on #687, none of which carry VideoInDayNight at all.
DAY_NIGHT_COLOR = {"Color": 0, "Brightness": 1, "BlackWhite": 2}
SIREN_TYPE = 2


# Some Dahua devices append a short proprietary block after the JPEG's end-of-image
# marker. It is not part of the image and strict decoders are entitled to refuse it.
DAHUA_TRAILER_SIGNATURE = b"dhav"
JPEG_SOI = bytes((0xFF, 0xD8))
JPEG_EOI = bytes((0xFF, 0xD9))


def strip_dahua_snapshot_trailer(data: bytes) -> bytes:
    """Drop a trailing `dhav` block that some devices add after the JPEG.

    Measured on a VTO doorbell: every snapshot ends eight bytes past the
    end-of-image marker, with `dhav` and four varying bytes. The NVR channels on
    the same network end exactly at the marker, so this is per device rather than
    per request, and it is stable across fetches.

    Lenient decoders skip it, which is why this goes unnoticed. Strict ones do
    not, and a JPEG with bytes after EOI is genuinely malformed.

    Deliberately narrow: the data must look like a JPEG, must not already end at
    the marker, and what follows the marker must carry the signature. Anything
    else is returned untouched, because truncating an image on a guess is worse
    than passing on a trailer.
    """
    if not data.startswith(JPEG_SOI) or data.endswith(JPEG_EOI):
        return data
    end = data.rfind(JPEG_EOI)
    if end == -1:
        return data
    if not data[end + 2:].startswith(DAHUA_TRAILER_SIGNATURE):
        return data
    return data[:end + 2]


JPEG_SOS = 0xDA
JPEG_COM = 0xFE
JPEG_APP0 = 0xE0
JPEG_APP15 = 0xEF

# Markers that carry a two-byte length and can appear in a JPEG header. Used to
# find where a segment really ended when it lied about where that was.
JPEG_LENGTH_BEARING_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
     0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF, 0xFE}
    | set(range(JPEG_APP0, JPEG_APP15 + 1))
)


def _jpeg_header_is_consistent(data: bytes) -> bool:
    """Whether every header segment's declared length lands on the next marker.

    Walks from SOI to SOS only. Entropy-coded scan data is not marker-structured
    and is never examined.
    """
    i = 2
    while i + 3 < len(data):
        if data[i] != 0xFF:
            return False
        marker = data[i + 1]
        if marker == 0xFF:                      # fill byte
            i += 1
            continue
        if marker == JPEG_SOS:
            return True
        if marker not in JPEG_LENGTH_BEARING_MARKERS:
            return False
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            return False
        i += 2 + length
    return False


def _next_length_bearing_marker(data: bytes, start: int) -> int:
    """Offset of the next plausible header marker at or after start, or -1."""
    i = start
    while i + 1 < len(data):
        if data[i] == 0xFF and data[i + 1] in JPEG_LENGTH_BEARING_MARKERS:
            return i
        i += 1
    return -1


def repair_dahua_snapshot_header(data: bytes) -> bytes:
    """Drop an ignorable header segment that declares the wrong length.

    Measured on a DH-IPC-HFW2449TL-S-PRO snapshot supplied on #575. The file
    starts and ends correctly and carries no trailer, but the first COM segment
    declares 4094 bytes and actually occupies 4702 -- 608 short. A decoder that
    trusts the length lands mid-padding on a 0x00 where a marker should be and
    loses the rest of the file:

        009e  COM  declares 4094  ->  109e  0x00, not 0xFF
             the next real marker is at 12fe

    The reporter's own tests isolate it exactly: removing that segment makes the
    file acceptable, while zeroing its payload and leaving the length alone does
    not. So the defect is the length, not the contents.

    Only COM and APPn segments are dropped. Both are ignorable by definition --
    a comment and application metadata -- so losing one costs nothing, whereas a
    quantisation table or a frame header is the image. The result is re-walked
    and the original returned unless the repair actually produced a consistent
    header, so a guess that does not pay off changes nothing.
    """
    if not data.startswith(JPEG_SOI) or _jpeg_header_is_consistent(data):
        return data

    i = 2
    while i + 3 < len(data):
        if data[i] != 0xFF:
            return data
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == JPEG_SOS or marker not in JPEG_LENGTH_BEARING_MARKERS:
            return data
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            return data
        end = i + 2 + length
        if end + 1 < len(data) and data[end] == 0xFF:
            i = end
            continue

        # This segment does not end where it says it does.
        if marker != JPEG_COM and not (JPEG_APP0 <= marker <= JPEG_APP15):
            return data
        resume = _next_length_bearing_marker(data, i + 4)
        if resume == -1:
            return data
        repaired = data[:i] + data[resume:]
        return repaired if _jpeg_header_is_consistent(repaired) else data

    return data


# A device that answers and refuses has told us something about itself. One that
# never answers has told us about this moment.
TRANSIENT_RPC2_FAILURES = (TimeoutError, aiohttp.ClientConnectionError)


def rpc2_failure_is_permanent(exception: BaseException) -> bool:
    """Whether an RPC2 failure should rule the transport out for this host.

    The verdict is permanent for the life of the process, so it has to mean
    "this device does not speak RPC2" rather than "this read did not come back".
    Every exception used to count, which made a single timeout during one busy
    moment switch a working device back to a login per call until Home Assistant
    was restarted -- silently, since the fallback works.

    Observed on a DHI-NVR5464-16P-EI: nine reads timed out within the same
    second, two hours after startup, on a host that had been serving RPC2
    perfectly well and went on being able to.
    """
    return not isinstance(exception, TRANSIENT_RPC2_FAILURES)


def lighting_scheme_illuminator_tables(
        lighting_scheme: list, lighting_v2: list, channel: int,
        profile_mode: int, light_index: int, enabled: bool,
        brightness: int, restore_mode: str | None = None) -> tuple[list, list]:
    """Build the two complete tables used by dual-light Web5 cameras.

    On IPC-Color4M-TZ, selecting WhiteMode without configuring the white
    emitter does not light it, and configuring the emitter without selecting
    WhiteMode does not light it either. Turning it off restores a captured mode.
    Without one, do not guess: only stop a still-selected white emitter.
    """
    scheme = deepcopy(lighting_scheme)
    lighting = deepcopy(lighting_v2)
    try:
        scheme_row = scheme[channel][profile_mode]
        light_row = lighting[channel][profile_mode][light_index]
    except (IndexError, KeyError, TypeError):
        raise ValueError("Dahua lighting tables do not contain the selected light") from None
    if not isinstance(scheme_row, dict) or not isinstance(light_row, dict):
        raise ValueError("Dahua lighting tables contain malformed rows")
    current_mode = scheme_row.get("LightingMode")
    if not isinstance(current_mode, str) or not current_mode:
        raise ValueError("Dahua lighting scheme is missing LightingMode")
    if light_row.get("LightType") != "WhiteLight":
        raise ValueError("Selected Dahua light is not the white emitter")

    if enabled:
        scheme_row["LightingMode"] = "WhiteMode"
        light_row["Mode"] = "Manual"
        light_row["PercentOfMaxBrightness"] = brightness
        for bank_name in ("NearLight", "MiddleLight", "FarLight"):
            bank = light_row.get(bank_name, [])
            if not isinstance(bank, list):
                raise ValueError("Dahua white-emitter bank is malformed")
            for emitter in bank:
                if not isinstance(emitter, dict):
                    raise ValueError("Dahua white-emitter entry is malformed")
                emitter["Light"] = brightness
    elif restore_mode is not None:
        scheme_row["LightingMode"] = restore_mode
    elif current_mode == "WhiteMode":
        light_row["Mode"] = "Off"
    return scheme, lighting


# 1 main stream + 2 sub-streams, which is what the coordinator starts with and
# what the comment in get_max_extra_streams has always described.
DEFAULT_EXTRA_STREAMS = 2


def parse_extra_streams(value) -> int:
    """How many sub-streams the device says it has, as a usable number.

    Measured, because the shape of this answer decides how many camera
    entities get created:

        DHI-NVR5464-16P-EI (G61_NVR16PRO16P-I3)   table.MaxExtraStream=2
        VTO2000A doorbell                         table.MaxExtraStream=1

    A doorbell really does answer 1, and #237 is an AD410 owner whose log fills
    with `Error opening stream ... subtype=2` for a sub-stream that does not
    exist. So over-guessing this is not harmless -- it is a camera entity that
    404s on every attempt, for as long as the entry exists.

    Anything unreadable falls back to the common case rather than raising. The
    caller is inside the one-time init block, whose handler turns any exception
    into UpdateFailed, so a device answering a non-numeric value here would
    never finish initialising and would retry for as long as it kept saying it.
    """
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_EXTRA_STREAMS
    # A negative count is not a smaller camera, it is a device talking nonsense.
    if count < 0:
        return DEFAULT_EXTRA_STREAMS
    return count


def _is_login_refused(exception: aiohttp.ClientResponseError) -> bool:
    """True when the device refused the credentials, not the endpoint.

    The identity calls below fall back to an id built from the credentials when
    magicBox.cgi answers with an error, which is how cameras that do not
    implement it at all are still supported. That fallback is right for a 404 or
    a 501 -- the device has no such endpoint -- and wrong for a 401, where the
    device understood the request perfectly and rejected the login. Synthesising
    an identity from a password the camera has just refused is how a wrong
    password came to produce a working-looking camera that never polls.

    403 deliberately keeps the fallback. It means the login was accepted and
    this account is not allowed that endpoint, which a restricted Dahua user
    really can hit, and their credentials are not wrong.
    """
    return exception.status == 401


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
            session: aiohttp.ClientSession,
            use_https: bool = None,
            use_rpc2: bool = False
    ) -> None:
        self._username = username
        self._password = password
        # Strip trailing slashes from address to prevent malformed URLs like http://host/:80
        self._address = address.rstrip('/')
        self._port = port
        # Which device this is, as opposed to which address answers for it.
        self._device = _device_key(self._address, port)
        # One digest challenge shared by every request to this device, so a call
        # doesn't have to take a 401 before it can authenticate -- and neither
        # does the next config entry for the same NVR.
        self._digest_state = _digest_state(self._device, username)
        self._session = session
        self._rtsp_port = rtsp_port

        # Callers that do not say keep the old behaviour: HTTPS only on 443.
        if use_https is None:
            use_https = int(port) == 443
        self._use_https = use_https
        # Keyed by address so every entry for one NVR shares a single budget.
        self._host_limit = _host_limiter(self._address)
        self._rpc2_session_instance = None
        # Prototype (#636): route config reads over one RPC2 session instead of
        # a fresh digest handshake per call. Off unless the entry asks for it.
        self._use_rpc2 = use_rpc2
        # Whether this client holds a share of the host's session, and whether
        # it has given it back. Two flags rather than one because async_stop is
        # reachable three ways and a second release would close the session out
        # from under the other entries.
        self._rpc2_acquired = False
        self._rpc2_released = False
        # Preserve the camera's policy while the illuminator temporarily owns
        # a channel/profile. The entry is removed only after a successful off.
        self._lighting_scheme_restore_modes: dict[tuple[int, int], str] = {}
        # True once this device has failed to report a serial number and we have had
        # to derive its identity from the connection details instead. That derivation
        # includes the password, so the identity changes if the password does.
        self.identity_derived_from_credentials = False
        protocol = "https" if use_https else "http"
        self._base = "{0}://{1}:{2}".format(protocol, self._address, port)

    def get_rtsp_stream_url(self, channel: int, subtype: int) -> str:
        """
        Returns the RTSP url for the supplied subtype (subtype is 0=Main stream, 1=Sub stream)
        """
        url = "rtsp://{0}:{1}@{2}:{3}/cam/realmonitor?channel={4}&subtype={5}".format(
            quote(self._username, safe=''),
            quote(self._password, safe=''),
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
        return repair_dahua_snapshot_header(
            strip_dahua_snapshot_trailer(await self.get_bytes(url)))

    async def async_get_system_info(self, strict_auth: bool = False) -> dict:
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
            # strict_auth only for the config flow, which is deciding whether a
            # password is right. The coordinator shares this method, and there a
            # 401 is not proof of a wrong password: eight channels of one NVR
            # share a digest challenge, and a nonce that races between them is
            # refused exactly like a bad credential. Raising here made every
            # channel start a reauth flow at startup (#714), where before the
            # identity simply fell back and the entry carried on.
            if strict_auth and _is_login_refused(e):
                raise
            self.identity_derived_from_credentials = True
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
            if _is_login_refused(e):
                raise
            self.identity_derived_from_credentials = True
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
        except aiohttp.ClientResponseError:
            # No such endpoint on this device. Assume the standard 2, which is
            # what this comment has always said -- the code returned 3.
            return DEFAULT_EXTRA_STREAMS
        return parse_extra_streams(result.get("table.MaxExtraStream"))

    async def async_get_alarm_output_slots(self) -> dict:
        """Return the number of physical alarm-output slots reported by the device."""
        return await self.get("/cgi-bin/alarm.cgi?action=getOutSlots")

    async def async_get_alarm_output_state(self) -> dict:
        """Return the physical alarm-output state.

        The response is deliberately left unmodified. Single-output devices
        return ``result=0`` or ``result=1``; the encoding for devices with
        multiple outputs has not yet been verified.
        """
        data = await self.get("/cgi-bin/alarm.cgi?action=getOutState")
        return {"status.AlarmOut[0]": data.get("result")}

    async def async_set_alarm_output_state(self, output: int, enabled: bool) -> dict:
        """Force one alarm output on or off.

        AlarmOut.Mode is a three-state control mode, not a boolean: 0 is Auto,
        1 is Manual/Force ON, and 2 is Close/Force OFF.
        """
        mode = 1 if enabled else 2
        url = (
            "/cgi-bin/configManager.cgi?action=setConfig&"
            "AlarmOut[{output}].Mode={mode}"
        ).format(output=output, mode=mode)
        return await self.get(url)

    async def async_get_coaxial_control_io_status(self, channel: int = 1) -> dict:
        """
        async_get_coaxial_control_io_status returns the the current state of the speaker and white light.
        Note that the "white light" here seems to also work for cameras that have the red/blue flashing alarm light
        like the IPC-HDW3849HP-AS-PV.

        Example response:

        status.status.Speaker=Off
        status.status.WhiteLight=Off
        """
        url = "/cgi-bin/coaxialControlIO.cgi?action=getStatus&channel={channel}".format(channel=channel)
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
            self.identity_derived_from_credentials = True
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

    async def async_enabled_smart_motion_detection(self, channel: int, enabled: bool):
        """ Enables or disabled smart motion detection for Dahua devices (doesn't work for Amcrest)

        SmartMotionDetect is indexed by channel, like MotionDetect. Writing to
        [0] from every channel of an NVR set channel one's option no matter
        which camera the switch belonged to.
        """
        url = "/cgi-bin/configManager.cgi?action=setConfig&SmartMotionDetect[{0}].Enable={1}".format(
            channel, str(enabled).lower())
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

    async def async_get_ptz_position(self) -> dict:
        """
        Gets the status of PTZ Example output:
        status.Action=Preset
        status.MoveStatus=Idle
        status.PTS=0
        status.Postion[0]=91.600000
        status.Postion[1]=-2.600000
        status.Postion[2]=1.000000
        status.PresetID=2
        status.Sequence=0
        status.UTC=0
        status.ZoomStatus=Idle
        """
        url = "/cgi-bin/ptz.cgi?action=getStatus"
        return await self.get(url)

    @staticmethod
    def parse_ptz_preset_ids(data: list) -> list[int]:
        """Return sorted positive preset IDs from ptz.getPresets."""
        preset_ids: set[int] = set()
        if not isinstance(data, list):
            return []
        for preset in data:
            if not isinstance(preset, dict):
                continue
            value = preset.get("Index")
            if isinstance(value, bool):
                continue
            try:
                preset_id = int(value)
            except (TypeError, ValueError):
                continue
            if preset_id > 0:
                preset_ids.add(preset_id)
        return sorted(preset_ids)

    def _rpc2_key(self):
        return (self._device, self._username)

    async def _shared_rpc2(self) -> "_SharedRpc2Session":
        """This host's RPC2 session, logged in on first use.

        The login future is registered before it is awaited, so eleven channels
        of one NVR waking together share the one in flight instead of each
        starting another -- the same move _SharedRead makes for reads.

        The reference is taken here rather than in the constructor. A refcount
        can survive a failed setup; a task started in a synchronous constructor
        cannot, and that is the leak #620 was about.
        """
        key = self._rpc2_key()
        holder = _HOST_RPC2.get(key)
        if holder is None:
            session = self._new_rpc2_session()
            client = DahuaRpc2Client(
                self._username, self._password, self._address, self._port,
                self._rtsp_port, session, self._use_https
            )
            holder = _SharedRpc2Session(session, client, None)
            _HOST_RPC2[key] = holder
        if not self._rpc2_acquired:
            holder.refs += 1
            self._rpc2_acquired = True

        if holder.task is None:
            holder.task = asyncio.ensure_future(holder.client.login())
        try:
            response = await asyncio.shield(holder.task)
        except Exception:
            # Clear the login, not the holder: the entries still hold
            # references to it, and the next read should try again.
            if _HOST_RPC2.get(key) is holder and holder.task is not None and holder.task.done():
                holder.task = None
            raise

        if keepalive_needs_starting(holder.keepalive):
            interval = (response.get("params") or {}).get(
                "keepAliveInterval", RPC2_KEEPALIVE_FALLBACK_SECONDS)
            try:
                interval = max(float(interval) - RPC2_KEEPALIVE_MARGIN_SECONDS, 5.0)
            except (TypeError, ValueError):
                interval = RPC2_KEEPALIVE_FALLBACK_SECONDS - RPC2_KEEPALIVE_MARGIN_SECONDS
            holder.keepalive = asyncio.ensure_future(_rpc2_keepalive(holder, interval))
        return holder

    async def _rpc2_get_config(self, name: str) -> dict:
        """A config read over the shared session, in CGI's shape.

        One retry, because the failure this expects is an expired session and
        the answer to that is to log in again. A second failure means RPC2 is
        not going to work here, so it says so and the caller falls back.
        """
        for attempt in (1, 2):
            try:
                holder = await self._shared_rpc2()
                params = await holder.client.get_config({"name": name})
                return flatten_rpc2_config(name, params.get("table"))
            except Rpc2MethodRefused:
                # The device answered. Logging in again cannot change its mind
                # about a table it does not serve, and dropping the shared
                # session to retry costs a login for nothing.
                raise
            except Exception:  # pylint: disable=broad-except
                holder = _HOST_RPC2.get(self._rpc2_key())
                if holder is not None:
                    holder.task = None
                if attempt == 2:
                    raise
        return {}

    async def async_get_lighting_scheme(self) -> dict:
        """Which emitter the camera is willing to use, on Smart Dual Light models.

        Deliberately not part of the poll. This is read when a light command is
        given -- rare, and user initiated -- rather than on every poll for the
        sake of a warning most devices never need.

        CGI first, because that is what a camera answers and it costs no login.
        RPC2 when CGI will not answer: a recorder refuses
        getConfig&name=LightingScheme with 400 -- measured on a
        DHI-NVR5464-16P-EI and on the recorder in #647 -- while the same table
        reads perfectly over RPC2 on that second device.

        That gap is the whole reason the warning exists. #647's white light was
        held off by LightingMode=AIMode for weeks, the camera accepted every
        write and lit nothing, and the check that would have said so could not
        run because the only transport it tried was the one that recorder
        refuses.

        Judged by what comes back, not by whether something was raised:
        _request returns {} for a table a device does not have, and that is not
        a scheme.
        """
        try:
            over_cgi = await self._request(
                "/cgi-bin/configManager.cgi?action=getConfig&name=LightingScheme",
                allow_rpc2=False,
            )
            if over_cgi:
                return over_cgi
        except aiohttp.ClientResponseError:
            pass
        return await self._rpc2_get_config("LightingScheme")

    async def async_set_lighting_scheme_illuminator(
            self, channel: int, enabled: bool, brightness: int,
            profile_mode: int, light_index: int) -> dict:
        """Control a white emitter that needs LightingScheme and Lighting_V2.

        The IPC-Color4M-TZ physically requires both complete tables in one
        Web5/RPC2 transaction. This user command opens the shared RPC2 session
        even when RPC2 polling is disabled. Partial CGI writes are accepted but
        do not light the emitter.
        """
        async with asyncio.timeout(TIMEOUT_SECONDS), self._host_limit:
            holder = await self._shared_rpc2()
            scheme_params = await holder.client.get_config({"name": "LightingScheme"})
            lighting_params = await holder.client.get_config({"name": "Lighting_V2"})
            profile = int(profile_mode)
            try:
                current_mode = scheme_params["table"][channel][profile]["LightingMode"]
            except (IndexError, KeyError, TypeError):
                raise ValueError("Dahua lighting tables do not contain the selected scheme") from None
            if not isinstance(current_mode, str) or not current_mode:
                raise ValueError("Dahua lighting scheme is missing LightingMode")

            key = (channel, profile)
            restore_modes = getattr(self, "_lighting_scheme_restore_modes", None)
            if restore_modes is None:
                restore_modes = {}
                self._lighting_scheme_restore_modes = restore_modes
            restore_mode = None
            if not enabled and current_mode == "WhiteMode":
                restore_mode = restore_modes.get(key)
            elif enabled and current_mode != "WhiteMode":
                # Keep the recovery value even if the multicall reports a
                # failure: an earlier nested write may already have selected
                # WhiteMode, and a later off still needs a safe way back.
                restore_modes[key] = current_mode
            scheme, lighting = lighting_scheme_illuminator_tables(
                scheme_params.get("table"), lighting_params.get("table"), channel,
                profile, light_index, enabled, brightness, restore_mode,
            )
            clear_host_cache(self._device)
            response = await holder.client.set_configs([
                ("LightingScheme", scheme),
                ("Lighting_V2", lighting),
            ])
            if not enabled:
                restore_modes.pop(key, None)
            return response

    @staticmethod
    def _new_rpc2_session() -> aiohttp.ClientSession:
        """Use an isolated RPC2 session whose cookie jar accepts IP hosts."""
        # The unsafe cookie jar is load bearing: RPC2 sets a cookie against a
        # bare IP, which the default jar drops. enable_cleanup_closed is not --
        # aiohttp ignores it on every Python Home Assistant now runs on and
        # warns once per connector for the trouble, which is why the shared
        # connector dropped it too.
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )

    def _rpc2_session(self) -> aiohttp.ClientSession:
        """Returns this client's RPC2 session, building it on first use.

        Sessions are meant to be long lived. Creating one per call also built a
        connector and a connection pool each time, and threw them away again.
        """
        if self._rpc2_session_instance is None or self._rpc2_session_instance.closed:
            self._rpc2_session_instance = self._new_rpc2_session()
        return self._rpc2_session_instance

    async def close(self) -> None:
        """Releases anything this client owns. Safe to call more than once.

        The release is guarded by its own flag rather than by whether a socket
        closed cleanly: a failed close must not leave a second call able to
        decrement the host's refcount again.
        """
        if self._rpc2_acquired and not self._rpc2_released:
            self._rpc2_released = True
            await _release_rpc2(self._rpc2_key())
        session = self._rpc2_session_instance
        self._rpc2_session_instance = None
        if session is not None and not session.closed:
            await session.close()

    async def async_get_ptz_preset_ids(self, channel_index: int) -> list[int]:
        """Read the real preset IDs exposed by Web5.0 RPC2."""
        session = self._rpc2_session()
        rpc2 = DahuaRpc2Client(
            self._username, self._password, self._address, self._port,
            self._rtsp_port, session, self._use_https
        )
        try:
            async with asyncio.timeout(5):
                presets = await rpc2.async_get_ptz_presets(channel_index)
            ids = self.parse_ptz_preset_ids(presets)
            if presets and not ids:
                raise ValueError("Dahua RPC2 preset response contains no valid IDs")
            return ids
        finally:
            try:
                async with asyncio.timeout(3):
                    logout_ok = await rpc2.logout()
                if not logout_ok:
                    _LOGGER.debug(
                        "RPC2 logout reported failure after preset discovery"
                    )
            except Exception:
                _LOGGER.debug("RPC2 logout failed after preset discovery", exc_info=True)

    async def async_goto_preset_rpc2(self, channel: int, position: int) -> dict:
        """Go to a real preset through the hardware-validated RPC2 contract."""
        session = self._rpc2_session()
        rpc2 = DahuaRpc2Client(
            self._username, self._password, self._address, self._port,
            self._rtsp_port, session, self._use_https
        )
        try:
            async with asyncio.timeout(5):
                return await rpc2.async_goto_preset_position(channel, position)
        finally:
            try:
                async with asyncio.timeout(3):
                    logout_ok = await rpc2.logout()
                if not logout_ok:
                    _LOGGER.debug("RPC2 logout reported failure after GotoPreset")
            except Exception:
                _LOGGER.debug("RPC2 logout failed after GotoPreset", exc_info=True)

    async def _async_privacy_mode_rpc2(self, action, description: str):
        """Run one privacy-mode operation over this client's RPC2 session."""
        session = self._rpc2_session()
        rpc2 = DahuaRpc2Client(
            self._username, self._password, self._address, self._port,
            self._rtsp_port, session, self._use_https
        )
        try:
            async with asyncio.timeout(5):
                return await action(rpc2)
        finally:
            try:
                async with asyncio.timeout(3):
                    logout_ok = await rpc2.logout()
                if not logout_ok:
                    _LOGGER.debug("RPC2 logout reported failure after %s", description)
            except Exception:
                _LOGGER.debug("RPC2 logout failed after %s", description, exc_info=True)

    async def async_privacy_mode_over_cgi(self):
        """(row index, enabled) for this camera's LeLensMask, or None.

        Judged by what comes back rather than by an exception, for the reason
        async_detect_lighting_support gives: async_get_config swallows a
        ClientResponseError and returns {}, and a device can also answer 200
        with an empty body for a table it does not have -- a recorder on #669
        does exactly that for VideoAnalyseRule. Neither of those is "privacy
        mode is off", so only a response actually carrying the key counts.

        The index is returned, and not assumed, because the write has to reach
        the row the state was read from. Accepting any row while always writing
        row 0 would be a control that reports one thing and changes another on
        any device that reports more than one -- the shape of #679, #683 and
        #689. Which row a device uses is not something I can check: neither of
        mine carries this table at all.

        Lowest index first, so the answer does not depend on dict ordering.
        """
        data = await self.async_get_config("LeLensMask")
        if not data:
            return None
        for key in sorted(data):
            match = re.match(r"table\.LeLensMask\[(\d+)\]\.Enable$", key)
            if match:
                return int(match.group(1)), str(data[key]).strip().lower() == "true"
        return None

    async def async_get_privacy_mode(self) -> bool:
        """Return True if the camera's lens privacy mask is enabled.

        CGI first. The RPC2 route came first historically, but #379 has a camera
        -- an IP4M-1041W -- whose LeLensMask is readable and writable over CGI
        while RPC2 answers `Authority:check failure`, which looks like a
        permissions problem and is not one. Plain CGI is also what the Amcrest
        integration uses for this, and it costs no login of its own, where the
        RPC2 path logs in and out around every call.

        RPC2 stays as the fallback: it is the route the feature was built and
        verified on, and a camera that answers only there must keep working.
        """
        over_cgi = await self.async_privacy_mode_over_cgi()
        if over_cgi is not None:
            return over_cgi[1]
        return await self._async_privacy_mode_rpc2(
            lambda rpc2: rpc2.async_get_privacy_mode(), "privacy mode read"
        )

    async def async_set_privacy_mode(self, enabled: bool) -> None:
        """Enable or disable the camera's lens privacy mask.

        Written over whichever transport can read it, so the write never goes
        somewhere the state is not read back from.

        The CGI write names only Enable. setConfig merges, so the camera keeps
        its own TimeSection schedule -- which is what the RPC2 path takes the
        trouble to read back and rewrite by hand.
        """
        row = await self.async_privacy_mode_over_cgi()
        if row is not None:
            url = ("/cgi-bin/configManager.cgi?action=setConfig"
                   "&LeLensMask[{0}].Enable={1}").format(row[0], str(bool(enabled)).lower())
            await self.get(url, True)
            return
        await self._async_privacy_mode_rpc2(
            lambda rpc2: rpc2.async_set_privacy_mode(enabled), "privacy mode write"
        )

    async def async_get_light_global_enabled(self) -> dict:
        """
        Returns the state of the Amcrest blue ring light (if it's on or off)
        Example output:
        table.LightGlobal[0].Enable=true
        """
        url = "/cgi-bin/configManager.cgi?action=getConfig&name=LightGlobal[0].Enable"
        return await self.get(url)

    async def async_get_floodlightmode(self) -> dict:
        """ async_get_config_floodlightmode gets floodlight mode """
        url = "/cgi-bin/configManager.cgi?action=getConfig&name=FloodLightMode.Mode"
        try:
            return await self.async_get_config("FloodLightMode.Mode")
        except aiohttp.ClientResponseError as e:
            return 2

    async def async_set_floodlightmode(self, mode: int) -> dict:
        """ async_set_floodlightmode will set the floodlight lighting control  """
        # 1 - Motion Acvtivation
        # 2 - Manual (for manual switching)
        # 3 - Schedule
        # 4 - PIR
        url = "/cgi-bin/configManager.cgi?action=setConfig&FloodLightMode.Mode={mode}".format(mode=mode)
        return await self.get(url)

    async def async_set_lighting_v1(self, channel: int, enabled: bool, brightness: int,
                                    profile_mode="0") -> dict:
        """ async_get_lighting_v1 will turn the IR light (InfraRed light) on or off """
        # on = Manual, off = Off
        mode = "Manual"
        if not enabled:
            mode = "Off"
        return await self.async_set_lighting_v1_mode(channel, mode, brightness, profile_mode)

    async def async_set_lighting_v2_mode(self, channel: int, mode: str, brightness: int,
                                         profile_mode: str, light_index: int = 0,
                                         bank: str = "MiddleLight") -> dict:
        """Set the illuminator's mode and brightness, including back to Auto.

        The light entity can only say on or off, which writes Manual or Off. Off
        is not the same as automatic: it leaves the camera's own illumination
        disabled until someone puts it back, and nothing in Home Assistant could
        do that. This is the illuminator's equivalent of
        async_set_lighting_v1_mode, which infrared has had all along.

        Mode should be one of Auto, Manual or Off; On is accepted as Manual, as
        the infrared service does.
        """
        if mode.lower() == "on":
            mode = "Manual"
        # The Dahua API expects the first character capitalised.
        mode = mode.capitalize()

        url = ("/cgi-bin/configManager.cgi?action=setConfig"
               "&Lighting_V2[{channel}][{profile_mode}][{light_index}].Mode={mode}"
               "&Lighting_V2[{channel}][{profile_mode}][{light_index}].{bank}[0].Light={brightness}").format(
            channel=channel, profile_mode=profile_mode, light_index=light_index,
            mode=mode, bank=bank, brightness=brightness,
        )
        return await self.get(url)

    async def async_set_lighting_v1_mode(self, channel: int, mode: str, brightness: int,
                                         profile_mode="0") -> dict:
        """
        async_set_lighting_v1_mode will set IR light (InfraRed light) mode and brightness
        Mode should be one of: Manual, Off, or Auto
        Brightness should be between 0 and 100 inclusive. 100 being the brightest
        """

        if mode.lower() == "on":
            mode = "Manual"
        # Dahua api expects the first char to be capital
        mode = mode.capitalize()

        # The profile is the caller's, not a hardcoded 0. The poll reads this
        # channel's live profile, so writing to 0 wrote somewhere the state is
        # not read back from, and on a camera running night the camera is not
        # rendering from it either.
        url = ("/cgi-bin/configManager.cgi?action=setConfig"
               "&Lighting[{channel}][{profile}].Mode={mode}"
               "&Lighting[{channel}][{profile}].MiddleLight[0].Light={brightness}").format(
            channel=channel, profile=profile_mode, mode=mode, brightness=brightness
        )
        return await self.get(url)

    async def async_goto_preset_position(self, channel: int, position: int) -> dict:
        """
        async_goto_preset_position will go to a specific preset position
        Position should be between 1 and 10 inclusive.
        """

        url = "/cgi-bin/ptz.cgi?action=start&channel={0}&code=GotoPreset&arg1=0&arg2={1}&arg3=0".format(
            channel, position
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

    async def async_adjustfocus_v1(self, focus: str, zoom: str):
        """
        async_adjustfocus will set the zoom and focus
        """

        url = "/cgi-bin/devVideoInput.cgi?action=adjustFocus&focus={0}&zoom={1}".format(focus, zoom)
        return await self.get(url, True)

    async def async_setprivacymask(self, index: int, enabled: bool):
        """
        async_setprivacymask will enable or disable the privacy mask
        """

        url = "/cgi-bin/configManager.cgi?action=setConfig&PrivacyMasking[0][{0}].Enable={1}".format(
            index, str(enabled).lower()
        )
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
        text = _overlay_text(text1, text2)
        url = "/cgi-bin/configManager.cgi?action=setConfig&ChannelTitle[{0}].Name={1}".format(
            channel, text
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not set text")

    async def async_set_service_set_text_overlay(self, channel: int, group: int, text1: str, text2: str, text3: str,
                                                 text4: str):
        """ async_set_service_set_text_overlay sets the video text overlay """
        text = _overlay_text(text1, text2, text3, text4)
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[{0}].CustomTitle[{1}].Text={2}".format(
            channel, group, text
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not set text")

    async def async_set_service_set_custom_overlay(self, channel: int, group: int, text1: str, text2: str):
        """ async_set_service_set_custom_overlay sets the customer overlay on the video"""
        text = _overlay_text(text1, text2)
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[{0}].UserDefinedTitle[{1}].Text={2}".format(
            channel, group, text
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not set text")

    async def async_set_lighting_v2(self, channel: int, enabled: bool, brightness: int, profile_mode: str,
                                    light_index: int = 0, bank: str = "MiddleLight") -> dict:
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
        # light_index is which light this device calls the white one. It is 0 on
        # most models; some report 0 as the infrared emitter, and writing there
        # changes a light nobody can see. See illuminator_light_index.
        url = "/cgi-bin/configManager.cgi?action=setConfig&Lighting_V2[{channel}][{profile_mode}][{light_index}].Mode={mode}&Lighting_V2[{channel}][{profile_mode}][{light_index}].{bank}[0].Light={brightness}".format(
            channel=channel, profile_mode=profile_mode, mode=mode, brightness=brightness,
            light_index=light_index, bank=bank
        )
        _LOGGER.debug("Turning light on: %s", url)
        return await self.get(url)

    # async def async_set_lighting_v2_for_flood_lights(self, channel: int, enabled: bool, brightness: int, profile_mode: str) -> dict:
    async def async_set_lighting_v2_for_flood_lights(self, channel: int, enabled: bool, profile_mode: str) -> dict:
        """
        async_set_lighting_v2_for_floodlights will turn on or off the flood light on the camera. If turning on, the brightness will be used.
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
        try:
            value = await self.get(url)
            if "OK" in value or "ok" in value:
                return
        except aiohttp.ClientResponseError:
            pass

        # Plenty of devices do not have VideoInDayNight at all. Measured:
        # a DHI-NVR5464-16P-EI answers 400 Bad Request, a VTO answers "Unknown
        # error", and the DHI-VTO2311R-WP on #687 answers 400 -- while all three
        # carry VideoInOptions[channel].DayNightColor, which is what their own
        # web UI writes.
        #
        # Note this key is not profile scoped: VideoInOptions also carries
        # NightOptions.DayNightColor and NormalOptions.DayNightColor, and the
        # bare one is the setting the web UI exposes and the one verified to
        # work. So config_type has no effect on this path, and saying so is
        # better than picking a profile on a guess.
        url = "/cgi-bin/configManager.cgi?action=setConfig&VideoInOptions[{0}].DayNightColor={1}".format(
            channel, DAY_NIGHT_COLOR[mode]
        )
        value = await self.get(url)
        if "OK" not in value and "ok" not in value:
            raise Exception("Could not set Day/Night mode")

    async def async_get_video_in_options(self) -> dict:
        """The VideoInOptions table, which carries this device's Day/Night mode.

        Read whole, because neither narrower spelling works: measured on a
        DHI-NVR5464-16P-EI and a VTO, both `name=VideoInOptions[0]` and
        `name=VideoInOptions[0].DayNightColor` return an empty 200.

        It is a host-wide getConfig, so the shared read cache answers it for
        every channel of a recorder and holds it for CONFIG_CACHE_TTL_SECONDS --
        one fetch per five minutes per host rather than one per poll. A write
        clears that cache for the device, so setting the mode is reflected on
        the next read rather than up to five minutes later.
        """
        return await self.async_get_config("VideoInOptions")

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

    async def async_set_nvr_coaxial_control_state(
        self, channel: int, dahua_type: int, enabled: bool
    ) -> dict:
        """Set an NVR-connected camera's coaxial deterrence state."""
        io = 1 if enabled else 2
        url = (
            "/cgi-bin/coaxialControlIO.cgi?action=control&channel={channel}"
            "&info[0].Type={dahua_type}&info[0].IO={io}&info[0].TriggerMode=2"
        ).format(channel=channel, dahua_type=dahua_type, io=io)
        _LOGGER.debug("Setting NVR coaxial control state to %s: %s", io, url)
        return await self.get(url)

    async def async_set_disarming_linkage(self, channel: int, enabled: bool) -> dict:
        """
        async_set_disarming_linkage will set the camera's disarming linkage (Event -> Disarming in the UI)
        """

        value = "false"
        if enabled:
            value = "true"

        url = "/cgi-bin/configManager.cgi?action=setConfig&DisableLinkage[{0}].Enable={1}".format(channel, value)
        try:
            return await self.get(url)
        except aiohttp.ClientResponseError:
            # Some cameras (e.g. DH-P3D-3F-PV-P) don't support channel-indexed disarming linkage
            url = "/cgi-bin/configManager.cgi?action=setConfig&DisableLinkage.Enable={0}".format(value)
            return await self.get(url)

    async def async_set_event_notifications(self, channel: int, enabled: bool) -> dict:
        """
        async_set_event_notifications will set the camera's disarming event notifications (Event -> Disarming -> Event Notifications in the UI)
        """

        value = "true"
        if enabled:
            value = "false"

        url = "/cgi-bin/configManager.cgi?action=setConfig&DisableEventNotify[{0}].Enable={1}".format(channel, value)
        try:
            return await self.get(url)
        except aiohttp.ClientResponseError:
            # Some cameras (e.g. DH-P3D-3F-PV-P) don't support channel-indexed event notifications
            url = "/cgi-bin/configManager.cgi?action=setConfig&DisableEventNotify.Enable={0}".format(value)
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
        return await self.get(url)

    async def async_get_event_notifications(self) -> dict:
        """
        async_get_event_notifications will return false if the event notifications in disarmed state are enabled

        returns
        table.DisableEventNotify.Enable=false
        """

        url = "/cgi-bin/configManager.cgi?action=getConfig&name=DisableEventNotify"
        return await self.get(url)

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
        if "All" in events:
            codes = "All"
        else:
            codes = ",".join(events)
        url = "{0}/cgi-bin/eventManager.cgi?action=attach&codes=[{1}]&heartbeat={2}".format(
            self._base, codes, EVENT_STREAM_HEARTBEAT_SECONDS)
        if self._username is None or self._password is None:
            # Returning quietly here spun a silent sixty second retry loop that
            # never did anything and never said so.
            raise EventStreamClosed(
                "Cannot subscribe to events on %s without credentials" % self._address)

        response = None

        try:
            # A long poll must not inherit the session's default total
            # timeout, which tears a healthy stream down every 5 minutes.
            # Bound it on read instead, so a socket that stops delivering
            # is detected but one that keeps heartbeating is left alone.
            timeout = aiohttp.ClientTimeout(
                total=None, sock_read=EVENT_STREAM_READ_TIMEOUT_SECONDS)
            auth = DigestAuth(self._username, self._password, self._session, self._digest_state)
            response = await auth.request("GET", url, timeout=timeout)
            response.raise_for_status()

            # Buffer chunks until boundary delimiters so large event payloads (e.g. ANPR JSON)
            # are never split across TCP chunk boundaries.
            boundary = b"--myboundary"
            content_type = response.headers.get("Content-Type", "")
            if "boundary=" in content_type:
                b_val = content_type.split("boundary=")[1].split(";")[0].strip().strip('"\'')
                if b_val:
                    boundary = b"--" + b_val.encode()

            buffer = b""
            async for data, _ in response.content.iter_chunks():
                # Buffer multipart parts across TCP chunks. Content-Length lets
                # us deliver a complete part immediately instead of waiting for
                # the next boundary (or the next five-second heartbeat).
                if boundary in data or boundary in buffer:
                    buffer += data
                    while True:
                        complete_part, buffer = _pop_complete_multipart_part(buffer, boundary)
                        if complete_part is None:
                            if boundary not in buffer and len(buffer) > 131072:
                                buffer = buffer[-4096:]
                            break
                        on_receive(complete_part, channel)
                else:
                    on_receive(data, channel)

            if buffer and buffer.startswith(boundary) and len(buffer.strip()) > len(boundary):
                on_receive(buffer, channel)
        finally:
            if response is not None:
                response.close()

        # Falling out of the loop means the device closed the stream on us.
        # It raises no exception, so without this the caller cannot tell a
        # refused subscription from a healthy one.
        raise EventStreamClosed("Event stream to %s closed by the device" % self._address)

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

    async def async_probe_snapshot(self, channel_number: int) -> None:
        """Checks the snapshot endpoint answers for a channel, without fetching the image.

        Used only to work out how this device numbers its channels, so the JPEG
        body is never needed. Reading it would pull a full-resolution image per
        entry at setup, which is the worst possible moment on a busy NVR.
        Raises the same errors async_get_snapshot would.
        """
        url = self._base + "/cgi-bin/snapshot.cgi?channel={0}".format(channel_number)
        async with asyncio.timeout(TIMEOUT_SECONDS), self._host_limit:
            response = None
            try:
                auth = DigestAuth(self._username, self._password, self._session, self._digest_state)
                response = await auth.request("GET", url)
                response.raise_for_status()
            finally:
                if response is not None:
                    # close() rather than read(): drops the body without transferring it
                    response.close()

    async def get_bytes(self, url: str) -> bytes:
        """Get information from the API. This will return the raw response and not process it"""
        # The timeout covers the wait for a slot as well as the request, so a
        # busy host sheds load instead of building an unbounded queue.
        async with asyncio.timeout(TIMEOUT_SECONDS), self._host_limit:
            response = None
            try:
                auth = DigestAuth(self._username, self._password, self._session, self._digest_state)
                response = await auth.request("GET", self._base + url)
                response.raise_for_status()

                return await response.read()
            finally:
                if response is not None:
                    response.close()

    async def get(self, url: str, verify_ok=False) -> dict:
        """Get information from the API, sharing the read across this device.

        Two entries for one NVR asking the same question at the same moment get
        one round trip between them, and a repeat inside the TTL gets none.
        Shared per device, not per address: two boxes behind one IP on different
        ports are not each other, and answering one with the other's reply is
        how their identities got swapped (#664).
        """
        if not _is_read(url):
            # Every write passes through here, so this is the one place that
            # can say what was sent. Most write methods logged nothing at all,
            # and a few logged their own URL, so "I clicked the entity and the
            # debug log shows no request" was indistinguishable from "the
            # request was never made" -- which is exactly the question #647
            # needed answered about the infrared control.
            _LOGGER.debug("Writing to %s: %s", self._address, url)
            clear_host_cache(self._device)
            return await self._request(url, verify_ok)

        # Credentials are part of the key: entries for one device may be
        # configured with different users, and a successful read is not
        # otherwise scoped to who made it.
        key = (self._device, self._username, url)
        now = time.monotonic()
        entry = _HOST_CACHE.get(key)
        if entry is None or not entry.is_usable(now):
            # Registering before the request starts is what makes this work:
            # the per-host limiter queues callers inside _request, so by the
            # time the first one has a slot the rest are already sharing it.
            entry = _SharedRead(asyncio.ensure_future(self._request(url, verify_ok)))
            _HOST_CACHE[key] = entry

        # Shielded so that one entry's cancelled refresh -- a reload, a
        # timeout -- does not take the read away from the others.
        result = await asyncio.shield(entry.task)

        # A read is only published once it lands. A failure leaves the expiry
        # unset, so is_usable rejects it and the next poll asks the device
        # again rather than being told no for the rest of the TTL. And a write
        # that dropped this entry while it was in flight has already replaced
        # it, so it settles for whoever is waiting without going back in.
        if _HOST_CACHE.get(key) is entry and entry.expires_at is None:
            entry.expires_at = time.monotonic() + _cache_lifetime(url)

        return dict(result)

    async def _request(self, url: str, verify_ok=False, allow_rpc2=True) -> dict:
        """Make the request. One caller per shared read reaches here."""
        # Not after close(): this client has given its share back, and taking
        # a new one would build a session nobody is left to release.
        if (allow_rpc2 and self._use_rpc2 and not self._rpc2_released
                and self._rpc2_key() not in _HOST_RPC2_UNAVAILABLE and not verify_ok):
            match = _CONFIG_READ.search(url)
            if match and (self._rpc2_key(), match.group(1)) in _RPC2_TABLE_UNAVAILABLE:
                match = None    # this table only; the transport is still good
            if match:
                try:
                    async with asyncio.timeout(TIMEOUT_SECONDS), self._host_limit:
                        return await self._rpc2_get_config(match.group(1))
                except Exception as rpc2_exception:  # pylint: disable=broad-except
                    # Either way this read falls through to CGI rather than
                    # being lost. What differs is whether the host is written
                    # off: a device that cannot serve RPC2 should not pay for
                    # the attempt on every read, but one that merely did not
                    # answer in time should not lose the transport for good.
                    if isinstance(rpc2_exception, Rpc2MethodRefused):
                        # The device spoke RPC2 and declined this table. Ask
                        # CGI for it from now on, and keep the transport for
                        # everything else -- writing the host off here is what
                        # put a working device back on a login per call.
                        _RPC2_TABLE_UNAVAILABLE.add(
                            (self._rpc2_key(), match.group(1)))
                        _LOGGER.debug(
                            "%s does not serve %s over RPC2, using CGI for that "
                            "table; RPC2 is still in use for the rest",
                            self._address, match.group(1),
                        )
                    elif not rpc2_failure_is_permanent(rpc2_exception):
                        # Falls through to the CGI path below, like any other
                        # failure here, but without writing the host off.
                        _LOGGER.debug(
                            "RPC2 read timed out for %s, using CGI for this one",
                            self._address, exc_info=True,
                        )
                    else:
                        _HOST_RPC2_UNAVAILABLE.add(self._rpc2_key())
                        _LOGGER.warning(
                            "RPC2 config reads are not working for %s, using CGI instead",
                            self._address, exc_info=True,
                        )
        url = self._base + url
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS), self._host_limit:
                response = None
                try:
                    auth = DigestAuth(self._username, self._password, self._session, self._digest_state)
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

