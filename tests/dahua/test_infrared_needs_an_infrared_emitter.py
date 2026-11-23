"""An Infrared light on a camera that has no infrared emitter.

The infrared half of #1094. rroller's IPC-Color4K-X is a full-colour camera
with one white emitter and no infrared at all, matches none of the model
strings `supports_infrared_light` excludes, and so got an Infrared light and an
Infrared Mode select that drive nothing. Its own Lighting_V2 table says so, on
two firmwares (2.840.0000000.28.R and 35.R):

    light[0]  LightType=WhiteLight   7 fields  banks=['NearLight', 'FarLight']

and `devVideoInput.getCapsEx` agrees: `LightTypeComplex: ["WhiteLight"]`.

A row is believed only when it is a physical emitter type and carries a
brightness bank: measured on a DHI-NVR5464-16P-EI, that agreed with
`LightTypeComplex` on all 36 InfraredLight and WhiteLight rows, while a bare
`LightType` names stubs for emitters that are not fitted.
"""

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator(data, model="IPC-Color4K-X", channel=0):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.data = data
    c._channel = channel
    c.model = model
    c._supports_lighting = True
    c.is_indoor_monitor_without_video = lambda: False
    return c


def _row(data, index, light, banks=("NearLight", "FarLight"), channel=0):
    prefix = "table.Lighting_V2[{0}][0][{1}]".format(channel, index)
    data["{0}.LightType".format(prefix)] = light
    data["{0}.Mode".format(prefix)] = "Auto"
    for bank in banks:
        data["{0}.{1}[0].Light".format(prefix, bank)] = "50"
    return data


# --- the #1094 camera -------------------------------------------------------


def test_a_camera_that_lists_only_a_white_emitter_has_no_infrared():
    """rroller's IPC-Color4K-X, the row exactly as it reports it."""
    coordinator = _coordinator(_row({}, 0, "WhiteLight"))

    assert coordinator.supports_infrared_light() is False


def test_a_white_emitter_beside_a_smart_dual_light_composite_still_has_none():
    """AIMixLight is the composite of the other two, not an emitter."""
    data = _row({}, 0, "WhiteLight")
    _row(data, 1, "AIMixLight", banks=())

    assert _coordinator(data).supports_infrared_light() is False


# --- what must keep its infrared --------------------------------------------


def test_a_camera_that_lists_a_banked_infrared_emitter_keeps_it():
    assert (
        _coordinator(
            _row({}, 0, "InfraredLight", banks=("MiddleLight",))
        ).supports_infrared_light()
        is True
    )


def test_a_dual_light_camera_keeps_it():
    data = _row({}, 0, "InfraredLight", banks=("FarLight", "NearLight"))
    _row(data, 1, "WhiteLight")
    _row(data, 2, "AIMixLight", banks=())

    assert _coordinator(data).supports_infrared_light() is True


def test_a_camera_that_names_no_lights_keeps_the_old_answer():
    """Older firmware names no LightType. Silence is not absence."""
    data = {"table.Lighting_V2[0][0][0].Mode": "Auto"}

    assert _coordinator(data).supports_infrared_light() is True


def test_no_lighting_v2_at_all_keeps_the_old_answer():
    assert _coordinator({}).supports_infrared_light() is True
    assert _coordinator(None).supports_infrared_light() is True


def test_a_stub_white_row_is_not_evidence_that_infrared_is_absent():
    """The NVR's channel 1: a bankless WhiteLight row for an emitter that is
    not fitted. A table naming only stubs has not listed its emitters."""
    data = _row({}, 0, "WhiteLight", banks=())

    assert _coordinator(data).supports_infrared_light() is True


def test_only_a_composite_row_is_not_evidence_either():
    data = _row({}, 0, "AIMixLight", banks=())

    assert _coordinator(data).supports_infrared_light() is True


def test_another_channels_rows_do_not_decide_this_one():
    """Lighting_V2 is indexed by channel, and a recorder answers for all of
    them in one table."""
    data = _row({}, 0, "WhiteLight", channel=3)

    assert _coordinator(data, channel=0).supports_infrared_light() is True
    assert _coordinator(data, channel=3).supports_infrared_light() is False


# --- the model list still applies -------------------------------------------


def test_a_model_already_excluded_by_name_stays_excluded():
    data = _row({}, 0, "InfraredLight")

    assert (
        _coordinator(data, model="IPC-HDW3849HP-AS-PV").supports_infrared_light()
        is False
    )
