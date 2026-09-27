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
    "IPv4Address": {"IPAddress": "192.168.0.232", "SubnetMask": "255.255.255.0",
                    "DefaultGateway": "192.168.0.4", "DhcpEnable": False},
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
    body = json.dumps({
        "mac": "3c:ef:8c:41:6c:ce",
        "method": "client.notifyDevInfo",
        "params": {"deviceInfo": MEASURED if device_info is None else device_info},
    }).encode("utf-8")
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

    assert declared == probe.index(b"{"), (
        "the announced header size must be where the body actually starts")


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


@pytest.mark.parametrize("raw", [
    b"",
    b"not json at all",
    b'{"method": "client.notifyDevInfo"}',          # no params
    b'{"params": {}}',                              # no deviceInfo
    b'{"params": {"deviceInfo": "a string"}}',       # wrong type
    b'{"params": null}',
    b"[]",
])
def test_anything_that_is_not_a_dahua_reply_reads_as_nothing(raw):
    """Something else on 37810 must not become a device."""
    assert discovery.parse_reply(raw) == {}


def test_a_reply_with_no_identity_at_all_is_not_useful():
    """No serial and no model: nothing to prefill and nothing to dedupe on."""
    assert discovery.parse_reply(_reply({"Vendor": "General"})) == {}


def test_a_model_without_a_serial_is_still_worth_having():
    """Enough to name the card and prefill the port, which is the point."""
    info = discovery.parse_reply(_reply({"DeviceType": "IPC-HDW5831R", "HttpPort": 8000}))

    assert info["DeviceType"] == "IPC-HDW5831R"
    assert info["HttpPort"] == 8000


async def test_a_device_that_does_not_answer_costs_nothing():
    """Four of the five devices this was written against do not answer. That has to
    be an ordinary outcome, not an error: 192.0.2.1 is reserved and never replies."""
    assert await discovery.async_probe("192.0.2.1", timeout=0.2) == {}


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
