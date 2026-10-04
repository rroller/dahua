"""The three selects: what they report, and what they write.

test_day_night_select.py covers the coordinator's reading of the day/night value and
test_preset_list.py covers enumerating presets. The entities in between had nothing, which
is most of what select.py was missing.

A select is the one entity type that can lie quietly. A switch is on or off and a wrong
answer is obvious; a select reports one of several named options, and reporting the wrong
name looks exactly like the camera being in that mode.
"""

import pytest

from custom_components.dahua import select as select_module
from custom_components.dahua.select import (
    DahuaCameraPresetPositionSelect,
    DahuaDayNightModeSelect,
    DahuaDoorbellLightSelect,
)

from . import adds_entities


class _Client:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name,) + args)

        return call


class _Coordinator:
    # The platforms file each channel's entities under its own subentry, so they
    # read this on every entity they add. None is a single camera, and is what
    # `async_add_entities` wants for an entry that has no subentries.
    subentry_id = None

    def __init__(self, data=None, channel=0, day_night=None):
        self.client = _Client()
        self.data = data or {}
        self._channel = channel
        self._day_night = day_night
        self.refreshed = 0

    def get_channel(self):
        return self._channel

    def get_serial_number(self):
        return "SERIAL1"

    def get_day_night_color(self):
        return self._day_night

    async def async_refresh(self):
        self.refreshed += 1


def _select(cls, coordinator, **attrs):
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    for key, value in attrs.items():
        setattr(entity, key, value)
    return entity


# --- the doorbell light, which has three states rather than two ---------------
#
# Off, On and Strobe, read out of a Mode and a State together. ForceOn on its own does not
# say which of the two it is, so reading only the Mode would report a steady light for a
# strobing one.

LIGHT = "table.Lighting_V2[0][0][1]."


def _light(mode=None, state=None):
    data = {}
    if mode is not None:
        data[LIGHT + "Mode"] = mode
    if state is not None:
        data[LIGHT + "State"] = state
    return _select(DahuaDoorbellLightSelect, _Coordinator(data))


def test_a_steady_light_reads_as_on():
    assert _light(mode="ForceOn", state="On").current_option == "On"


def test_a_flickering_light_reads_as_strobe():
    """The State is the only thing that separates this from On."""
    assert _light(mode="ForceOn", state="Flicker").current_option == "Strobe"


def test_forceon_with_any_other_state_is_off():
    """Not guessed at. A state this does not recognise is not evidence the light is on."""
    assert _light(mode="ForceOn", state="Off").current_option == "Off"
    assert _light(mode="ForceOn", state="Something").current_option == "Off"


def test_a_light_the_camera_is_not_forcing_is_off():
    """Auto is the camera deciding, and the select has no option for that, so it reports
    Off rather than claiming a mode it cannot express."""
    assert _light(mode="Auto", state="On").current_option == "Off"


def test_a_camera_that_has_reported_nothing_yet_is_off():
    """A poll that has not landed must not read as a light that is on."""
    assert _light().current_option == "Off"


async def test_choosing_a_light_option_writes_it_and_reads_back():
    c = _Coordinator()
    s = _select(DahuaDoorbellLightSelect, c)

    await s.async_select_option("Strobe")

    assert c.client.calls == [("async_set_lighting_v2_for_amcrest_doorbells", "Strobe")]
    assert c.refreshed == 1


# --- the preset position ------------------------------------------------------


async def test_a_firmware_with_no_readback_does_not_claim_a_position():
    """The SDT4E425 has no supported CGI position readback. Reporting the last preset
    asked for would show a position the camera may have been driven away from since,
    which is worse than admitting to not knowing."""
    s = _select(
        DahuaCameraPresetPositionSelect,
        _Coordinator({"status.PresetID": "3"}),
        _rpc2_channel=1,
        _attr_options=["Manual", "1", "2"],
    )

    assert s.current_option == "Manual"


def test_a_camera_at_no_preset_reads_as_manual():
    s = _select(
        DahuaCameraPresetPositionSelect,
        _Coordinator({"status.PresetID": "0"}),
        _rpc2_channel=None,
        _attr_options=["Manual", "1"],
    )

    assert s.current_option == "Manual"


def test_a_camera_sitting_at_a_preset_reports_it():
    s = _select(
        DahuaCameraPresetPositionSelect,
        _Coordinator({"status.PresetID": "3"}),
        _rpc2_channel=None,
        _attr_options=["Manual", "3"],
    )

    assert s.current_option == "3"


def test_a_camera_that_has_not_reported_a_preset_reads_as_manual():
    s = _select(
        DahuaCameraPresetPositionSelect,
        _Coordinator(),
        _rpc2_channel=None,
        _attr_options=["Manual"],
    )

    assert s.current_option == "Manual"


async def test_choosing_manual_moves_nothing():
    """Manual is what the select reports when the camera is not at a preset, so it has to
    be selectable without meaning "go somewhere". There is no position to go to."""
    c = _Coordinator()
    s = _select(
        DahuaCameraPresetPositionSelect,
        c,
        _rpc2_channel=None,
        _attr_options=["Manual", "1"],
    )

    await s.async_select_option("Manual")

    assert c.client.calls == []


async def test_choosing_a_preset_on_the_rpc2_firmware_uses_rpc2():
    """And passes the RPC2 channel rather than the logical one, which is the whole reason
    that attribute exists."""
    c = _Coordinator()
    s = _select(
        DahuaCameraPresetPositionSelect,
        c,
        _rpc2_channel=1,
        _attr_options=["Manual", "4"],
    )

    await s.async_select_option("4")

    assert c.client.calls == [("async_goto_preset_rpc2", 1, 4)]


# --- the day/night mode -------------------------------------------------------


def test_the_mode_is_whatever_the_device_reported():
    s = _select(
        DahuaDayNightModeSelect,
        _Coordinator(day_night="BlackWhite"),
        _attr_options=["Color", "BlackWhite", "Auto"],
    )

    assert s.current_option == "BlackWhite"


def test_a_device_that_reported_nothing_has_no_mode():
    """None shows as unknown, which is what a poll that has not landed means. Picking one
    of the three would claim the camera is in a mode nobody has read."""
    s = _select(
        DahuaDayNightModeSelect,
        _Coordinator(day_night=None),
        _attr_options=["Color", "BlackWhite", "Auto"],
    )

    assert s.current_option is None


async def test_choosing_a_mode_writes_it_against_the_general_profile():
    c = _Coordinator(channel=2)
    s = _select(
        DahuaDayNightModeSelect, c, _attr_options=["Color", "BlackWhite", "Auto"]
    )

    await s.async_select_option("Color")

    assert c.client.calls == [
        ("async_set_video_in_day_night_mode", 2, "general", "Color")
    ]
    assert c.refreshed == 1


async def test_an_option_the_select_does_not_offer_is_ignored():
    """A service call can name anything. Passing it through would write a mode the device
    does not accept and then refresh as though something had changed."""
    c = _Coordinator()
    s = _select(
        DahuaDayNightModeSelect, c, _attr_options=["Color", "BlackWhite", "Auto"]
    )

    await s.async_select_option("Sepia")

    assert c.client.calls == []
    assert c.refreshed == 0


# --- which selects an entry creates ------------------------------------------


async def _added_for(coordinator, cgi_presets=None):
    """Run the real setup with the entities recorded.

    `_async_preset_ids` is replaced as well, because the branch for an ordinary camera
    goes through it and its own behaviour -- an empty list meaning "no presets" and None
    meaning "the camera would not say" -- is covered by test_preset_list.py. Leaving it
    live here would make these tests depend on that instead of on what setup decides.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    async def _preset_ids(_coordinator):
        return cgi_presets

    entry = SimpleNamespace(entry_id="e1", options={}, runtime_data={0: coordinator})
    added = []
    with patch.multiple(
        select_module,
        DahuaDoorbellLightSelect=lambda *a, **k: "light",
        DahuaCameraPresetPositionSelect=lambda *a, **k: ("preset", k.get("preset_ids")),
        DahuaDayNightModeSelect=lambda *a, **k: "day_night",
        _async_preset_ids=_preset_ids,
    ):
        await select_module.async_setup_entry(
            SimpleNamespace(data={}), entry, adds_entities(added)
        )
    return added


class _SetupCoordinator(_Coordinator):
    def __init__(
        self,
        amcrest=False,
        security_light=False,
        model="IPC-HDW1234",
        presets=(1, 2),
        preset_error=None,
        day_night_supported=False,
        infrared_supported=False,
    ):
        super().__init__()
        self._day_night_supported = day_night_supported
        self._infrared_supported = infrared_supported
        self._amcrest = amcrest
        self._security_light = security_light
        self._model = model
        self._presets = list(presets)
        self._preset_error = preset_error

        async def _get_ptz_preset_ids(channel):
            if self._preset_error is not None:
                raise self._preset_error
            return self._presets

        self.client.async_get_ptz_preset_ids = _get_ptz_preset_ids

    def is_amcrest_doorbell(self):
        return self._amcrest

    def supports_security_light(self):
        return self._security_light

    def get_model(self):
        return self._model

    def get_channel_number(self):
        return 1

    def supports_day_night_color(self):
        return self._day_night_supported

    def supports_infrared_light(self):
        return self._infrared_supported

    def is_indoor_monitor(self):
        # A camera. The indoor monitor's camera-link selects are in
        # test_vth_camera_link.py.
        return False

    def is_indoor_monitor_without_video(self):
        # A camera; the indoor monitor is in test_preset_list.py.
        return False

    def reported_device_class(self):
        # What a camera answers; the VTO is in test_preset_list.py.
        return "IPC"


async def test_the_doorbell_light_select_needs_a_doorbell_with_one():
    assert "light" not in await _added_for(_SetupCoordinator())
    assert "light" not in await _added_for(_SetupCoordinator(amcrest=True))
    assert "light" not in await _added_for(_SetupCoordinator(security_light=True))
    assert "light" in await _added_for(
        _SetupCoordinator(amcrest=True, security_light=True)
    )


async def test_a_device_that_refuses_to_list_its_presets_still_gets_a_select():
    """The enumeration is over RPC2 and can fail. Losing the entity with it would leave a
    PTZ camera with no preset control at all, where an empty list still offers Manual and
    can be corrected by a reload."""
    added = await _added_for(
        _SetupCoordinator(
            model="DH-SDT4E425-4F-GB-A-PV1", preset_error=RuntimeError("refused")
        )
    )

    assert ("preset", []) in added


async def test_presets_that_were_listed_reach_the_select():
    added = await _added_for(
        _SetupCoordinator(model="DH-SDT4E425-4F-GB-A-PV1", presets=[1, 4])
    )

    assert ("preset", [1, 4]) in added


async def test_the_day_night_select_needs_a_device_that_reports_the_mode():
    """Reading it back is the point of the entity, so a device that does not report it
    would leave a control showing unknown for ever."""
    assert "day_night" not in await _added_for(_SetupCoordinator())
    assert "day_night" in await _added_for(_SetupCoordinator(day_night_supported=True))


async def test_a_camera_that_holds_no_presets_gets_no_preset_control():
    """#525: four reporters, four cameras, one 400 from a dropdown that was never going
    to work, and two of those cameras have no PTZ motor at all. An empty list is the
    camera answering that it holds nothing."""
    added = await _added_for(_SetupCoordinator(), cgi_presets=[])

    assert not any(isinstance(item, tuple) for item in added)


async def test_a_camera_that_would_not_say_keeps_its_control():
    """None is the device declining to answer, which is not the same as holding none.
    The SDT4E425 is that shape: its CGI getStatus answers 400 while its PTZ works, so
    taking the control away would lose a working feature."""
    added = await _added_for(_SetupCoordinator(), cgi_presets=None)

    assert ("preset", None) in added
