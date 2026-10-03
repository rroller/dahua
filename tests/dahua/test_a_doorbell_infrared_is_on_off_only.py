"""The infrared light is on/off on a device with no brightness bank (#966).

A doorbell's Lighting row is Mode only, so get_infrared_bank reports None (#963) and
there is nothing to dim. Advertising ColorMode.BRIGHTNESS there draws a slider that
drives nothing. The entity now advertises ONOFF, and reports no brightness, when the
device has no bank -- and is unchanged on a camera that does.
"""

from unittest.mock import Mock

from homeassistant.components.light import ColorMode

from custom_components.dahua.light import DahuaInfraredLight


class _Coordinator:
    def __init__(self, bank, brightness=200):
        self._bank = bank
        self._brightness = brightness

    def get_infrared_bank(self):
        return self._bank

    def get_infrared_brightness(self):
        return self._brightness


def _light(bank, brightness=200):
    entity = object.__new__(DahuaInfraredLight)
    entity._coordinator = _Coordinator(bank, brightness)
    return entity


# --- a doorbell: no bank ------------------------------------------------------

def test_no_bank_is_on_off():
    assert _light(None).color_mode == ColorMode.ONOFF


def test_no_bank_supports_only_on_off():
    assert _light(None).supported_color_modes == {ColorMode.ONOFF}


def test_no_bank_reports_no_brightness():
    """So Home Assistant draws no slider rather than a dead one."""
    assert _light(None, brightness=200).brightness is None


# --- a camera: a real bank, unchanged ----------------------------------------

def test_a_bank_is_dimmable():
    assert _light("MiddleLight").color_mode == ColorMode.BRIGHTNESS


def test_a_bank_supports_brightness():
    assert _light("MiddleLight").supported_color_modes == {ColorMode.BRIGHTNESS}


def test_a_bank_reports_its_brightness():
    assert _light("NearLight", brightness=137).brightness == 137


# --- the two agree ------------------------------------------------------------

def test_the_only_mode_is_the_supported_one():
    """supported_color_modes is built from color_mode, so a device cannot end up
    advertising a mode it does not report."""
    for bank in (None, "MiddleLight", "FarLight"):
        light = _light(bank)
        assert light.supported_color_modes == {light.color_mode}
