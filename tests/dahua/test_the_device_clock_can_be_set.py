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
    """Records the write URLs, and serves the table that follows each one."""

    def __init__(self, before, after=None):
        self.urls = []
        self._before = before
        self._after = after if after is not None else before

    async def get(self, url):
        self.urls.append(url)
        return {}

    async def read(self, name):
        return self._after if self.urls else self._before


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
    after = dict(NVR_TABLE)
    after["table.NTP.Address"] = "192.168.0.10"
    after["table.NTP.ServerList[0].Address"] = "192.168.0.10"
    recorder = _Recorder(NVR_TABLE, after)
    client = _client()
    _wire(client, recorder)

    await client.async_set_ntp_server("192.168.0.10")

    url = recorder.urls[0]
    assert "NTP.Address=192.168.0.10" in url
    assert "NTP.ServerList[0].Address=192.168.0.10" in url
    assert "NTP.Enable=true" in url


async def test_the_second_server_is_left_as_a_fallback():
    after = dict(NVR_TABLE)
    after["table.NTP.Address"] = "192.168.0.10"
    after["table.NTP.ServerList[0].Address"] = "192.168.0.10"
    recorder = _Recorder(NVR_TABLE, after)
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
    recorder = _Recorder(NVR_TABLE, NVR_TABLE)
    client = _client()
    _wire(client, recorder)

    with pytest.raises(DahuaWriteDeclined) as error:
        await client.async_set_ntp_server("192.168.0.10")

    assert "kept its old value" in str(error.value)


async def test_an_empty_address_is_refused_before_anything_is_sent():
    recorder = _Recorder(NVR_TABLE)
    client = _client()
    _wire(client, recorder)

    with pytest.raises(ValueError):
        await client.async_set_ntp_server("   ")

    assert not recorder.urls


async def test_the_update_period_is_only_sent_when_asked_for():
    after = dict(NVR_TABLE)
    after["table.NTP.Address"] = "h"
    after["table.NTP.ServerList[0].Address"] = "h"

    client = _client()
    recorder = _Recorder(NVR_TABLE, after)
    _wire(client, recorder)
    await client.async_set_ntp_server("h")
    assert "UpdatePeriod" not in recorder.urls[0]

    client = _client()
    recorder = _Recorder(NVR_TABLE, after)
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
