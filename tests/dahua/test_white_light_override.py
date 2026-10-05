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


# --- disabling the scene schedule while forced -------------------------------


def test_build_on_disables_each_scene_schedule(tables):
    """A scene schedule left enabled can flip the scene back off WhiteMode on a
    timer. build_on turns Enable off wherever a schedule is present, and never
    adds one where it is absent."""
    scheme, lighting = tables
    scheme = copy.deepcopy(scheme)
    scheme[0]["SchemeSchedule"]["Enable"] = True  # a camera with it on
    del scheme[1]["SchemeSchedule"]  # and one with no schedule at all

    scheme_on, _ = wlo.build_on(scheme, lighting, 100)

    assert scheme_on[0]["SchemeSchedule"]["Enable"] is False
    assert "SchemeSchedule" not in scheme_on[1]


# --- merging the restore so other scenes survive -----------------------------


def test_build_restore_puts_every_owned_part_back(tables):
    """With nothing changed since, the merge is the whole original: every scene
    is still WhiteMode and every WhiteLight still Manual, so all of them revert."""
    scheme, lighting = tables
    scheme_on, lighting_on = wlo.build_on(scheme, lighting, 100)

    rs, rl = wlo.build_restore(scheme_on, lighting_on, scheme, lighting)

    assert wlo.scheme_modes(rs) == wlo.scheme_modes(scheme)
    assert wlo.white_light_modes(rl) == wlo.white_light_modes(lighting)


def test_build_restore_preserves_a_scene_changed_since(tables):
    """A scene no longer in WhiteMode was changed while the light was on, so the
    merge leaves it exactly as the camera reports it and reverts only the rest."""
    scheme, lighting = tables
    scheme_on, lighting_on = wlo.build_on(scheme, lighting, 100)
    cur = copy.deepcopy(scheme_on)
    cur[3]["LightingMode"] = "ColorMode"  # a value neither original nor forced
    cur[3]["Marker"] = "user"

    rs, _ = wlo.build_restore(cur, lighting_on, scheme, lighting)

    assert rs[3] == cur[3]  # the user's scene is untouched
    assert rs[0]["LightingMode"] == scheme[0]["LightingMode"]  # the rest reverts


def test_build_restore_preserves_a_white_row_changed_since(tables):
    """Same for the WhiteLight row: one no longer Manual is left alone, while the
    rows still Manual go back to what the snapshot held."""
    scheme, lighting = tables
    scheme_on, lighting_on = wlo.build_on(scheme, lighting, 100)
    cur = copy.deepcopy(lighting_on)
    wlo.white_light(cur[2])["Mode"] = "Auto"

    _, rl = wlo.build_restore(scheme_on, cur, scheme, lighting)

    assert wlo.white_light(rl[2])["Mode"] == "Auto"  # preserved
    assert wlo.white_light(rl[0])["Mode"] == wlo.white_light(lighting[0])["Mode"]


def test_build_restore_does_not_mutate_its_inputs(tables):
    scheme, lighting = tables
    scheme_on, lighting_on = wlo.build_on(scheme, lighting, 100)
    a, b, c, d = (
        copy.deepcopy(scheme_on),
        copy.deepcopy(lighting_on),
        copy.deepcopy(scheme),
        copy.deepcopy(lighting),
    )

    wlo.build_restore(scheme_on, lighting_on, scheme, lighting)

    assert scheme_on == a and lighting_on == b and scheme == c and lighting == d
