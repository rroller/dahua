"""A Dahua device on the network should be offered, not typed in.

Two halves, and they fail differently.

`discovery.py` asks a device to identify itself over UDP 37810. The framing is the
whole trick: every public description of `DHDiscover.search` gives the payload as
bare JSON, and bare JSON gets no reply. Measured against a VTO2000A and a
DHI-NVR5464, so does NUL-terminated JSON, a length-prefixed form, and
`DHDiscover.getVersion`. What answers is the same JSON behind a 32 byte DHIP header.

Every Dahua device on the network this was written against answers, three probes
each: a VTO2000A doorbell, a DHI-NVR5464 recorder and two VTH5221D indoor monitors.

That was not true of the first version, and the reason is the point of
`test_the_probe_carries_the_dhip_magic`. With the magic byte-reversed -- 0x44484950
packed little endian emits "PIHD" -- only the doorbell replied, which looked like
partial support across Dahua's range rather than a bug here. A partly-wrong frame
produces partly-working discovery, so the framing is pinned byte by byte.

Four devices is still not every firmware, and a device can be busy or off, so a
discovery must work with nothing but the DHCP announcement and treat anything the
device volunteers as a bonus.
"""

import asyncio
import json
import struct

import pytest

from custom_components.dahua import discovery
from custom_components.dahua.config_flow import DahuaFlowHandler

# The reply a VTO2000A actually sent, rebuilt from the captured fields.
MEASURED = {
    "AlarmInputChannels": 8,
    "AlarmOutputChannels": 0,
    "DeviceClass": "VTO",
    "DeviceType": "VTO2000A",
    "Find": "BC",
    "HttpPort": 80,
    "Init": 150,
    "IPv4Address": {
        "IPAddress": "192.168.0.232",
        "SubnetMask": "255.255.255.0",
        "DefaultGateway": "192.168.0.4",
        "DhcpEnable": False,
    },
    "MachineName": "3C06520PAN00001",
    "Manufacturer": "General",
    "Port": 37777,
    "RemoteVideoInputChannels": 0,
    "SerialNo": "3C06520PAN00001",
    "Vendor": "General",
    "Version": "3.120.0000.0.R",
    "VideoInputChannels": 1,
    "VideoOutputChannels": 16,
}


def _reply(device_info=None, header_bytes=32):
    """A reply framed the way the device frames it."""
    body = json.dumps(
        {
            "mac": "3c:ef:8c:41:6c:ce",
            "method": "client.notifyDevInfo",
            "params": {"deviceInfo": MEASURED if device_info is None else device_info},
        }
    ).encode("utf-8")
    return b"\x20\x00\x00\x00DHIP" + b"\x00" * (header_bytes - 8) + body


# --- the probe ---------------------------------------------------------------


def test_the_probe_is_not_bare_json():
    """The mistake every public write-up makes. If this ever becomes true again,
    no device will answer and discovery will look like a device problem."""
    probe = discovery.build_probe()

    assert not probe.startswith(b"{"), "bare JSON gets no reply from any device"


def test_the_probe_carries_the_dhip_magic():
    probe = discovery.build_probe()

    assert probe[4:8] == b"DHIP"


def test_the_header_is_32_bytes_and_the_body_follows_it():
    probe = discovery.build_probe()

    assert probe.index(b"{") == 32, "the body starts after a 32 byte header"


def test_the_header_states_the_body_length():
    """A wrong length is the other way this silently stops working."""
    probe = discovery.build_probe()
    body = probe[32:]

    declared = struct.unpack("<I", probe[16:20])[0]

    assert declared == len(body)


def test_the_declared_header_size_matches_the_real_one():
    """The first field announces the header length, and nothing else in the frame
    derives from it, so a wrong value is invisible until a device rejects the
    packet. Mine accept it either way, which is exactly why this needs pinning."""
    probe = discovery.build_probe()

    declared = struct.unpack("<I", probe[0:4])[0]

    assert declared == probe.index(
        b"{"
    ), "the announced header size must be where the body actually starts"


def test_the_probe_asks_the_documented_method():
    body = json.loads(discovery.build_probe()[32:].decode("utf-8"))

    assert body["method"] == "DHDiscover.search"


# --- reading the reply -------------------------------------------------------


def test_a_measured_reply_yields_the_identity():
    info = discovery.parse_reply(_reply())

    assert info["SerialNo"] == "3C06520PAN00001"
    assert info["DeviceType"] == "VTO2000A"
    assert info["DeviceClass"] == "VTO"
    assert info["HttpPort"] == 80


def test_the_channel_counts_come_through():
    """What would let a recorder's channel count stop being something typed."""
    info = discovery.parse_reply(_reply())

    assert info["VideoInputChannels"] == 1
    assert info["RemoteVideoInputChannels"] == 0


def test_only_the_fields_we_asked_for_are_kept():
    """The blob carries more than this. Storing all of it would mean keeping
    whatever a device felt like sending, including its addressing."""
    info = discovery.parse_reply(_reply())

    assert "IPv4Address" not in info
    assert "Find" not in info
    assert "AlarmInputChannels" not in info


def test_the_json_is_found_rather_than_sliced_at_a_fixed_offset():
    """Header length has varied across the firmwares this was tested on, and a
    fixed offset would silently truncate the body."""
    info = discovery.parse_reply(_reply(header_bytes=40))

    assert info["SerialNo"] == "3C06520PAN00001"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json at all",
        b'{"method": "client.notifyDevInfo"}',  # no params
        b'{"params": {}}',  # no deviceInfo
        b'{"params": {"deviceInfo": "a string"}}',  # wrong type
        b'{"params": null}',
        b"[]",
    ],
)
def test_anything_that_is_not_a_dahua_reply_reads_as_nothing(raw):
    """Something else on 37810 must not become a device."""
    assert discovery.parse_reply(raw) == {}


def test_a_reply_with_no_identity_at_all_is_not_useful():
    """No serial and no model: nothing to prefill and nothing to dedupe on."""
    assert discovery.parse_reply(_reply({"Vendor": "General"})) == {}


def test_a_model_without_a_serial_is_still_worth_having():
    """Enough to name the card and prefill the port, which is the point."""
    info = discovery.parse_reply(
        _reply({"DeviceType": "IPC-HDW5831R", "HttpPort": 8000})
    )

    assert info["DeviceType"] == "IPC-HDW5831R"
    assert info["HttpPort"] == 8000


# --- a device that will not answer -------------------------------------------
#
# No real sockets. Home Assistant's test framework blocks them, and a test that
# leans on a reserved address being unreachable is leaning on the network anyway.


class _SilentTransport:
    """Accepts the probe and never delivers anything back."""

    def __init__(self):
        self.sent = []

    def sendto(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


async def test_a_silent_device_costs_nothing(monkeypatch):
    """Most likely reason for silence is a device that is busy or switched off. It
    has to read as "nothing to add", not as a failure."""
    transport = _SilentTransport()

    async def _endpoint(factory, remote_addr=None):
        factory()
        return transport, None

    monkeypatch.setattr(
        asyncio.get_running_loop(), "create_datagram_endpoint", _endpoint
    )

    assert await discovery.async_probe("10.0.0.5", timeout=0.01) == {}
    assert transport.sent, "the probe should still have been sent"


async def test_the_transport_is_closed_even_when_nothing_answers(monkeypatch):
    """A socket left open per discovery would accumulate for the life of the
    process, and DHCP discovery fires whenever a lease is renewed."""
    transport = _SilentTransport()

    async def _endpoint(factory, remote_addr=None):
        factory()
        return transport, None

    monkeypatch.setattr(
        asyncio.get_running_loop(), "create_datagram_endpoint", _endpoint
    )

    await discovery.async_probe("10.0.0.5", timeout=0.01)

    assert getattr(transport, "closed", False)


async def test_an_unreachable_host_is_not_an_error(monkeypatch):
    """No route, or an ICMP port-unreachable, arrives as OSError."""

    async def _refuse(factory, remote_addr=None):
        raise OSError(101, "network unreachable")

    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", _refuse)

    assert await discovery.async_probe("10.0.0.5", timeout=0.01) == {}


async def test_a_device_that_answers_with_rubbish_is_not_a_device(monkeypatch):
    """Something else listening on 37810 must not become a discovery."""

    async def _endpoint(factory, remote_addr=None):
        protocol = factory()
        protocol.datagram_received(b"who knows", ("10.0.0.5", 37810))
        return _SilentTransport(), None

    monkeypatch.setattr(
        asyncio.get_running_loop(), "create_datagram_endpoint", _endpoint
    )

    assert await discovery.async_probe("10.0.0.5", timeout=1) == {}


async def test_a_real_reply_comes_back_parsed(monkeypatch):
    """The whole path, from probe to identity, without a network."""

    async def _endpoint(factory, remote_addr=None):
        protocol = factory()
        protocol.datagram_received(_reply(), ("10.0.0.5", 37810))
        return _SilentTransport(), None

    monkeypatch.setattr(
        asyncio.get_running_loop(), "create_datagram_endpoint", _endpoint
    )

    info = await discovery.async_probe("10.0.0.5", timeout=1)

    assert info["SerialNo"] == "3C06520PAN00001"
    assert info["HttpPort"] == 80


# --- which entries count as "this device already" ----------------------------
#
# A recorder is one entry per channel, and the unique_id is the bare serial for
# channel 0 and `serial_N` above it. Matching only the bare serial would leave a
# recorder whose channel 3 was added announcing itself as undiscovered for ever.


def _handler(entries=()):
    handler = DahuaFlowHandler()
    handler._async_current_entries = lambda: list(entries)
    return handler


def _entry(unique_id, address="10.0.0.5"):
    from types import SimpleNamespace

    return SimpleNamespace(unique_id=unique_id, data={"address": address})


def test_the_bare_serial_matches_channel_zero():
    found = _handler([_entry("SER123")])._async_entries_for_serial("SER123")

    assert len(found) == 1


def test_a_suffixed_id_matches_too():
    """Somebody who added only channel 3."""
    found = _handler([_entry("SER123_3")])._async_entries_for_serial("SER123")

    assert len(found) == 1


def test_every_channel_of_one_recorder_is_found():
    entries = [_entry("SER123"), _entry("SER123_1"), _entry("SER123_2")]

    found = _handler(entries)._async_entries_for_serial("SER123")

    assert len(found) == 3


def test_another_devices_serial_does_not_match():
    found = _handler([_entry("OTHER")])._async_entries_for_serial("SER123")

    assert found == []


def test_a_serial_that_merely_starts_the_same_does_not_match():
    """SER1234 is not a channel of SER123, and a bare startswith would say it was."""
    found = _handler([_entry("SER1234")])._async_entries_for_serial("SER123")

    assert found == [], "the separator is what makes it a channel, not a prefix"


def test_nothing_configured_matches_nothing():
    assert _handler([])._async_entries_for_serial("SER123") == []


# --- the rest of "a device that will not answer costs nothing" ----------------
#
# async_probe never raises; it returns {} for anything it cannot make sense of. That is
# deliberate, because this only ever *adds* information to a discovery or a form. Three
# of those paths had no test, and each of them is something else being on UDP 37810 or
# the network saying no.


def _framed(body: bytes) -> bytes:
    """A reply with the right DHIP header and an arbitrary body."""
    return b"\x20\x00\x00\x00DHIP" + b"\x00" * 24 + body


def _answering(monkeypatch, payload):
    """A device that sends `payload` as soon as the probe goes out."""
    transport = _SilentTransport()

    async def _endpoint(factory, remote_addr=None):
        protocol = factory()
        protocol.datagram_received(payload, ("10.0.0.5", 37810))
        return transport, None

    monkeypatch.setattr(
        asyncio.get_running_loop(), "create_datagram_endpoint", _endpoint
    )
    return transport


async def test_a_reply_whose_json_is_truncated_is_not_a_device(monkeypatch):
    """The header is right, so the framing check passes and the JSON is what fails. A
    half-sent datagram is the ordinary way that happens."""
    _answering(monkeypatch, _framed(b'{"method": "client.notifyDevInfo", "par'))

    assert await discovery.async_probe("10.0.0.5", timeout=1) == {}


async def test_a_reply_that_is_valid_json_but_not_an_object_is_not_a_device(
    monkeypatch,
):
    """`json.loads` is happy with a list or a bare string, and `.get` is not."""
    _answering(monkeypatch, _framed(b'["not", "an", "object"]'))

    assert await discovery.async_probe("10.0.0.5", timeout=1) == {}


def test_a_body_with_no_json_object_in_it_is_not_a_device():
    """Asserted on `parse_reply` directly rather than through `async_probe`, which
    catches everything and returns {} whatever happened inside it.

    These are rejected by the `raw.find(b"{") < 0` check rather than by the type check
    below it, which is worth naming because the two are easy to confuse: a JSON array or
    a bare number contains no brace at all, so the parser never gets as far as asking
    what type it decoded.
    """
    assert discovery.parse_reply(_framed(b'["not", "an", "object"]')) == {}
    assert discovery.parse_reply(_framed(b'"a bare string"')) == {}
    assert discovery.parse_reply(_framed(b"42")) == {}
    assert discovery.parse_reply(b"") == {}


def test_the_parser_rejects_a_truncated_body_without_raising():
    assert discovery.parse_reply(_framed(b'{"params": {"deviceInf')) == {}


def test_the_parser_wants_the_dhip_header():
    """No header means this is not a Dahua reply at all, whatever the body says."""
    assert discovery.parse_reply(b'{"params": {"deviceInfo": {"serialNo": "X"}}}') == {}


async def test_an_icmp_port_unreachable_is_an_answer_not_a_failure(monkeypatch):
    """It arrives at the protocol's `error_received` rather than as a raised OSError,
    which is a different path from test_an_unreachable_host_is_not_an_error above. It
    means "nothing is serving discovery here", which is ordinary."""
    transport = _SilentTransport()

    async def _endpoint(factory, remote_addr=None):
        protocol = factory()
        protocol.error_received(ConnectionRefusedError("port unreachable"))
        return transport, None

    monkeypatch.setattr(
        asyncio.get_running_loop(), "create_datagram_endpoint", _endpoint
    )

    assert await discovery.async_probe("10.0.0.5", timeout=1) == {}
    assert transport.closed, "the socket was left open"


async def test_the_protocol_reports_a_refusal_rather_than_letting_it_time_out():
    """Asserted on the protocol directly, for the same reason as the parser above: a
    refusal that is never reported still ends as {} once the timeout expires, so going
    through async_probe cannot tell the two apart. What differs is how long the caller
    waits, and discovery runs while somebody is looking at a form."""
    future = asyncio.get_running_loop().create_future()
    protocol = discovery._ProbeProtocol(future)

    protocol.error_received(ConnectionRefusedError("port unreachable"))

    assert future.done(), "the refusal was not reported, so the probe waits it out"
    with pytest.raises(ConnectionRefusedError):
        future.result()


async def test_a_second_refusal_does_not_raise_on_a_settled_future():
    """Two ICMP replies to one probe is ordinary, and setting a result twice is an
    InvalidStateError inside a transport callback."""
    future = asyncio.get_running_loop().create_future()
    protocol = discovery._ProbeProtocol(future)

    protocol.error_received(ConnectionRefusedError("first"))
    protocol.error_received(ConnectionRefusedError("second"))

    assert future.done()
    future.exception()


async def test_a_failure_that_is_not_a_network_error_still_costs_nothing(monkeypatch):
    """The broad except. Discovery runs on a DHCP lease renewal, so whatever goes wrong
    in here must not surface to the user as a problem with their camera."""

    async def _endpoint(factory, remote_addr=None):
        raise RuntimeError("something entirely unexpected")

    monkeypatch.setattr(
        asyncio.get_running_loop(), "create_datagram_endpoint", _endpoint
    )

    assert await discovery.async_probe("10.0.0.5", timeout=1) == {}
