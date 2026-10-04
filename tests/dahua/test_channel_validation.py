"""The form must check what it is given, and refuse only what the device disowns.

The channel was accepted unchecked. `_test_credentials` takes it as its sixth
argument and never uses it, and `DahuaClient` has no channel parameter at all -- its
`__init__` takes username, password, address, port, rtsp_port, session, use_https,
use_rpc2 and illuminator_restore_store. Both calls it makes, `get_machine_name` and
`async_get_system_info`, are device wide.

So typing the number the recorder displays, 4 instead of 3, or 16 on a sixteen
channel box, produced a green tick and an entry whose every entity was dead. #646 is
someone who added two of their three cameras and never found the third.

The line this draws matters as much as the check. Measured on a DHI-NVR5464, the
RemoteDevice table is keyed 0..15 in the same space as this field, and says for each
slot whether it is enabled and which protocol it uses. Those are statements by the
recorder about its own hardware, so they can be refused. Anything else is a guess and
is left alone -- a standalone camera has no such table, and neither does a multi-lens
camera serving several channels over one address.
"""

import pytest

from custom_components.dahua import config_flow
from custom_components.dahua.config_flow import (
    CHANNEL_ERRORS,
    TRANSPORT_WORKED,
    DahuaFlowHandler,
    async_channel_refusal,
)

# Deliberately the module's own voluptuous rather than a fresh import. Home Assistant
# aliases voluptuous to probatio in sys.modules, so importing it separately can yield
# a second engine whose Invalid is unrelated to the one the schema raises.
vol = config_flow.vol

# A sixteen slot recorder shaped like the one this was measured on: mostly enabled,
# one switched off, one reached over Onvif. Built in the flat CGI shape the device
# actually returns, so the real parse_remote_devices runs rather than a stub of it.
_KEY = "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_%d.%s"


def _cgi_table(slots):
    """slots: {index: (enabled, protocol)} -> the flat reply the recorder sends."""
    out = {}
    for index, (enabled, protocol) in slots.items():
        out[_KEY % (index, "Enable")] = "true" if enabled else "false"
        out[_KEY % (index, "ProtocolType")] = protocol
    return out


SLOTS = {index: (True, "Private") for index in range(16)}
SLOTS[10] = (False, "Private")
SLOTS[14] = (True, "Onvif")
TABLE = _cgi_table(SLOTS)


class _Recorder:
    """A client whose RemoteDevice read returns `table`, or raises `error`."""

    def __init__(self, table=None, error=None):
        self._table = table
        self._error = error
        self.reads = 0

    async def async_get_remote_devices(self):
        self.reads += 1
        if self._error is not None:
            raise self._error
        return self._table


def test_the_fixture_matches_what_the_recorder_reports():
    """If this drifts, every refusal below is being checked against a shape the
    device never sends. Measured on a DHI-NVR5464: keys 0..15, 12 enabled."""
    from custom_components.dahua import dahua_utils

    parsed = dahua_utils.parse_remote_devices(TABLE)

    assert sorted(parsed) == list(range(16))
    assert parsed[3] == {"enabled": True, "protocol": "private"}
    assert parsed[10]["enabled"] is False
    assert parsed[14]["protocol"] == "onvif"


# --- what the recorder disowns ----------------------------------------------


async def test_a_channel_the_recorder_does_not_have_is_refused():
    """16 on a sixteen channel recorder: the number it shows, one past the last
    index. This is the commonest form of the mistake."""
    assert await async_channel_refusal(_Recorder(TABLE), 16) == "channel_not_on_device"


async def test_a_wildly_wrong_channel_is_refused():
    assert await async_channel_refusal(_Recorder(TABLE), 47) == "channel_not_on_device"


async def test_a_switched_off_slot_says_so():
    """It exists, so "does not have that channel" would be wrong and unhelpful."""
    assert await async_channel_refusal(_Recorder(TABLE), 10) == "channel_disabled"


async def test_an_onvif_slot_says_so():
    """The recorder does not serve these on its own Dahua paths (#710), so the entry
    could never work, and the fix is a different integration."""
    assert await async_channel_refusal(_Recorder(TABLE), 14) == "channel_is_onvif"


async def test_a_good_channel_is_accepted():
    assert await async_channel_refusal(_Recorder(TABLE), 3) is None


async def test_the_last_real_channel_is_accepted():
    """Off by one in the refusal would be worse than no check at all."""
    assert await async_channel_refusal(_Recorder(TABLE), 15) is None


# --- and everything it does not claim ---------------------------------------


async def test_channel_zero_is_never_questioned():
    """A standalone camera at least as often as a recorder's first slot."""
    recorder = _Recorder(TABLE)

    assert await async_channel_refusal(recorder, 0) is None
    assert recorder.reads == 0, "channel 0 should not spend a request"


async def test_a_camera_with_no_table_is_left_alone():
    """A standalone camera has no RemoteDevice table. Refusing on its absence would
    break every direct camera."""
    recorder = _Recorder(error=Exception("400 Bad Request"))

    assert await async_channel_refusal(recorder, 2) is None


async def test_a_multi_lens_camera_is_left_alone():
    """Several channels over one address and no recorder table. This is why "no
    table and channel > 0" must not be a refusal."""
    assert await async_channel_refusal(_Recorder({}), 2) is None


async def test_an_unreadable_table_claims_nothing():
    assert await async_channel_refusal(_Recorder(error=TimeoutError()), 5) is None


async def test_a_channel_that_is_not_a_number_is_not_our_problem():
    assert await async_channel_refusal(_Recorder(TABLE), "not a number") is None
    assert await async_channel_refusal(_Recorder(TABLE), None) is None


async def test_an_empty_slot_that_is_enabled_is_still_accepted():
    """A camera removed from the recorder leaves a stale enabled slot, and only a
    snapshot probe tells the difference. Refusing here would be a guess."""
    table = _cgi_table({0: (True, "Private"), 1: (True, "Private")})

    assert await async_channel_refusal(_Recorder(table), 1) is None


# --- the error has to reach the field ---------------------------------------


def test_every_channel_error_is_listed_as_one():
    """CHANNEL_ERRORS is what routes the message to the channel field instead of the
    top of the form. A new refusal that is not in it renders in the wrong place."""
    produced = {"channel_not_on_device", "channel_disabled", "channel_is_onvif"}

    assert produced == set(CHANNEL_ERRORS)


def test_the_channel_errors_all_have_translations():
    import json
    import pathlib

    path = (
        pathlib.Path(__file__).parents[2]
        / "custom_components"
        / "dahua"
        / "translations"
        / "en.json"
    )
    strings = json.loads(path.read_text(encoding="utf-8"))["config"]["error"]

    missing = set(CHANNEL_ERRORS) - set(strings)
    assert not missing, "no translation for %s" % sorted(missing)


# --- the ports and the channel are now validated on the form ----------------


def _validate(field, value):
    """Push one value through the add form's own schema.

    With the transport fields revealed, because that is the only form they appear
    on: they are hidden until something fails.
    """
    handler = DahuaFlowHandler()
    handler._errors = {}
    schema = handler._user_schema(reveal_transport=True)
    return schema(
        {
            "username": "u",
            "password": "p",
            "address": "1.2.3.4",
            "port": "80",
            "rtsp_port": "554",
            "channel": 0,
            **{field: value},
        }
    )[field]


@pytest.mark.parametrize("field", ["port", "rtsp_port"])
def test_a_port_with_a_trailing_space_is_cleaned(field):
    """It used to survive validation and produce "http://ip: 80", a yarl InvalidURL,
    and the message "the log has the reason"."""
    assert _validate(field, "80 ") == "80"


@pytest.mark.parametrize("field", ["port", "rtsp_port"])
def test_a_port_that_is_not_a_number_is_refused(field):
    """The most likely typo on the form, and it used to reach the client and raise a
    ValueError that matched no branch in describe_setup_failure."""
    with pytest.raises(vol.Invalid):
        _validate(field, "eighty")


@pytest.mark.parametrize("field", ["port", "rtsp_port"])
@pytest.mark.parametrize("value", [0, 70000, -1])
def test_a_port_outside_the_range_is_refused(field, value):
    with pytest.raises(vol.Invalid):
        _validate(field, value)


@pytest.mark.parametrize("field", ["port", "rtsp_port"])
def test_a_valid_port_is_still_stored_as_a_string(field):
    """Every existing entry holds a string. Storing an int here would leave two
    shapes mixed across entries for no gain."""
    assert _validate(field, 8443) == "8443"
    assert isinstance(_validate(field, "8443"), str)


def test_a_negative_channel_is_refused():
    with pytest.raises(vol.Invalid):
        _validate("channel", -1)


def test_channel_zero_is_still_fine():
    assert _validate("channel", 0) == 0


# --- the flow has to actually call it ----------------------------------------
#
# Every test above exercises async_channel_refusal directly, so all of them pass
# even if _test_credentials never calls it. A mutation that disabled the call
# survived until this was added.


class _Session:
    def __init__(self, *args, **kwargs):
        pass

    async def close(self):
        pass


def _flow_with(monkeypatch, table=None, error=None):
    """A handler whose device answers identity, and RemoteDevice from `table`."""

    class _Device:
        def __init__(self, *args, **kwargs):
            self.identity_derived_from_credentials = False

        async def get_machine_name(self):
            return {"name": "FrontDoor"}

        async def async_get_system_info(self, strict_auth=False):
            return {"serialNumber": "SER123"}

        async def async_get_remote_devices(self):
            if error is not None:
                raise error
            return table

    monkeypatch.setattr(config_flow, "DahuaClient", _Device)
    monkeypatch.setattr(config_flow, "ClientSession", _Session)
    monkeypatch.setattr(config_flow, "TCPConnector", lambda **kwargs: None)
    return DahuaFlowHandler()


async def test_the_add_form_asks_for_the_channel_to_be_checked(monkeypatch):
    handler = _flow_with(monkeypatch, TABLE)

    data, error = await handler._test_credentials(
        "u", "p", "1.2.3.4", "80", "554", 16, None, check_channel=True
    )

    assert data is None
    assert error == "channel_not_on_device"


async def test_a_good_channel_still_gets_through(monkeypatch):
    handler = _flow_with(monkeypatch, TABLE)

    data, error = await handler._test_credentials(
        "u", "p", "1.2.3.4", "80", "554", 3, None, check_channel=True
    )

    assert error is None
    assert data["name"] == "FrontDoor"


async def test_the_import_step_does_not_recheck(monkeypatch):
    """It adds channels that came out of this very table a moment earlier, so
    re-reading it once per channel would be sixteen pointless requests."""
    handler = _flow_with(monkeypatch, TABLE)

    data, error = await handler._test_credentials(
        "u", "p", "1.2.3.4", "80", "554", 16, None
    )

    assert error is None, "check_channel defaults off for the import path"
    assert data is not None


# --- and the form only asks what it has to -----------------------------------
#
# Eight fields, including a 42 option multi-select of Dahua event codes and two
# ports, before the integration would talk to anything. Most of that the
# integration can work out, and #794 now names the cases where it cannot.


def _keys(reveal=False):
    return [
        str(marker.schema) for marker in DahuaFlowHandler()._user_schema(reveal).schema
    ]


def test_the_form_asks_four_questions():
    assert _keys() == ["username", "password", "address", "channel"]


def test_the_transport_details_are_not_asked_up_front():
    """80 and 554 are right almost always, and HTTPS follows from the port."""
    hidden = {"port", "rtsp_port", "use_https"}

    assert hidden.isdisjoint(_keys())


def test_a_failure_reveals_them():
    revealed = _keys(reveal=True)

    assert "port" in revealed
    assert "rtsp_port" in revealed
    assert "use_https" in revealed


def test_revealing_keeps_the_four_it_already_asked():
    """The user must not have to retype what they already gave."""
    for field in ("username", "password", "address", "channel"):
        assert field in _keys(reveal=True)


def test_the_events_list_is_not_on_the_add_form_at_all():
    """Not needed to connect, so the Bronze config-flow rule puts it in options,
    and it silently decided how many entities the device would get."""
    assert "events" not in _keys()
    assert "events" not in _keys(reveal=True)


def test_an_entry_created_without_events_still_gets_the_defaults():
    """Which is what makes removing the field safe."""
    from types import SimpleNamespace

    from custom_components.dahua import get_configured_events
    from custom_components.dahua.const import DEFAULT_EVENTS

    entry = SimpleNamespace(data={"address": "1.2.3.4"}, options={})

    assert get_configured_events(entry) == list(DEFAULT_EVENTS)


def test_a_refused_login_does_not_reveal_the_ports():
    """The device answered, so the transport is fine and the ports are a
    distraction from the actual problem."""
    handler = DahuaFlowHandler()
    handler._errors = {"base": "auth"}

    assert (
        any(reason not in TRANSPORT_WORKED for reason in handler._errors.values())
        is False
    )


def test_a_channel_refusal_does_not_reveal_the_ports_either():
    handler = DahuaFlowHandler()
    handler._errors = {"channel": "channel_not_on_device"}

    assert (
        any(reason not in TRANSPORT_WORKED for reason in handler._errors.values())
        is False
    )


def test_a_connection_failure_does_reveal_them():
    handler = DahuaFlowHandler()
    handler._errors = {"base": "cannot_connect"}

    assert (
        any(reason not in TRANSPORT_WORKED for reason in handler._errors.values())
        is True
    )


# --- the values the form stopped asking for still have to arrive --------------
#
# Hiding the ports is only safe if something supplies them. Testing the schema
# alone leaves that unproven: mutations that dropped the fill, and that ignored a
# discovery's port, both survived until these were added.


async def _port_seen_by(monkeypatch, discovered=None, given=None):
    """Drive the add form and report the transport it handed onwards."""
    seen = {}
    handler = _flow_with(monkeypatch, TABLE)
    handler._discovered = dict(discovered or {})

    async def _capture(
        username,
        password,
        address,
        port,
        rtsp_port,
        channel,
        use_https=None,
        check_channel=False,
    ):
        seen.update(port=port, rtsp_port=rtsp_port, use_https=use_https)
        return None, "auth"  # stay on the form; only the arguments matter

    handler._test_credentials = _capture
    submitted = {"username": "u", "password": "p", "address": "1.2.3.4", "channel": 0}
    submitted.update(given or {})
    await handler.async_step_user(submitted)
    return seen


async def test_the_ordinary_ports_are_supplied_without_being_asked(monkeypatch):
    seen = await _port_seen_by(monkeypatch)

    assert seen["port"] == "80"
    assert seen["rtsp_port"] == "554"


async def test_a_discovered_port_is_used_instead_of_the_default(monkeypatch):
    """The whole reason #798 reads HttpPort off the device."""
    seen = await _port_seen_by(monkeypatch, discovered={"port": "8000"})

    assert seen["port"] == "8000"


async def test_what_the_user_typed_beats_both(monkeypatch):
    """Once a failure has revealed the field, their answer is the one that counts."""
    seen = await _port_seen_by(
        monkeypatch, discovered={"port": "8000"}, given={"port": "8443"}
    )

    assert seen["port"] == "8443"


async def test_an_unticked_https_box_still_means_derive_it(monkeypatch):
    """Absent is not False: DahuaClient reads None as "work it out from the port"."""
    seen = await _port_seen_by(monkeypatch)

    assert seen["use_https"] is None
