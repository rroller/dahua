"""The diagnostics file says what each device was asked (#603).

@DeveloperMateusz counted the Login and Logout lines in two cameras' own logs:
243 in 26 minutes, where the same count ten weeks earlier was 247 in 2 hours
52. Nothing here recorded what had been asked, and the camera's log has no
URLs, so which reads those were could only be worked out by asking him for
his version, settings and capabilities.

`request_tally` counts each request as it is sent, after the read cache, by a
name that carries no value. These tests are in three parts: the names, the
counting, and the real client being wired to it at every place a request
leaves, which is the part that goes dead silently if it is only assumed.
"""

import inspect
import re
from types import SimpleNamespace

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua import request_tally
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.diagnostics import _host_block
from custom_components.dahua.rpc2 import DahuaRpc2Client

from .test_digest import PASSWORD, USER, FakeSession

CGI = "/cgi-bin/"


# --- the names ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, name",
    [
        (
            CGI + "configManager.cgi?action=getConfig&name=MotionDetect",
            "configManager getConfig MotionDetect",
        ),
        # The channel and the profile are not part of what was asked for: one
        # row per table, or a recorder fills the list with its channel numbers.
        (
            CGI + "configManager.cgi?action=getConfig&name=Lighting[11][2]",
            "configManager getConfig Lighting",
        ),
        (
            CGI + "configManager.cgi?action=getConfig&name=Encode[0].MainFormat[0]",
            "configManager getConfig Encode",
        ),
        (
            "http://10.0.0.5:80" + CGI + "magicBox.cgi?action=getSystemInfo",
            "magicBox getSystemInfo",
        ),
        (
            CGI + "magicBox.cgi?action=getProductDefinition&name=MaxExtraStream",
            "magicBox getProductDefinition MaxExtraStream",
        ),
        (
            CGI + "coaxialControlIO.cgi?action=getStatus&channel=1",
            "coaxialControlIO getStatus",
        ),
        (CGI + "snapshot.cgi?channel=3", "snapshot"),
        (
            CGI + "eventManager.cgi?action=attach&codes=[VideoMotion]&heartbeat=5",
            "eventManager attach",
        ),
        (
            CGI + "audio.cgi?action=postAudio&httptype=singlepart&channel=1",
            "audio postAudio",
        ),
        (CGI + "magicBox.cgi?action=reboot", "magicBox reboot"),
        ("", "other"),
        ("not a url at all", "other"),
        (CGI + "weird.cgi?action=a b", "weird other"),
    ],
)
def test_a_request_is_named_by_what_it_asks_for(url, name):
    assert request_tally.request_name(url) == name


@pytest.mark.parametrize(
    "url, name",
    [
        (
            CGI
            + "configManager.cgi?action=setConfig&ChannelTitle[0].Name=Front%20Porch",
            "configManager setConfig ChannelTitle",
        ),
        (
            CGI + "configManager.cgi?action=setConfig&NTP.Address=pool.example.org",
            "configManager setConfig NTP",
        ),
        (
            CGI
            + "configManager.cgi?action=setConfig&VideoWidget[0].CustomTitle[1].Text=Secret",
            "configManager setConfig VideoWidget",
        ),
    ],
)
def test_a_write_is_named_by_its_table_and_never_by_what_was_written(url, name):
    got = request_tally.request_name(url)

    assert got == name
    for value in ("Front", "Porch", "pool", "example", "Secret"):
        assert value not in got


def test_a_name_is_never_the_url():
    """Whatever it is handed, including things that are not strings."""
    for odd in (None, 5, object(), "http://u:p@host/x?token=abc", "?password=hunter2"):
        got = request_tally.request_name(odd)
        assert "hunter2" not in got and "abc" not in got and "u:p" not in got


@pytest.mark.parametrize(
    "method, params, name",
    [
        (
            "configManager.getConfig",
            {"name": "Encode"},
            "rpc2 configManager.getConfig Encode",
        ),
        (
            "configManager.getConfig",
            {"name": "Lighting[3]", "onlyLocal": False},
            "rpc2 configManager.getConfig Lighting",
        ),
        ("magicBox.getSystemInfo", "unset", "rpc2 magicBox.getSystemInfo"),
        (
            "eventManager.getEventIndexes",
            {"code": "VideoMotion"},
            "rpc2 eventManager.getEventIndexes",
        ),
        (None, None, "rpc2 other"),
    ],
)
def test_an_rpc2_call_is_named_by_its_method(method, params, name):
    assert request_tally.rpc2_request_name(method, params) == name


def test_a_name_somebody_typed_is_not_kept():
    """Outside the config manager `name` is not a table. A login carries the
    user name and a password hash, and a preset can be named anything."""
    login = {"userName": "admin", "password": "5F4DCC3B", "name": "admin"}

    assert request_tally.rpc2_request_name("global.login", login) == "rpc2 global.login"
    assert (
        request_tally.rpc2_request_name("ptz.setPreset", {"name": "Garden gate"})
        == "rpc2 ptz.setPreset"
    )


# --- the counting ---------------------------------------------------------------------

DEVICE = "10.0.0.5:80"
T0 = 1_700_000_040.0  # the start of a minute


def test_a_device_nothing_was_sent_to_says_so():
    assert request_tally.summary(DEVICE, now=T0) == {"tracked": False}


def test_requests_are_counted_by_name_most_frequent_first():
    for name, times in (("b read", 2), ("a read", 5), ("c read", 2)):
        for _ in range(times):
            request_tally.note(DEVICE, name, now=T0)

    got = request_tally.summary(DEVICE, now=T0)

    assert got["total_since_start"] == 9
    assert list(got["last_10_minutes"]["by_request"].items()) == [
        ("a read", 5),
        ("b read", 2),
        ("c read", 2),
    ]


def test_the_two_windows_are_ten_minutes_and_an_hour():
    request_tally.note(DEVICE, "old", now=T0)
    request_tally.note(DEVICE, "recent", now=T0 + 55 * 60)

    got = request_tally.summary(DEVICE, now=T0 + 59 * 60)

    assert got["last_10_minutes"]["by_request"] == {"recent": 1}
    assert got["last_60_minutes"]["by_request"] == {"old": 1, "recent": 1}
    assert got["tracked_for_minutes"] == 60


def test_what_is_older_than_an_hour_is_dropped_but_still_in_the_total():
    request_tally.note(DEVICE, "old", now=T0)
    request_tally.note(DEVICE, "new", now=T0 + 61 * 60)

    got = request_tally.summary(DEVICE, now=T0 + 61 * 60)

    assert got["last_60_minutes"]["by_request"] == {"new": 1}
    assert got["total_since_start"] == 2
    assert len(request_tally._TALLY[DEVICE]["minutes"]) == 1, "the old bucket is gone"


def test_the_buckets_kept_are_bounded_however_long_it_runs():
    for minute in range(600):
        request_tally.note(DEVICE, "read", now=T0 + minute * 60)

    assert len(request_tally._TALLY[DEVICE]["minutes"]) == request_tally.WINDOW_MINUTES


def test_the_rate_is_over_the_time_there_was_to_count_in():
    """Three minutes after the first request, thirty requests is ten a minute
    and not three, which is what dividing by the whole window would say."""
    for minute in range(3):
        for _ in range(10):
            request_tally.note(DEVICE, "read", now=T0 + minute * 60)

    got = request_tally.summary(DEVICE, now=T0 + 2 * 60)

    assert got["last_10_minutes"]["minutes_covered"] == 3
    assert got["last_10_minutes"]["per_minute"] == 10.0
    assert got["last_60_minutes"]["per_minute"] == 10.0


def test_the_rate_is_over_the_whole_window_once_it_has_been_running_that_long():
    request_tally.note(DEVICE, "read", now=T0)
    for _ in range(20):
        request_tally.note(DEVICE, "read", now=T0 + 30 * 60)

    got = request_tally.summary(DEVICE, now=T0 + 30 * 60)

    assert got["last_10_minutes"] == {
        "minutes_covered": 10,
        "total": 20,
        "per_minute": 2.0,
        "by_request": {"read": 20},
    }


def test_each_device_has_its_own_count():
    request_tally.note("10.0.0.5:80", "read", now=T0)
    request_tally.note("10.0.0.5:81", "read", now=T0)
    request_tally.note("10.0.0.5:81", "read", now=T0)

    assert request_tally.summary("10.0.0.5:80", now=T0)["total_since_start"] == 1
    assert request_tally.summary("10.0.0.5:81", now=T0)["total_since_start"] == 2


def test_a_client_that_cannot_say_which_device_it_is_counts_nothing():
    request_tally.note(None, "read", now=T0)

    assert request_tally._TALLY == {}


# --- the real client ------------------------------------------------------------------

MOTION = CGI + "configManager.cgi?action=getConfig&name=MotionDetect"
STATUS = CGI + "coaxialControlIO.cgi?action=getStatus&channel=1"


def _client(session=None, port=80):
    return DahuaClient(USER, PASSWORD, "d", port, 554, session or FakeSession())


def _asked(client):
    return (
        request_tally.summary(client._device)
        .get("last_10_minutes", {})
        .get("by_request", {})
    )


@pytest.fixture(autouse=True)
def _clean():
    client_module._HOST_DIGEST_STATE.clear()
    client_module._HOST_CACHE.clear()
    yield
    client_module._HOST_DIGEST_STATE.clear()
    client_module._HOST_CACHE.clear()


async def test_a_read_that_reaches_the_device_is_counted_once():
    """Once, although the digest exchange made two round trips to send it."""
    session = FakeSession()
    client = _client(session)

    await client.get(MOTION)

    assert len(session.requests) == 2, "an unsigned try and the signed one"
    assert _asked(client) == {"configManager getConfig MotionDetect": 1}


async def test_a_read_answered_from_the_cache_is_not_counted():
    """The point of counting where the request is sent: the cache is what
    keeps a poll cheap, and a count taken above it would not show it working."""
    session = FakeSession()
    client = _client(session)

    for _ in range(4):
        await client.get(MOTION)

    assert _asked(client) == {"configManager getConfig MotionDetect": 1}


async def test_a_read_shared_by_two_entries_is_counted_once():
    session = FakeSession()
    one, other = _client(session), _client(session)

    await one.get(MOTION)
    await other.get(MOTION)

    assert _asked(one) == {"configManager getConfig MotionDetect": 1}


async def test_a_status_read_and_a_still_are_counted_too():
    client = _client(FakeSession(body="status.Speaker=Off"))

    await client.get(STATUS)
    await client.get_bytes(CGI + "snapshot.cgi?channel=1")

    assert _asked(client) == {"coaxialControlIO getStatus": 1, "snapshot": 1}


async def test_a_write_is_counted_under_its_table():
    client = _client(FakeSession(body="OK"))

    await client.get(
        CGI + "configManager.cgi?action=setConfig&ChannelTitle[0].Name=Porch"
    )

    assert _asked(client) == {"configManager setConfig ChannelTitle": 1}


async def test_a_request_the_device_refuses_was_still_sent():
    """It cost the device a login either way, which is what is being counted."""
    client = _client(FakeSession(password="something else"))

    with pytest.raises(Exception):
        await client.get(MOTION)

    assert _asked(client) == {"configManager getConfig MotionDetect": 1}


def test_every_place_a_request_leaves_the_client_is_counted():
    """A sixth way out added without the count would make the dump quietly
    under-report, and nothing else here would notice."""
    source = inspect.getsource(client_module).splitlines()
    sends = [i for i, line in enumerate(source) if "await auth.request(" in line]

    assert len(sends) == 4, "the request sites moved; count the new one too"
    for index in sends:
        assert (
            "self._note_request(" in source[index - 1]
        ), "line %d sends a request that is not counted" % (index + 1)
    text = "\n".join(source)
    assert len(re.findall(r"\bDigestAuth\(", text)) == 5, (
        "four that send through `auth.request` and the speaker, which signs "
        "its own POST"
    )
    assert text.count("self._note_request(") == 5


# --- RPC2 -----------------------------------------------------------------------------


async def test_the_rpc2_client_says_what_it_is_about_to_send():
    heard = []
    rpc2 = object.__new__(DahuaRpc2Client)
    rpc2._id = 0
    rpc2._session_id = None
    rpc2._base = "http://d"
    rpc2.on_request = lambda method, params=None: heard.append((method, params))

    async def _post(url, data):
        raise ConnectionError("nothing is listening, which is beside the point")

    rpc2._post = _post

    with pytest.raises(ConnectionError):
        await rpc2.request("configManager.getConfig", {"name": "Encode"})

    assert heard == [("configManager.getConfig", {"name": "Encode"})]


async def test_an_rpc2_client_nobody_listens_to_still_works():
    assert DahuaRpc2Client.on_request is None


async def test_the_shared_session_is_counted_against_its_device(monkeypatch):
    """So a device whose config reads moved to RPC2 shows as that, and not as
    a device that is asked less."""

    class _Rpc2:
        on_request = None

        def __init__(self, *args, **kwargs):
            pass

        async def login(self):
            self.on_request("global.login", {"userName": "admin", "password": "x"})
            return {"params": {"keepAliveInterval": 60}}

        async def get_config(self, params):
            self.on_request("configManager.getConfig", params)
            return {"table": []}

    monkeypatch.setattr(client_module, "DahuaRpc2Client", _Rpc2)
    monkeypatch.setattr(client_module, "keepalive_needs_starting", lambda task: False)
    client = _client()
    monkeypatch.setattr(
        client, "_new_rpc2_session", lambda: SimpleNamespace(closed=True)
    )
    monkeypatch.setattr(client, "_new_dhip_transport", lambda: None)

    try:
        await client._rpc2_shared_call(
            lambda rpc2: rpc2.get_config({"name": "MotionDetect"})
        )
    finally:
        client_module._HOST_RPC2.pop(client._rpc2_key(), None)

    assert _asked(client) == {
        "rpc2 global.login": 1,
        "rpc2 configManager.getConfig MotionDetect": 1,
    }


# --- the dump -------------------------------------------------------------------------


def _block(hass, client):
    coordinator = SimpleNamespace(client=client, _address="d")
    config_entry = SimpleNamespace(
        entry_id="entry-1",
        title="Camera",
        data={"address": "d", "channel": 0},
        state="ConfigEntryState.LOADED",
    )
    return _host_block(hass, coordinator, config_entry)


async def test_the_dump_reports_what_the_real_client_sent(hass):
    """Through the real request path and the real block, so the field cannot
    be reading a count that nothing increments."""
    client = _client(FakeSession())

    await client.get(MOTION)
    await client.get(MOTION)
    await client.get(STATUS)

    requests = _block(hass, client)["requests"]
    assert requests["tracked"] is True
    assert requests["total_since_start"] == 2
    assert requests["last_10_minutes"]["by_request"] == {
        "configManager getConfig MotionDetect": 1,
        "coaxialControlIO getStatus": 1,
    }


async def test_the_dump_for_a_device_not_yet_asked_anything_says_so(hass):
    assert _block(hass, _client())["requests"] == {"tracked": False}


async def test_another_device_on_the_same_address_has_its_own_section(hass):
    on_80, on_81 = _client(port=80), _client(port=81)

    await on_80.get(MOTION)

    assert _block(hass, on_80)["requests"]["total_since_start"] == 1
    assert _block(hass, on_81)["requests"] == {"tracked": False}


async def test_nothing_in_the_section_is_anything_but_a_name_and_a_number(hass):
    client = _client(FakeSession(body="OK"))
    await client.get(
        CGI + "configManager.cgi?action=setConfig&NTP.Address=pool.example.org"
    )

    text = repr(_block(hass, client)["requests"])

    assert "configManager setConfig NTP" in text
    for leaked in ("pool", "example", PASSWORD, USER, "nonce"):
        assert leaked not in text
