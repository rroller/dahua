"""The illuminator must drive the light the device calls white.

Lighting_V2 lists a device's lights by index, and the order is not the same on
every model. The index was hardcoded to 0, which is right on most cameras and
wrong on some: the HFW3449E-S-IL in #647 reports index 0 as InfraredLight and
the white light at 1. Writing to 0 there is accepted, stores, and changes a
light nobody can see -- which is exactly what that reporter measured, with the
WhiteLight entry sitting untouched the whole time.

The device names each light in LightType, so this does not have to be guessed.
It is only guessed when the device declines to say.
"""

from custom_components.dahua import illuminator_light_index


def _table(channel, profile, types):
    """Lighting_V2 rows as the flat keys the coordinator stores."""
    return {
        "table.Lighting_V2[{0}][{1}][{2}].LightType".format(channel, profile, i): t
        for i, t in types.items()
    }


# --- the model this exists for ------------------------------------------------

def test_white_light_at_one_is_found_when_zero_is_infrared():
    """#647: index 0 is the invisible emitter, so the illuminator must not use it."""
    data = _table(0, 0, {0: "InfraredLight", 1: "WhiteLight"})
    assert illuminator_light_index(data, 0, 0) == 1


def test_it_reads_the_row_for_this_channel_and_profile():
    """An NVR's channels each have their own row, and profiles differ too."""
    data = _table(3, 1, {0: "InfraredLight", 1: "WhiteLight"})
    assert illuminator_light_index(data, 3, 1) == 1


# --- and the models that must not change --------------------------------------

def test_white_light_at_zero_stays_at_zero():
    data = _table(0, 0, {0: "WhiteLight", 1: "InfraredLight"})
    assert illuminator_light_index(data, 0, 0) == 0


def test_a_device_that_says_nothing_keeps_the_old_behaviour():
    """No LightType at all is the common case on older cameras."""
    assert illuminator_light_index({}, 0, 0) == 0


def test_another_channels_lights_are_not_borrowed():
    """Reading the wrong row is the bug #638 fixed elsewhere; don't reintroduce it."""
    data = _table(0, 0, {0: "InfraredLight", 1: "WhiteLight"})
    assert illuminator_light_index(data, 5, 0) == 0


def test_zero_is_not_white_and_nothing_else_claims_to_be():
    """Moving on that basis would be a guess; the old behaviour is a better one."""
    data = _table(0, 0, {0: "InfraredLight", 1: "AIMixLight"})
    assert illuminator_light_index(data, 0, 0) == 0


def test_a_white_light_further_down_is_still_found():
    data = _table(0, 0, {0: "InfraredLight", 1: "AIMixLight", 2: "WhiteLight"})
    assert illuminator_light_index(data, 0, 0) == 2
