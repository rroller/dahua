"""An Illuminator entity on a camera that has no white light.

#540, an IPC-HDW2831T-AS-S2. It has no visible white emitter at all: its single
light is infrared. The check was only that *a* Lighting_V2 row existed:

    return not (...) and "table.Lighting_V2[0][0][0].Mode" in self.data

so the camera got an Illuminator entity, `illuminator_light_index` fell back to
0 because nothing named a white light, and the entity drove the night vision
LED. The camera's own interface calls that control Illuminator too, which is
how it went unnoticed.

The same check already existed in this function for one model, behind a name
prefix. This applies it to whatever the device says, which is the fourth time a
capability here was gated on a model string when the device was willing to
answer (#570, #676, #690).

A device that names no LightType keeps the behaviour it has always had. Only a
device that lists its emitters and names no white one loses the entity, which is
the only case where we know it was wrong.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator(data, model="IPC-HDW2831T-AS-S2", channel=0):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.data = data
    c._channel = channel
    c.model = model
    c._supports_lighting_scheme_illuminator = False
    return c


def _lighting(*types, channel=0, mode="Off", bank="NearLight"):
    """A Lighting_V2 row, optionally naming each emitter.

    Each named emitter also gets a brightness bank, because a real one has
    one: that is what #1094 measured, and what `supports_illuminator` now
    requires of a white row. `bank=None` builds the bankless shape a camera
    reports for an emitter it does not have.
    """
    data = {"table.Lighting_V2[{0}][0][0].Mode".format(channel): mode}
    for index, light in enumerate(types):
        if light is None:
            continue
        data["table.Lighting_V2[{0}][0][{1}].LightType".format(channel, index)] = light
        if bank is not None:
            data[
                "table.Lighting_V2[{0}][0][{1}].{2}[0].Light".format(
                    channel, index, bank
                )
            ] = "50"
    return data


# --- the #540 case ----------------------------------------------------------


def test_a_camera_whose_only_light_is_infrared_gets_no_illuminator():
    coordinator = _coordinator(_lighting("InfraredLight"))

    assert coordinator.supports_illuminator() is False


def test_two_emitters_and_neither_is_white_still_gets_none():
    coordinator = _coordinator(_lighting("InfraredLight", "AIMixLight"))

    assert coordinator.supports_illuminator() is False


# --- what must keep working -------------------------------------------------


def test_a_camera_that_names_a_white_light_keeps_it():
    coordinator = _coordinator(_lighting("WhiteLight"))

    assert coordinator.supports_illuminator() is True


def test_white_at_a_later_index_counts():
    """Index 0 is the infrared emitter on dual light models (#652)."""
    coordinator = _coordinator(_lighting("InfraredLight", "WhiteLight"))

    assert coordinator.supports_illuminator() is True


def test_a_camera_that_names_nothing_keeps_the_old_behaviour():
    """Older firmware reports no LightType.

    Withdrawing the entity on silence would take the light away from everyone
    who has one working, to fix the few we know are wrong.
    """
    coordinator = _coordinator({"table.Lighting_V2[0][0][0].Mode": "Off"})

    assert coordinator.supports_illuminator() is True


def test_no_lighting_row_at_all_is_still_no_illuminator():
    coordinator = _coordinator({})

    assert coordinator.supports_illuminator() is False


# --- #1094: a white row that names no brightness bank -----------------------


def test_a_white_row_with_no_brightness_bank_gets_no_illuminator():
    """Measured on a DHI-NVR5464-16P-EI whose owner could say which channels
    physically have a white light. The one that does not reports a three field
    row, and turning the entity on makes the recorder refuse the write,
    because `illuminator_brightness_bank` composes a `MiddleLight` key the row
    has never had."""
    coordinator = _coordinator(_lighting("WhiteLight", bank=None))

    assert coordinator.supports_illuminator() is False


def test_the_bank_is_what_counts_not_any_brightness_field():
    """The phantom row carries `PercentOfMaxBrightness` as well, so reading a
    brightness-shaped field as evidence of an emitter is the same mistake one
    layer down."""
    data = _lighting("WhiteLight", bank=None)
    data["table.Lighting_V2[0][0][0].PercentOfMaxBrightness"] = "100"

    assert _coordinator(data).supports_illuminator() is False


@pytest.mark.parametrize("bank", ["MiddleLight", "NearLight", "FarLight"])
def test_any_of_the_three_banks_is_enough(bank):
    """The same three `illuminator_brightness_bank` chooses between, so the
    question asked here is the one the write will ask."""
    coordinator = _coordinator(_lighting("WhiteLight", bank=bank))

    assert coordinator.supports_illuminator() is True


def test_a_banked_infrared_row_does_not_lend_its_bank_to_a_bankless_white_one():
    """Channel 1 of the measured recorder exactly: a banked InfraredLight row
    beside a bankless WhiteLight one. The bank has to be on the white row."""
    data = _lighting("InfraredLight", bank="MiddleLight")
    data["table.Lighting_V2[0][0][1].LightType"] = "WhiteLight"

    assert _coordinator(data).supports_illuminator() is False


def test_the_measured_working_channel_keeps_its_illuminator():
    """Channel 11 of the same recorder, which does have a white light: a
    banked infrared row and a banked white one."""
    data = _lighting("InfraredLight", "WhiteLight", bank="NearLight")

    assert _coordinator(data).supports_illuminator() is True


# --- the existing exclusions are untouched ----------------------------------


def test_an_amcrest_doorbell_is_still_excluded():
    coordinator = _coordinator(_lighting("WhiteLight"), model="AD410")

    assert coordinator.supports_illuminator() is False


def test_the_channel_is_respected():
    """Channel 1 must not read channel 0's emitters."""
    data = _lighting("InfraredLight", channel=1)
    data.update(_lighting("WhiteLight", channel=0))

    assert _coordinator(data, channel=1).supports_illuminator() is False
    assert _coordinator(data, channel=0).supports_illuminator() is True
