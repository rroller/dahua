"""Ask a Dahua device to identify itself, with no credentials.

Dahua's own ConfigTool finds devices with a `DHDiscover.search` call over UDP
37810. Every public description of it -- the amplification advisories, the
third-party libraries -- gives the payload as bare JSON. **Bare JSON gets no reply.**
Measured here against a VTO2000A and a DHI-NVR5464: bare JSON, NUL-terminated JSON,
a length-prefixed form and `DHDiscover.getVersion` all return nothing.

What answers is the same JSON behind a 32 byte DHIP header, which is the framing the
device's other binary protocols use. That is the whole trick, and it is why anyone
implementing this from the public sources concludes the device is silent.

What comes back needs no authentication and is worth a lot at setup time:

    SerialNo          the id this integration already uses as its unique_id
    DeviceType        the model, e.g. VTO2000A
    DeviceClass       VTO, IPC, NVR, VTH ...
    HttpPort          which port to put in the form
    VideoInputChannels / RemoteVideoInputChannels
    Version           firmware

Measured, three probes each, every Dahua device on the network this was written
against answering every time:

    VTO2000A doorbell        3C06520PAN00001   HttpPort 80
    DHI-NVR5464 recorder     BC0A198PAJ779DF   HttpPort 80, RemoteVideoInputChannels 16
    two VTH5221D monitors    3E00E38PAN00139, 3E0164EPAN00095

**A warning worth more than the field list.** An earlier version of this sent the
magic byte-reversed, because 0x44484950 packed little endian emits "PIHD" rather than
"DHIP". With that header only the doorbell replied, and it looked exactly like
partial support across Dahua's range: a firmware quirk, something to work around,
something to write "the recorder does not answer" about. It was this code being
wrong. If coverage ever looks patchy again, suspect the frame before the devices.

Four devices on one network is still not every firmware, and a device can be busy or
switched off, so a caller must treat {} as ordinary rather than as a fault.

Nothing here sends credentials, so it cannot contribute to the login lockout that
Dahua devices apply to repeated failed authentication.
"""

import asyncio
import json
import logging
import struct

_LOGGER: logging.Logger = logging.getLogger(__package__)

DISCOVERY_PORT = 37810

# The DHIP frame, laid out to match what the device itself sends back:
#
#   0..4    header size, little endian (32)
#   4..8    the magic, the ASCII bytes "DHIP"
#   8..16   session and request id, zero for an unauthenticated discovery
#   16..24  payload length, little endian 64 bit
#   24..32  payload length again
#
# The magic is written as bytes rather than a packed integer on purpose. Expressing
# it as 0x44484950 and packing it little endian emits "PIHD", which is the sort of
# thing that works by accident and then looks correct in review.
_DHIP_MAGIC = b"DHIP"
_HEADER_SIZE = 32

_PROBE_BODY = json.dumps(
    {"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}}
).encode("utf-8")

# The device answers quickly or not at all: it is a single UDP round trip on the
# local network. A long wait would only delay a discovery that is not coming.
PROBE_TIMEOUT_SECONDS = 2.0

# Only these are read out of the reply. The blob carries more, and taking all of it
# into a config entry would be storing whatever a device felt like sending.
_WANTED = (
    "SerialNo",
    "DeviceType",
    "DeviceClass",
    "Vendor",
    "Manufacturer",
    "Version",
    "HttpPort",
    "Port",
    "MachineName",
    "VideoInputChannels",
    "RemoteVideoInputChannels",
)


def build_probe() -> bytes:
    """The bytes that make a Dahua device answer on 37810.

    Bare JSON gets no reply. The same JSON behind this 32 byte header gets one from
    every device tested.

    The devices are not uniformly strict about the header, and that nearly cost the
    whole feature: with the magic byte-reversed the doorbell still answered while the
    recorder and both indoor monitors did not. So a partly-wrong frame produces
    partly-working discovery, which reads as a device limitation rather than a bug.
    This mirrors the frame the device itself emits instead of relying on tolerance.
    """
    size = len(_PROBE_BODY)
    header = (
        struct.pack("<I", _HEADER_SIZE)
        + _DHIP_MAGIC
        + bytes(8)
        + struct.pack("<QQ", size, size)
    )
    return header + _PROBE_BODY


def parse_reply(raw: bytes) -> dict:
    """The device's own description of itself, or {} if this is not one.

    The reply is the same framing in reverse, so the JSON is found rather than
    sliced at a fixed offset: the header length has varied across the firmwares
    this was tested on, and a wrong offset would silently truncate the body.
    """
    if not raw:
        return {}
    start = raw.find(b"{")
    if start < 0:
        return {}
    try:
        message = json.loads(raw[start:].decode("utf-8", "replace").rstrip("\x00"))
    except ValueError:
        return {}
    if not isinstance(message, dict):
        return {}
    params = message.get("params")
    info = params.get("deviceInfo") if isinstance(params, dict) else None
    if not isinstance(info, dict):
        return {}

    found = {key: info[key] for key in _WANTED if key in info}
    # A reply with no serial is no use for identity, which is the main reason to
    # ask. Keep it anyway if it named a model, so the form can still be prefilled.
    if not found.get("SerialNo") and not found.get("DeviceType"):
        return {}
    return found


class _ProbeProtocol(asyncio.DatagramProtocol):
    """One reply is enough; the rest of the exchange is of no interest."""

    def __init__(self, future: asyncio.Future) -> None:
        self._future = future

    def datagram_received(self, data, addr) -> None:
        if not self._future.done():
            self._future.set_result(data)

    def error_received(self, exc) -> None:
        # An ICMP port-unreachable arrives here. It means "no discovery service",
        # which is an ordinary answer rather than a failure.
        if not self._future.done():
            self._future.set_exception(exc)


async def async_probe(address: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict:
    """Ask one address to identify itself. Never raises, returns {} when it will not.

    Swallowing the failure is deliberate: this only ever *adds* information to a
    discovery or a form, so a device that does not answer has to cost nothing.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    transport = None
    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _ProbeProtocol(future), remote_addr=(address, DISCOVERY_PORT)
        )
        transport.sendto(build_probe())
        raw = await asyncio.wait_for(future, timeout)
    except (asyncio.TimeoutError, OSError) as err:
        _LOGGER.debug("No DHDiscover reply from %s: %s", address, err)
        return {}
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("DHDiscover probe of %s failed", address, exc_info=True)
        return {}
    finally:
        if transport is not None:
            transport.close()
    info = parse_reply(raw)
    if info:
        _LOGGER.debug(
            "DHDiscover: %s identified itself as %s",
            address,
            info.get("DeviceType") or info.get("SerialNo"),
        )
    return info
