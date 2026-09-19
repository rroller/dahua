"""Writing the right row is not the same as the light coming on.

Smart Dual Light cameras decide separately, in LightingScheme, which emitter
they are willing to use. While that reads AIMode or InfraredMode the white light
stays off however correct the Lighting_V2 write was -- the value is stored, Home
Assistant reports the light on, and nothing lights up.

Measured on a DH-IPC-HFW3449E-S-IL in #647: the same command that did nothing
under AIMode worked physically once the camera was switched to WhiteMode, with
no change to the integration in between.

Absent is not blocking. Cameras that predate this table report no scheme at all,
and they must not start emitting warnings.
"""

from custom_components.dahua import scheme_blocking_white_light


def _scheme(channel, profile, mode):
    return {"table.LightingScheme[{0}][{1}].LightingMode".format(channel, profile): mode}


# --- the case that wasted a reporter's evening --------------------------------

def test_ai_mode_is_reported_as_blocking():
    assert scheme_blocking_white_light(_scheme(0, 0, "AIMode"), 0, 0) == "AIMode"


def test_infrared_mode_is_reported_as_blocking():
    assert scheme_blocking_white_light(_scheme(0, 1, "InfraredMode"), 0, 1) == "InfraredMode"


def test_an_unknown_scheme_is_treated_as_blocking():
    """Only WhiteMode is known to let the white light through; assume the rest do not."""
    assert scheme_blocking_white_light(_scheme(0, 0, "SomeFutureMode"), 0, 0) == "SomeFutureMode"


# --- and the silence that must be preserved -----------------------------------

def test_white_mode_does_not_block():
    assert scheme_blocking_white_light(_scheme(0, 0, "WhiteMode"), 0, 0) is None


def test_a_camera_with_no_scheme_is_never_reported():
    """Every camera older than this reports nothing here, and is fine."""
    assert scheme_blocking_white_light({}, 0, 0) is None


def test_another_channels_scheme_is_not_borrowed():
    """An NVR's channels each have their own row."""
    assert scheme_blocking_white_light(_scheme(0, 0, "AIMode"), 4, 0) is None


def test_another_profiles_scheme_is_not_borrowed():
    """Day can be AIMode while Night is not, and the write targets one profile."""
    assert scheme_blocking_white_light(_scheme(0, 0, "AIMode"), 0, 1) is None
