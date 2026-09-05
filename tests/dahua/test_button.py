"""button.py shipped as an empty stub with a TODO about HomeAssistant 2021.12.

Closes #552, which benchmarks this integration against the HA-certified Reolink
one. Two buttons, not the three that issue asks for -- see the PR for why
Cancel Call is deliberately left out.
"""

import pytest

from custom_components.dahua import button as button_module
from custom_components.dahua.button import DahuaOpenDoorButton, DahuaRebootButton
from custom_components.dahua.const import BUTTON, PLATFORMS


class _Client:
    def __init__(self):
        self.calls = []

    def _record(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name,) + args)
        return call

    def __getattr__(self, name):
        return self._record(name)


class _Coordinator:
    def __init__(self, doorbell=False, channel=0):
        self.client = _Client()
        self._doorbell = doorbell
        self._channel = channel
        self.refreshed = 0

    def is_doorbell(self):
        return self._doorbell

    def get_serial_number(self):
        return "SERIAL1" if self._channel == 0 else "SERIAL1_%d" % self._channel

    def get_device_name(self):
        return "Front Door"

    def get_address(self):
        return "10.0.0.1"

    async def async_refresh(self):
        self.refreshed += 1


@pytest.fixture(autouse=True)
def _skip_ha_plumbing(monkeypatch):
    monkeypatch.setattr(button_module.DahuaBaseEntity, "__init__", lambda self, c, e: None)


def _button(cls, coordinator=None):
    entity = object.__new__(cls)
    entity._coordinator = coordinator or _Coordinator()
    return entity


# --- the platform actually loads now ---------------------------------------

def test_the_button_platform_is_registered():
    """button.py existed on disk but was never in PLATFORMS, so it never ran."""
    assert BUTTON in PLATFORMS


async def test_the_setup_adds_a_reboot_button_for_a_camera():
    added = []
    coordinator = _Coordinator(doorbell=False)
    hass = type("H", (), {"data": {"dahua": {"e1": coordinator}}})()
    entry = type("E", (), {"entry_id": "e1"})()

    await button_module.async_setup_entry(hass, entry, added.extend)

    assert [type(b).__name__ for b in added] == ["DahuaRebootButton"]


async def test_a_doorbell_also_gets_an_open_door_button():
    added = []
    coordinator = _Coordinator(doorbell=True)
    hass = type("H", (), {"data": {"dahua": {"e1": coordinator}}})()
    entry = type("E", (), {"entry_id": "e1"})()

    await button_module.async_setup_entry(hass, entry, added.extend)

    assert [type(b).__name__ for b in added] == [
        "DahuaRebootButton", "DahuaOpenDoorButton",
    ]


async def test_a_camera_gets_no_open_door_button():
    """The endpoint is a VTO operation. A button that always errors is worse
    than no button."""
    added = []
    hass = type("H", (), {"data": {"dahua": {"e1": _Coordinator(doorbell=False)}}})()
    entry = type("E", (), {"entry_id": "e1"})()

    await button_module.async_setup_entry(hass, entry, added.extend)

    assert not any(isinstance(b, DahuaOpenDoorButton) for b in added)


# --- what each button actually calls ---------------------------------------

async def test_reboot_calls_reboot():
    b = _button(DahuaRebootButton)

    await b.async_press()

    assert b._coordinator.client.calls == [("reboot",)]


async def test_reboot_does_not_poll_the_device_afterwards():
    """It is on its way down; a refresh would turn a good press into an error."""
    b = _button(DahuaRebootButton)

    await b.async_press()

    assert b._coordinator.refreshed == 0


async def test_open_door_names_the_door():
    b = _button(DahuaOpenDoorButton, _Coordinator(doorbell=True))

    await b.async_press()

    assert b._coordinator.client.calls == [("async_access_control_open_door", 1)]


# --- identity ---------------------------------------------------------------

def test_the_unique_ids():
    assert _button(DahuaRebootButton).unique_id == "SERIAL1_reboot"
    assert _button(DahuaOpenDoorButton).unique_id == "SERIAL1_open_door"


def test_the_ids_follow_the_channel_like_every_other_entity():
    c = _Coordinator(channel=4)
    assert _button(DahuaRebootButton, c).unique_id == "SERIAL1_4_reboot"
    assert _button(DahuaOpenDoorButton, c).unique_id == "SERIAL1_4_open_door"


def test_the_two_buttons_do_not_collide():
    c = _Coordinator(doorbell=True)
    assert _button(DahuaRebootButton, c).unique_id != _button(DahuaOpenDoorButton, c).unique_id


def test_the_names():
    assert _button(DahuaRebootButton).name == "Front Door Reboot"
    assert _button(DahuaOpenDoorButton).name == "Front Door Open Door"


def test_reboot_is_a_restart_button_under_config():
    """So it lands in the device page controls, not among the sensors.

    Read off an instance, not the class: Home Assistant rewrites class-level
    _attr_ assignments into cached properties, so the class attribute is a
    descriptor rather than the value.
    """
    from homeassistant.components.button import ButtonDeviceClass
    from homeassistant.const import EntityCategory

    b = _button(DahuaRebootButton)

    assert b.device_class is ButtonDeviceClass.RESTART
    assert b.entity_category is EntityCategory.CONFIG


def test_open_door_is_not_a_restart_button():
    assert _button(DahuaOpenDoorButton).device_class is None


# --- the option toggle ------------------------------------------------------

def test_the_new_platform_has_a_label_on_the_options_screen():
    """The screen is built from sorted(PLATFORMS); a platform with no
    translation shows as a bare key."""
    import json
    from pathlib import Path

    path = Path(button_module.__file__).parent / "translations" / "en.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    labels = data["options"]["step"]["user"]["data"]

    for platform in PLATFORMS:
        assert platform in labels, "%s has no label on the options screen" % platform
