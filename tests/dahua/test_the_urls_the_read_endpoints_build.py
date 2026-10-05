"""The URLs the read endpoints build, and the two that reshape what comes back.

Nineteen methods whose entire body is "build a URL, send it, return the answer". None was
executed, and for a method that thin the URL **is** the contract: there is nothing else to
get wrong and nothing else to test.

A wrong URL here fails in the quietest way this device has. `configManager.cgi` answers
`OK` to a `setConfig` naming a key it does not have, and a `getConfig` for a table that
does not exist comes back empty rather than as an error, so the integration reports
success or reads a missing value and carries on. Nothing appears in the log either way.

So this is a table, and the important assertion is not any single row: it is that **no two
methods build the same URL**. These nineteen were written by copying each other, which is
the right way to write them and the reason a per-method test proves least — each
assertion gets written from the same line it is checking, so a method pointed at its
neighbour's table passes its own test. The table catches that; nineteen separate tests
could not.

Two of them do more than pass the answer through, and those have their own tests:
`async_get_alarm_output_state` reshapes the reply into the exact key
`coordinator.is_alarm_output_on` reads, and `async_get_snapshot` pipes the bytes through
both JPEG repairs.

`verify_response` is in the table too. Three of these ask `get` to check the reply and the
rest do not, which is a real difference: the three are writes dressed as reads.
"""

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient

CHANNEL = 3
POSITION = 5
INDEX = 2

# method, positional arguments, the URL it must build, and whether it asks `get` to
# check the reply. Taken from the source one at a time, not by pattern.
ENDPOINTS = [
    (
        "async_get_alarm_output_slots",
        (),
        "/cgi-bin/alarm.cgi?action=getOutSlots",
        False,
    ),
    (
        "async_get_coaxial_control_io_status",
        (CHANNEL,),
        "/cgi-bin/coaxialControlIO.cgi?action=getStatus&channel=3",
        False,
    ),
    (
        "async_get_lighting_v2",
        (),
        "/cgi-bin/configManager.cgi?action=getConfig&name=Lighting_V2",
        False,
    ),
    (
        "async_get_smart_motion_detection",
        (),
        "/cgi-bin/configManager.cgi?action=getConfig&name=SmartMotionDetect",
        False,
    ),
    ("async_get_ptz_position", (), "/cgi-bin/ptz.cgi?action=getStatus", False),
    (
        "async_get_ptz_presets",
        (CHANNEL,),
        "/cgi-bin/ptz.cgi?action=getPresets&channel=3",
        False,
    ),
    (
        "async_get_light_global_enabled",
        (),
        "/cgi-bin/configManager.cgi?action=getConfig&name=LightGlobal[0].Enable",
        False,
    ),
    (
        "async_get_remote_devices",
        (),
        "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice",
        False,
    ),
    (
        "async_get_video_widget",
        (),
        "/cgi-bin/configManager.cgi?action=getConfig&name=VideoWidget",
        False,
    ),
    (
        "async_get_video_in_mode",
        (),
        "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInMode",
        False,
    ),
    (
        "async_get_disarming_linkage",
        (),
        "/cgi-bin/configManager.cgi?action=getConfig&name=DisableLinkage",
        False,
    ),
    (
        "async_get_event_notifications",
        (),
        "/cgi-bin/configManager.cgi?action=getConfig&name=DisableEventNotify",
        False,
    ),
    (
        "async_goto_preset_position",
        (CHANNEL, POSITION),
        "/cgi-bin/ptz.cgi?action=start&channel=3&code=GotoPreset"
        "&arg1=0&arg2=5&arg3=0",
        False,
    ),
    (
        "async_set_floodlightmode",
        (2,),
        "/cgi-bin/configManager.cgi?action=setConfig&FloodLightMode.Mode=2",
        False,
    ),
    # The three that ask `get` to check the reply: writes dressed as reads.
    (
        "async_set_light_global_enabled",
        (True,),
        "/cgi-bin/configManager.cgi?action=setConfig&LightGlobal[0].Enable=true",
        True,
    ),
    (
        "async_adjustfocus_v1",
        ("0.5", "0.7"),
        "/cgi-bin/devVideoInput.cgi?action=adjustFocus&focus=0.5&zoom=0.7",
        True,
    ),
    (
        "async_setprivacymask",
        (INDEX, True),
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&PrivacyMasking[0][2].Enable=true",
        True,
    ),
]


class _Sent:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = {} if answer is None else answer

    async def get(self, url, verify_response=False):
        self.calls.append((url, verify_response))
        return self.answer

    async def get_bytes(self, url):
        self.calls.append((url, None))
        return self.answer


def _client(answer=None):
    client = object.__new__(DahuaClient)
    sent = _Sent(answer)
    client.get = sent.get
    client.get_bytes = sent.get_bytes
    client._sent = sent
    return client


async def _call(method, args, answer=None):
    client = _client(answer)
    result = await getattr(client, method)(*args)
    assert len(client._sent.calls) == 1, client._sent.calls
    return client._sent.calls[0], result


# --- the URL each one builds -------------------------------------------------


@pytest.mark.parametrize("method, args, url, _verify", ENDPOINTS)
async def test_the_endpoint_builds_its_url(method, args, url, _verify):
    (sent, _flag), _result = await _call(method, args)

    assert sent == url


@pytest.mark.parametrize("method, args, _url, verify", ENDPOINTS)
async def test_only_the_writes_ask_for_the_reply_to_be_checked(
    method, args, _url, verify
):
    """`verify_response` makes `get` raise on a reply that is not OK. Three of these
    are writes, and asking for a check on a read would reject a perfectly good empty
    table."""
    (_sent, flag), _result = await _call(method, args)

    assert bool(flag) is verify


async def test_no_two_endpoints_build_the_same_url():
    """The assertion the per-method tests cannot make.

    These were written by copying each other, so a method left pointing at its
    neighbour's table passes its own test: the expected URL was read off the same wrong
    line. Only comparing them all catches it.
    """
    built = {}
    for method, args, _url, _verify in ENDPOINTS:
        (sent, _flag), _result = await _call(method, args)
        built[method] = sent

    duplicates = [m for m, u in built.items() if list(built.values()).count(u) > 1]
    assert not duplicates, "these build an identical URL: %s" % sorted(duplicates)


@pytest.mark.parametrize("method, args, url, _verify", ENDPOINTS)
async def test_every_argument_reaches_the_url(method, args, url, _verify):
    """A dropped argument is the other silent failure: the URL is still valid, it just
    names channel 0 or preset 0 for ever."""
    for argument in args:
        if isinstance(argument, bool) or argument in ("", None):
            continue
        rendered = (
            str(argument).lower() if isinstance(argument, bool) else str(argument)
        )
        assert rendered in url, "%s does not carry %r in its expected URL" % (
            method,
            argument,
        )


@pytest.mark.parametrize("channel", [0, 1, 7, 15])
async def test_the_channel_is_the_one_it_was_given(channel):
    """Spelled out for one of them, because the table only proves the argument appears
    somewhere in a URL built with one value."""
    (sent, _flag), _result = await _call("async_get_ptz_presets", (channel,))

    assert sent.endswith("&channel=%d" % channel), sent


@pytest.mark.parametrize("enabled, written", [(True, "true"), (False, "false")])
async def test_a_boolean_is_written_the_way_the_device_spells_it(enabled, written):
    (sent, _flag), _result = await _call("async_set_light_global_enabled", (enabled,))

    assert sent.endswith("=%s" % written), sent


# --- the two that reshape what comes back ------------------------------------


async def test_the_alarm_output_state_is_reshaped_into_the_key_that_is_read():
    """`coordinator.is_alarm_output_on` reads exactly `status.AlarmOut[0]`, so this
    method and that reader have to agree on the spelling or the sensor never moves."""
    (sent, _flag), result = await _call(
        "async_get_alarm_output_state", (), answer={"result": "1"}
    )

    assert sent == "/cgi-bin/alarm.cgi?action=getOutState"
    assert result == {"status.AlarmOut[0]": "1"}


async def test_an_alarm_output_reply_with_no_result_is_still_shaped():
    """The value is deliberately passed through unmodified, so a device that answers
    something unexpected yields None rather than an error here."""
    _call_args, result = await _call("async_get_alarm_output_state", (), answer={})

    assert result == {"status.AlarmOut[0]": None}


async def test_the_snapshot_goes_through_both_jpeg_repairs(monkeypatch):
    """Both, in order. Each fixes a different malformation and a snapshot can have
    either, so dropping one produces a JPEG that some viewers reject and others do not.
    """
    applied = []

    def _strip(data):
        applied.append("strip")
        return data + b"-stripped"

    def _repair(data):
        applied.append("repair")
        return data + b"-repaired"

    monkeypatch.setattr(client_module, "strip_dahua_snapshot_trailer", _strip)
    monkeypatch.setattr(client_module, "repair_dahua_snapshot_header", _repair)

    (sent, _flag), result = await _call("async_get_snapshot", (4,), answer=b"jpeg")

    assert sent == "/cgi-bin/snapshot.cgi?channel=4"
    assert applied == ["strip", "repair"], applied
    assert result == b"jpeg-stripped-repaired"


async def test_the_snapshot_asks_for_the_channel_number_it_was_given():
    """Channel *number*, not index: channel index 0 is number 1, and the caller does
    that conversion. Sending the index asks for the wrong camera."""
    (sent, _flag), _result = await _call("async_get_snapshot", (1,), answer=b"x")

    assert sent == "/cgi-bin/snapshot.cgi?channel=1"


# --- and that the table is not quietly incomplete ---------------------------


def test_every_endpoint_in_the_table_exists_on_the_client():
    """A typo in a method name here would make its row silently test nothing, because
    `getattr` would raise and the test would read as an error rather than a gap."""
    for method, _args, _url, _verify in ENDPOINTS:
        assert hasattr(DahuaClient, method), method
