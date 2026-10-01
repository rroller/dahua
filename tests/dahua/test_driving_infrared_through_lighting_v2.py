"""Where a channel has a Lighting_V2 row, the infrared emitter is driven through it.

The integration only ever wrote the v1 `Lighting` table for infrared. On a
DHI-NVR5464-16P-EI that is refused, and the v2 table is not. Measured, with every
write setting a field to the value it already held:

    Lighting_V2[11][0][0].Mode   (LightType=InfraredLight)   200 'OK'
    Lighting_V2[1][0][0].Mode    (LightType=InfraredLight)   200 'OK'
    Lighting[11][0].Mode                                     403 'Authority:check failure.'
    Lighting[1][0].Mode                                      403 'Authority:check failure.'

and then with a real change, which is the only thing that settles acceptance from
action:

    Lighting_V2[11][0][0] before:  LightType=InfraredLight  Mode=ZoomPrio
    write Mode=Manual  ->  HTTP 200 'OK'
    read back Mode = Manual
    restore Mode=ZoomPrio  ->  row identical to before

So that recorder's infrared **is** controllable, through the table nobody was
using. Only two of its fifteen channels have a v2 row; the other thirteen keep the
v1 path, where the refusal is real and #941 reports it.

Three details the device forced, each with a test here:

- **The index is found by `LightType`, not assumed.** Channel 11 reports index 0 as
  `InfraredLight` and index 1 as `WhiteLight`. Driving the wrong one would move the
  *white* light while the user watched an infrared toggle -- which is #647 in
  reverse, and `illuminator_light_index` exists because of it.
- **The bank comes from the row.** Channel 11's infrared carries `NearLight` and
  `FarLight`; channel 1's carries `MiddleLight`.
- **The mode is read from the same table it is written to.** Channel 11 reports
  `ZoomPrio` in v2; reading v1 and writing v2 would report a state the control is
  not setting.
"""

import pytest

from custom_components.dahua.coordinator import infrared_v2_row


def _v2(channel, profile, index, light_type, banks=("MiddleLight",), mode="Auto"):
    """The keys a device serves for one Lighting_V2 row."""
    base = "table.Lighting_V2[%d][%s][%d]." % (channel, profile, index)
    data = {base + "LightType": light_type, base + "Mode": mode}
    for bank in banks:
        data[base + bank + "[0].Light"] = "50"
    return data


# --- finding the row ------------------------------------------------------------

def test_the_infrared_row_is_found_by_what_the_device_calls_it():
    """Channel 11's real shape: infrared at 0, white at 1."""
    data = {}
    data.update(_v2(11, "0", 0, "InfraredLight", ("NearLight", "FarLight")))
    data.update(_v2(11, "0", 1, "WhiteLight", ("NearLight", "FarLight")))

    assert infrared_v2_row(data, 11, "0") == ("0", 0, "NearLight")


def test_an_infrared_row_that_is_not_index_zero_is_still_found():
    """The reverse of #647: assuming 0 would drive the white light while the user
    watches an infrared toggle."""
    data = {}
    data.update(_v2(4, "0", 0, "WhiteLight"))
    data.update(_v2(4, "0", 2, "InfraredLight", ("FarLight",)))

    assert infrared_v2_row(data, 4, "0") == ("0", 2, "FarLight")


def test_the_bank_comes_from_the_row_rather_than_a_default():
    """Channel 11 carries NearLight and FarLight, channel 1 MiddleLight. Naming the
    wrong one is what made the v1 write answer 200 with the body Error."""
    near = _v2(11, "0", 0, "InfraredLight", ("NearLight", "FarLight"))
    middle = _v2(1, "0", 0, "InfraredLight", ("MiddleLight",))

    assert infrared_v2_row(near, 11, "0")[2] == "NearLight"
    assert infrared_v2_row(middle, 1, "0")[2] == "MiddleLight"


def test_a_row_naming_no_bank_falls_back_to_the_one_every_caller_used():
    data = _v2(11, "0", 0, "InfraredLight", ())

    assert infrared_v2_row(data, 11, "0") == ("0", 0, "MiddleLight")


def test_a_channel_with_no_v2_row_gets_none():
    """Thirteen of that recorder's fifteen channels. None is what keeps them on the
    v1 path rather than writing a row the device does not serve."""
    data = _v2(11, "0", 0, "InfraredLight")

    assert infrared_v2_row(data, 3, "0") is None
    assert infrared_v2_row({}, 11, "0") is None


def test_a_v2_row_that_is_only_a_white_light_is_not_infrared():
    """The illuminator's row must not be mistaken for the emitter's. Driving it
    would turn the white light on and off from the infrared control."""
    data = _v2(7, "0", 0, "WhiteLight", ("NearLight",))

    assert infrared_v2_row(data, 7, "0") is None


def test_a_row_naming_no_light_type_is_not_claimed_as_infrared():
    """A device that says nothing has not said infrared. Guessing here is how the
    white light gets driven by mistake."""
    base = "table.Lighting_V2[7][0][0]."
    data = {base + "Mode": "Auto", base + "MiddleLight[0].Light": "50"}

    assert infrared_v2_row(data, 7, "0") is None


# --- which profile ---------------------------------------------------------------

def test_the_live_profile_is_preferred():
    data = {}
    data.update(_v2(11, "0", 0, "InfraredLight", ("MiddleLight",)))
    data.update(_v2(11, "2", 0, "InfraredLight", ("NearLight",)))

    assert infrared_v2_row(data, 11, "2") == ("2", 0, "NearLight")


def test_profile_zero_is_the_fallback_when_the_live_one_is_not_served():
    """Channel 11 reports nine profiles and the poll holds the one it read. Same
    fallback infrared_profile uses for v1, and it stays inside this channel."""
    data = _v2(11, "0", 0, "InfraredLight", ("NearLight",))

    assert infrared_v2_row(data, 11, "5") == ("0", 0, "NearLight")


def test_a_profile_mode_given_as_an_int_still_matches():
    """get_profile_mode returns a string here and an int there; the keys are
    strings either way."""
    data = _v2(11, "2", 0, "InfraredLight", ("NearLight",))

    assert infrared_v2_row(data, 11, 2) == ("2", 0, "NearLight")


def test_the_live_profile_is_not_searched_twice_when_it_is_zero():
    """A cosmetic guard with teeth: dict.fromkeys collapses the duplicate, and
    without it a channel whose live profile is 0 would be scanned twice and any
    future per-profile side effect would run twice."""
    data = _v2(11, "0", 0, "InfraredLight", ("NearLight",))

    assert infrared_v2_row(data, 11, "0") == ("0", 0, "NearLight")
    assert infrared_v2_row(data, 11, 0) == ("0", 0, "NearLight")
