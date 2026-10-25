"""Setting a device's clock and its NTP server (#436).

@xijio keeps Amcrest doorbells off the internet, so their clocks are wrong,
and asked to set the NTP server from Home Assistant. Measured before building
any of it, on 2026-10-25:

                             DHI-NVR5464-16P-EI      VTO2000A doorbell
    NTP config table         Address, Enable, Port,  TimeZone and
                             UpdatePeriod, and       TimeZoneDesc, and
                             ServerList[0..1]        nothing else at all
    global.setCurrentTime    yes                     yes
    netApp.adjustTimeWithNTP yes                     yes
    NTP result read-back     getLastStatusWithNTP    neither, and absent
                             getLastAdjustTimeStatus from listMethod

Three things follow, and this file holds each one in place.

* **A doorbell may have no server field at all.** Not empty, absent, and the
  same answer over CGI and over RPC2. So the feature as literally requested
  does nothing for the device class that prompted the request, and asking for
  it has to fail loudly rather than report success.
* **`setCurrentTime` is the one mechanism both families have**, and it needs
  nothing on the network, which is the point for a device with no internet.
  Measured on the VTO2000A: sent a time 300s ahead, the clock moved 299s, and
  no daylight saving shift was applied on top. So it takes local time, and
  the write drives the hardware rather than being accepted and ignored.
* **A recorder carries two NTP shapes at once** and says nothing about which
  one it follows, so both are written. #458 is the same trap on `VideoInMode`,
  where three shapes exist and only one was handled.
"""

import datetime
from urllib.parse import unquote

import pytest

from custom_components.dahua.client import DahuaClient, DahuaWriteDeclined

NVR_TABLE = {
    "table.NTP.Address": "au.pool.ntp.org",
    "table.NTP.Enable": "true",
    "table.NTP.Port": "123",
    "table.NTP.ServerList[0].Address": "time.windows.com",
    "table.NTP.ServerList[0].Enable": "true",
    "table.NTP.ServerList[0].Port": "123",
    "table.NTP.ServerList[1].Address": "time.google.com",
    "table.NTP.TimeZone": "16",
    "table.NTP.UpdatePeriod": "60",
}

VTO_TABLE = {
    "table.NTP.TimeZone": "16",
    "table.NTP.TimeZoneDesc": "Canberra",
}


def _client():
    return DahuaClient("admin", "secret", "192.168.1.50", 80, 554, None)


class _Recorder:
    """A device that takes a `setConfig` and applies it to its own table.

    ⚠ This used to be handed the table the read-back should return, which let
    a test assert success against an answer the test had written itself. That
    is how @rroller's #1139 finding got past a green suite: with the address
    already correct, a write the device ignored entirely still read back as
    the address that was asked for.

    So it applies the write the way a device does, and a test states what the
    device IGNORES instead. `ignores` names the fields it takes and silently
    does not apply, which is the second voice a Dahua device says no in
    (#647, #937).
    """

    def __init__(self, table, ignores=()):
        self.urls = []
        self._table = dict(table)
        self._ignores = set(ignores)

    async def get(self, url):
        self.urls.append(url)
        for pair in url.split("?", 1)[-1].split("&"):
            key, sign, value = pair.partition("=")
            if not sign or key == "action":
                continue
            if key in self._ignores:
                continue
            if "table." + key in self._table:
                self._table["table." + key] = unquote(value)
        return {}

    async def read(self, name):
        return dict(self._table)


def _wire(client, recorder):
    client.get = recorder.get
    client.async_get_config = recorder.read


# --- which slots a table offers ----------------------------------------------


def test_a_recorder_offers_both_shapes():
    assert DahuaClient.ntp_server_slots(NVR_TABLE) == ["NTP", "NTP.ServerList[0]"]


def test_a_doorbell_offers_none():
    """The measured VTO2000A table. A timezone is not a server."""
    assert DahuaClient.ntp_server_slots(VTO_TABLE) == []


def test_a_table_with_only_the_flat_field_offers_only_that():
    assert DahuaClient.ntp_server_slots({"table.NTP.Address": "x"}) == ["NTP"]


# --- writing the server ------------------------------------------------------


async def test_both_shapes_are_written_because_neither_is_known_to_win():
    recorder = _Recorder(NVR_TABLE)
    client = _client()
    _wire(client, recorder)

    await client.async_set_ntp_server("192.168.0.10")

    url = recorder.urls[0]
    assert "NTP.Address=192.168.0.10" in url
    assert "NTP.ServerList[0].Address=192.168.0.10" in url
    assert "NTP.Enable=true" in url


async def test_the_second_server_is_left_as_a_fallback():
    recorder = _Recorder(NVR_TABLE)
    client = _client()
    _wire(client, recorder)

    await client.async_set_ntp_server("192.168.0.10")

    assert "ServerList[1]" not in recorder.urls[0]


async def test_a_doorbell_is_told_it_has_no_such_setting():
    """The #436 case, and it must not report success."""
    recorder = _Recorder(VTO_TABLE)
    client = _client()
    _wire(client, recorder)

    with pytest.raises(ValueError) as error:
        await client.async_set_ntp_server("192.168.0.10")

    assert "no NTP server field" in str(error.value)
    assert not recorder.urls, "a device with no field was written to anyway"


async def test_a_write_the_device_quietly_ignored_is_a_failure():
    """200 with nothing changed is this family's other way of saying no."""
    recorder = _Recorder(NVR_TABLE, ignores=ALL_NTP_FIELDS)
    client = _client()
    _wire(client, recorder)

    with pytest.raises(DahuaWriteDeclined) as error:
        await client.async_set_ntp_server("192.168.0.10")

    assert "did not apply all of it" in str(error.value)
    assert "NTP.Address" in str(error.value)


async def test_an_empty_address_is_refused_before_anything_is_sent():
    recorder = _Recorder(NVR_TABLE)
    client = _client()
    _wire(client, recorder)

    with pytest.raises(ValueError):
        await client.async_set_ntp_server("   ")

    assert not recorder.urls


async def test_the_update_period_is_only_sent_when_asked_for():
    client = _client()
    recorder = _Recorder(NVR_TABLE)
    _wire(client, recorder)
    await client.async_set_ntp_server("h")
    assert "UpdatePeriod" not in recorder.urls[0]

    client = _client()
    recorder = _Recorder(NVR_TABLE)
    _wire(client, recorder)
    await client.async_set_ntp_server("h", update_period=30)
    assert "NTP.UpdatePeriod=30" in recorder.urls[0]


# --- setting the clock -------------------------------------------------------


class _Clock:
    """A device whose clock takes what it is sent, or does not."""

    def __init__(self, reads_back):
        self.sent = []
        self._reads_back = reads_back

    async def shared_call(self, action):
        return await action(self)

    async def request(self, method, params=None):
        if method == "global.setCurrentTime":
            self.sent.append(params["time"])
            return {"result": True}
        if method == "global.getCurrentTime":
            return {"params": {"time": self._reads_back}}
        raise AssertionError("unexpected method " + method)


def _with_clock(client, clock):
    client._rpc2_shared_call = clock.shared_call


async def test_the_clock_is_sent_as_a_plain_local_stamp():
    """Measured: the device takes local time, not UTC, and adds no DST."""
    when = datetime.datetime(2026, 10, 25, 23, 37, 56)
    clock = _Clock("2026-10-25 23:37:56")
    client = _client()
    _with_clock(client, clock)

    await client.async_set_device_time(when)

    assert clock.sent == ["2026-10-25 23:37:56"]


async def test_a_clock_that_did_not_move_is_a_refusal():
    """`result: true` is not consent on these devices."""
    when = datetime.datetime(2026, 10, 25, 23, 37, 56)
    clock = _Clock("2020-01-01 00:00:00")
    client = _client()
    _with_clock(client, clock)

    with pytest.raises(DahuaWriteDeclined) as error:
        await client.async_set_device_time(when)

    assert "still reads" in str(error.value)


async def test_a_clock_an_hour_out_is_a_refusal_too():
    """The shape a device applying its own DST on top would take."""
    when = datetime.datetime(2026, 10, 25, 23, 37, 56)
    clock = _Clock("2026-10-26 00:37:56")
    client = _client()
    _with_clock(client, clock)

    with pytest.raises(DahuaWriteDeclined):
        await client.async_set_device_time(when)


async def test_a_second_or_two_of_round_trip_is_not_a_refusal():
    when = datetime.datetime(2026, 10, 25, 23, 37, 56)
    clock = _Clock("2026-10-25 23:37:58")
    client = _client()
    _with_clock(client, clock)

    await client.async_set_device_time(when)


async def test_a_device_that_will_not_say_what_it_reads_is_not_failed():
    """The write was acknowledged. Refusing to confirm is not refusing."""
    when = datetime.datetime(2026, 10, 25, 23, 37, 56)
    clock = _Clock(None)
    client = _client()
    _with_clock(client, clock)

    await client.async_set_device_time(when)

    assert clock.sent == ["2026-10-25 23:37:56"]


async def test_a_clock_answered_as_nonsense_is_not_failed():
    when = datetime.datetime(2026, 10, 25, 23, 37, 56)
    clock = _Clock("not a time")
    client = _client()
    _with_clock(client, clock)

    await client.async_set_device_time(when)


# --- every field is checked, not just the address (#1139 review) ------------
#
# @rroller reproduced three of these against the patch as it stood: with
# `NTP.Address` already holding the address being asked for, a write the
# device ignored completely still read back as a success, because the
# read-back only compared addresses. An NTP server left disabled, or pointed
# at the wrong UDP port, reported to the user as set.
#
# The address is the field most likely to be already correct, which is what
# makes it the worst one to judge the whole write by: somebody re-running the
# service after a reboot, or setting the same server on twelve channels, hits
# the concealing case every time.

ALL_NTP_FIELDS = [
    "NTP.Enable",
    "NTP.Address",
    "NTP.Port",
    "NTP.UpdatePeriod",
    "NTP.ServerList[0].Address",
    "NTP.ServerList[0].Enable",
    "NTP.ServerList[0].Port",
]

# The recorder as somebody finds it on a second run: already pointed at the
# server being asked for, and with NTP switched off.
SAME_ADDRESS = dict(
    NVR_TABLE,
    **{
        "table.NTP.Address": "192.168.0.10",
        "table.NTP.ServerList[0].Address": "192.168.0.10",
        "table.NTP.Enable": "false",
    }
)


async def test_an_address_that_already_matched_does_not_prove_the_write_took():
    """⭐ The finding. Everything else here is a consequence of it."""
    recorder = _Recorder(SAME_ADDRESS, ignores=ALL_NTP_FIELDS)
    client = _client()
    _wire(client, recorder)

    with pytest.raises(DahuaWriteDeclined) as error:
        await client.async_set_ntp_server("192.168.0.10")

    assert "NTP.Enable" in str(error.value)


async def test_an_enable_the_device_ignored_is_a_failure():
    """NTP off and a correct server is a clock that never syncs."""
    recorder = _Recorder(SAME_ADDRESS, ignores=["NTP.Enable"])
    client = _client()
    _wire(client, recorder)

    with pytest.raises(DahuaWriteDeclined) as error:
        await client.async_set_ntp_server("192.168.0.10")

    assert "NTP.Enable" in str(error.value)
    assert "NTP.Address" not in str(error.value), "the address did take"


async def test_a_port_the_device_ignored_is_a_failure():
    recorder = _Recorder(SAME_ADDRESS, ignores=["NTP.Port"])
    client = _client()
    _wire(client, recorder)

    with pytest.raises(DahuaWriteDeclined) as error:
        await client.async_set_ntp_server("192.168.0.10", port=8123)

    assert "NTP.Port is '123', not '8123'" in str(error.value)


async def test_an_update_period_the_device_ignored_is_a_failure():
    recorder = _Recorder(SAME_ADDRESS, ignores=["NTP.UpdatePeriod"])
    client = _client()
    _wire(client, recorder)

    with pytest.raises(DahuaWriteDeclined) as error:
        await client.async_set_ntp_server("192.168.0.10", update_period=30)

    assert "NTP.UpdatePeriod is '60', not '30'" in str(error.value)


async def test_the_list_slot_is_checked_too_not_only_the_flat_one():
    """Two shapes are written, so a half-applied write is a real outcome."""
    recorder = _Recorder(NVR_TABLE, ignores=["NTP.ServerList[0].Address"])
    client = _client()
    _wire(client, recorder)

    with pytest.raises(DahuaWriteDeclined) as error:
        await client.async_set_ntp_server("192.168.0.10")

    assert "NTP.ServerList[0].Address" in str(error.value)


async def test_a_write_that_fully_took_is_success():
    """The converse, so the checks above cannot pass by always refusing."""
    recorder = _Recorder(SAME_ADDRESS)
    client = _client()
    _wire(client, recorder)

    await client.async_set_ntp_server("192.168.0.10", port=8123, update_period=30)

    assert recorder._table["table.NTP.Enable"] == "true"
    assert recorder._table["table.NTP.Port"] == "8123"
    assert recorder._table["table.NTP.UpdatePeriod"] == "30"


# --- and a field the device does not have is said up front ------------------
#
# Only fields the table actually holds are written, because a `setConfig`
# naming a field the device does not serve is refused for the whole request
# and would lose the fields that would have worked. Which leaves the question
# of what to do when the caller asked for one of those, and silence is not it:
# that is the same "reported as set, is not set" the review was about.


async def test_a_device_with_no_update_period_field_says_so():
    flat = {"table.NTP.Address": "x", "table.NTP.Enable": "false"}
    recorder = _Recorder(flat)
    client = _client()
    _wire(client, recorder)

    with pytest.raises(ValueError) as error:
        await client.async_set_ntp_server("192.168.0.10", update_period=30)

    assert "UpdatePeriod" in str(error.value)
    assert not recorder.urls, "wrote anyway and then complained"


async def test_a_device_with_no_port_field_cannot_be_given_a_port():
    flat = {"table.NTP.Address": "x", "table.NTP.Enable": "false"}
    recorder = _Recorder(flat)
    client = _client()
    _wire(client, recorder)

    with pytest.raises(ValueError) as error:
        await client.async_set_ntp_server("192.168.0.10", port=8123)

    assert "port" in str(error.value)
    assert not recorder.urls


async def test_the_default_port_is_fine_on_a_device_with_no_port_field():
    """Asking for the default on a device that has no field for it is not a
    request that went unmet, and refusing it would break the plain case."""
    flat = {"table.NTP.Address": "x", "table.NTP.Enable": "false"}
    recorder = _Recorder(flat)
    client = _client()
    _wire(client, recorder)

    await client.async_set_ntp_server("192.168.0.10")

    assert "Port" not in recorder.urls[0]
