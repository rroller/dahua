"""The pure half of forcing a recorder channel's white light on (#959).

Driven by the tables a DHI-NVR5464-16P-EI really returned for channel 11 (a
VSIPP-6DIRMD-I3), captured read-only and kept in tests/dahua/fixtures. Those
are the shapes that lit the physical light when both were written and left it
off when only Lighting_V2 was.
"""

import copy
import json
from pathlib import Path

import pytest

from custom_components.dahua import white_light_override as wlo

FIXTURE = Path(__file__).parent / "fixtures" / "nvr5464_ch11_lighting.json"


@pytest.fixture
def tables():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["LightingScheme"], data["Lighting_V2"]


# --- recognising the shape -------------------------------------------------


def test_the_recorder_tables_are_a_shape_this_can_drive(tables):
    scheme, lighting = tables
    assert wlo.lighting_shape(scheme, lighting) == 9


def test_the_captured_modes_read_as_measured(tables):
    """What the camera was doing before anything was written: AI everywhere,
    the white light Off on scene 0 and zoom-priority on the rest."""
    scheme, lighting = tables
    assert wlo.scheme_modes(scheme) == ["AIMode"] * 9
    assert wlo.white_light_modes(lighting) == ["Off"] + ["ZoomPrio"] * 8


@pytest.mark.parametrize(
    "bad_scheme, bad_lighting, why",
    [
        (None, None, "no tables"),
        ([], [], "empty"),
        ([{"LightingMode": "AIMode"}], [[], []], "lengths differ"),
        (
            [{"NoMode": 1}],
            [[{"LightType": "WhiteLight", "Mode": "Off"}]],
            "no LightingMode",
        ),
        (
            [{"LightingMode": "AIMode"}],
            [[{"LightType": "InfraredLight", "Mode": "Auto"}]],
            "no WhiteLight",
        ),
        (
            [{"LightingMode": "AIMode"}],
            [
                [
                    {"LightType": "WhiteLight", "Mode": "Off"},
                    {"LightType": "WhiteLight", "Mode": "Off"},
                ]
            ],
            "two WhiteLights",
        ),
        (
            [{"LightingMode": "AIMode"}],
            [[{"LightType": "WhiteLight"}]],
            "WhiteLight has no Mode",
        ),
        # A direct camera's channel-scoped read is table[channel][profile], one
        # level deeper than a recorder channel's table[profile]. The probe gate
        # (_supports_lighting_v2 and not the Color4M flag) is true for nearly
        # every lighting_v2 camera, so this shape must read as undrivable or a
        # direct camera would be routed off its working whole-table path (#959,
        # the #570/#676/#690 model-gate bug class this fix is careful not to
        # repeat).
        (
            [[{"LightingMode": "AIMode"}]],
            [[[{"LightType": "WhiteLight", "Mode": "Off"}]]],
            "direct-camera shape: both tables indexed by channel",
        ),
        (
            [{"LightingMode": "AIMode"}],
            [[[{"LightType": "WhiteLight", "Mode": "Off"}]]],
            "direct-camera shape: profiles nested a level deeper",
        ),
    ],
)
def test_anything_else_is_refused_rather_than_guessed(bad_scheme, bad_lighting, why):
    assert wlo.lighting_shape(bad_scheme, bad_lighting) is None, why


# --- building the on state ---------------------------------------------------


def test_on_forces_every_scene_and_every_white_light(tables):
    scheme, lighting = tables
    scheme_on, lighting_on = wlo.build_on(scheme, lighting, 100)

    assert wlo.scheme_modes(scheme_on) == ["WhiteMode"] * 9
    assert wlo.white_light_modes(lighting_on) == ["Manual"] * 9
    assert wlo.is_forced_on(scheme_on, lighting_on)


def test_brightness_lands_in_the_banks_the_row_already_has(tables):
    scheme, lighting = tables
    _, lighting_on = wlo.build_on(scheme, lighting, 60)

    row = wlo.white_light(lighting_on[0])
    assert row["NearLight"][0]["Light"] == 60
    assert row["FarLight"][0]["Light"] == 60
    # The ceiling field is not a brightness bank and is left alone.
    assert row["PercentOfMaxBrightness"] == 100


def test_no_field_is_invented(tables):
    """A camera whose WhiteLight row has no MiddleLight must not grow one, and
    nothing else about the row may change shape."""
    scheme, lighting = tables
    _, lighting_on = wlo.build_on(scheme, lighting, 100)

    for before, after in zip(lighting, lighting_on):
        row_before, row_after = wlo.white_light(before), wlo.white_light(after)
        assert set(row_after) == set(row_before)
        assert "MiddleLight" not in row_after


def test_only_the_white_light_row_changes(tables):
    """The infrared and mixed-light rows, and the scene schedule, are carried
    over byte for byte."""
    scheme, lighting = tables
    scheme_on, lighting_on = wlo.build_on(scheme, lighting, 100)

    for before, after in zip(lighting, lighting_on):
        others_before = [r for r in before if r.get("LightType") != "WhiteLight"]
        others_after = [r for r in after if r.get("LightType") != "WhiteLight"]
        assert others_before == others_after
    for before, after in zip(scheme, scheme_on):
        assert after["SchemeSchedule"] == before["SchemeSchedule"]


def test_the_inputs_are_not_mutated(tables):
    """The caller keeps the originals as the snapshot it will restore."""
    scheme, lighting = tables
    scheme_copy, lighting_copy = copy.deepcopy(scheme), copy.deepcopy(lighting)

    wlo.build_on(scheme, lighting, 100)

    assert scheme == scheme_copy
    assert lighting == lighting_copy


# --- judging a read-back -----------------------------------------------------


def test_the_original_tables_are_not_the_on_state(tables):
    scheme, lighting = tables
    assert not wlo.is_forced_on(scheme, lighting)


def test_restore_is_judged_on_modes_against_the_snapshot(tables):
    """Read back after restoring must show the snapshot's modes -- Off and
    ZoomPrio here -- not some assumed default."""
    scheme, lighting = tables
    scheme_on, lighting_on = wlo.build_on(scheme, lighting, 100)

    assert wlo.modes_match(scheme, lighting, scheme, lighting)
    assert not wlo.modes_match(scheme, lighting, scheme_on, lighting_on)
