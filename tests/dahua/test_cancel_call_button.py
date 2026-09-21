"""Cancelling a call says whether it worked, and has a button.

#716 asks for the button. #552 asked for it too and #623 left it out on
purpose, for a reason that was true at the time:

    async def cancel_call(self):
        def cancel(message):
            _LOGGER.info(f"Got cancel call response: {message}")
        self.send("console.runCmd", cancel, {"command": "hc"})
        return True

Declared async, awaits nothing, returns True unconditionally. A button on that
would have gone green on every press whatever the doorbell did. #623 named two
prerequisites: a HomeAssistantError instead of an AttributeError when there is
no client, which landed already, and a real success signal, which is this.

That second one matters beyond the button. #526, cancel_call no longer working
on the VTO2211G-WP, is the most commented open issue here, and "it stopped
working" was all anyone could report because the doorbell's own answer was
logged at info where nothing read it.
"""
import asyncio

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import button as button_module
from custom_components.dahua.button import (
    DahuaCancelCallButton,
    DahuaOpenDoorButton,
    DahuaRebootButton,
    async_setup_entry,
)
from custom_components.dahua.vto import CancelCallRefused, DahuaVTOClient


class _Transport:
    def __init__(self):
        self.written = []

    def is_closing(self):
        return False

    def write(self, message):
        self.written.append(message)


def _protocol():
    """The real client, with just the pieces cancel_call touches."""
    p = object.__new__(DahuaVTOClient)
    p.host = "10.0.0.7"
    p.request_id = 1
    p.sessionId = 0
    p.data_handlers = {}
    p.transport = _Transport()
    p._loop = asyncio.get_running_loop()
    return p


async def _reply_with(p, message):
    """Answer whatever request cancel_call has just registered."""
    for _ in range(50):
        if p.data_handlers:
            break
        await asyncio.sleep(0)
    request_id = next(iter(p.data_handlers))
    p.data_handlers[request_id](message)
    return request_id


# --- the call now reports what happened -------------------------------------

async def test_a_doorbell_that_agrees_reports_success():
    p = _protocol()
    pending = asyncio.ensure_future(p.cancel_call(timeout=2))

    await _reply_with(p, {"id": 2, "result": True})

    assert await pending is True


async def test_a_doorbell_that_refuses_raises():
    """This is what #526 could never say."""
    p = _protocol()
    pending = asyncio.ensure_future(p.cancel_call(timeout=2))

    await _reply_with(p, {"id": 2, "result": False, "error": {"message": "nope"}})

    with pytest.raises(CancelCallRefused) as refused:
        await pending

    assert "10.0.0.7" in str(refused.value), "the message does not name the device"


async def test_silence_raises_rather_than_reporting_success():
    """The whole pre-fix behaviour: no answer, and True anyway."""
    p = _protocol()

    with pytest.raises(CancelCallRefused):
        await p.cancel_call(timeout=0.05)


async def test_a_reply_without_a_result_is_taken_as_agreement():
    """No captured console.runCmd reply to be stricter from.

    The doorbell answered, which is more than the failing case does.
    """
    p = _protocol()
    pending = asyncio.ensure_future(p.cancel_call(timeout=2))

    await _reply_with(p, {"id": 2, "params": {}})

    assert await pending is True


async def test_the_handler_does_not_leak():
    """send() registers one per request and only keep-alive ever removes one."""
    p = _protocol()
    pending = asyncio.ensure_future(p.cancel_call(timeout=2))
    await _reply_with(p, {"id": 2, "result": True})
    await pending

    assert p.data_handlers == {}, "the reply handler was left behind"


async def test_the_handler_does_not_leak_on_timeout_either():
    p = _protocol()

    with pytest.raises(CancelCallRefused):
        await p.cancel_call(timeout=0.05)

    assert p.data_handlers == {}


# --- the button -------------------------------------------------------------

class _Coordinator:
    def __init__(self, doorbell=True, vto_client=None):
        self._doorbell = doorbell
        self._vto = vto_client

    def is_doorbell(self):
        return self._doorbell

    def get_vto_client(self):
        return self._vto

    def get_device_name(self):
        return "Front Door"

    def get_serial_number(self):
        return "SER1"

    def get_address(self):
        return "10.0.0.7"


@pytest.fixture(autouse=True)
def _skip_ha_plumbing(monkeypatch):
    monkeypatch.setattr(button_module.DahuaBaseEntity, "__init__",
                        lambda self, c, e: None)


def _button(coordinator):
    b = object.__new__(DahuaCancelCallButton)
    b._coordinator = coordinator
    return b


async def test_only_a_doorbell_gets_one():
    added = []
    hass = type("H", (), {"data": {"dahua": {"e1": _Coordinator(doorbell=True)}}})()
    await async_setup_entry(hass, type("E", (), {"entry_id": "e1"})(), added.extend)

    assert any(isinstance(b, DahuaCancelCallButton) for b in added)


async def test_a_camera_does_not():
    added = []
    hass = type("H", (), {"data": {"dahua": {"e1": _Coordinator(doorbell=False)}}})()
    await async_setup_entry(hass, type("E", (), {"entry_id": "e1"})(), added.extend)

    assert not any(isinstance(b, DahuaCancelCallButton) for b in added)
    assert any(isinstance(b, DahuaRebootButton) for b in added)


async def test_no_connection_says_so_instead_of_raising_attributeerror():
    with pytest.raises(HomeAssistantError) as err:
        await _button(_Coordinator(vto_client=None)).async_press()

    assert "Front Door" in str(err.value)


async def test_a_refusal_reaches_the_user():
    class _Refuses:
        async def cancel_call(self):
            raise CancelCallRefused("10.0.0.7 refused the hang-up")

    with pytest.raises(HomeAssistantError) as err:
        await _button(_Coordinator(vto_client=_Refuses())).async_press()

    assert "refused" in str(err.value)


async def test_a_successful_press_is_quiet():
    pressed = []

    class _Agrees:
        async def cancel_call(self):
            pressed.append(True)
            return True

    await _button(_Coordinator(vto_client=_Agrees())).async_press()

    assert pressed == [True]


def test_its_unique_id_does_not_collide():
    """Compare two instances.

    `DahuaOpenDoorButton.unique_id` read off the class is a property object, so
    a string is never equal to it and the assertion would pass no matter what.
    Same trap as the device class in #726.
    """
    other = object.__new__(DahuaOpenDoorButton)
    other._coordinator = _Coordinator()

    assert _button(_Coordinator()).unique_id == "SER1_cancel_call"
    assert _button(_Coordinator()).unique_id != other.unique_id
