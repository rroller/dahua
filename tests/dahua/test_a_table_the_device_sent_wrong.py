"""What the client does with a table the device did not send, or sent wrong.

Two different answers to that, both uncovered, and the difference between them is the
point.

**When a table is absent, two getters answer "off" rather than failing.** A camera that
refuses `MotionDetect` reports motion detection as disabled, and the same for the Amcrest
video-analyse rule. That is a deliberate choice with a visible consequence: the switch
shows *off* instead of the device going unavailable. It is defensible, because a table the
device will not serve is a feature it does not have, and it is worth pinning precisely
because the alternative reading, "we could not tell", is equally plausible from the code.

**When a table is present but malformed, `lighting_scheme_illuminator_tables` refuses to
build a write.** It has four separate guards, each with its own message, and every one of
them exists so the failure happens *before* a garbled table is sent back to the camera.
`setConfig` replaces the whole table, so writing a malformed one is not a no-op: it is a
camera whose lighting configuration has been overwritten with nonsense. A `TypeError`
raised deep inside the loop would be the same crash with none of the explanation.

The guards are tested one at a time, each against an otherwise valid pair of tables, so a
test cannot pass because some earlier guard caught it instead. That is the failure mode a
single "malformed input raises" test would have.
"""

import pytest
from aiohttp import ClientResponseError
from types import SimpleNamespace

from custom_components.dahua.client import (
    DahuaClient,
    lighting_scheme_illuminator_tables,
)

CHANNEL = 0
PROFILE = 0
LIGHT = 1


def _refused(status=400):
    return ClientResponseError(
        request_info=SimpleNamespace(real_url="http://10.0.0.5/x"),
        history=(),
        status=status,
    )


def _client(answer):
    client = object.__new__(DahuaClient)

    async def async_get_config(name):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    client.async_get_config = async_get_config
    return client


# --- a table the device will not serve -------------------------------------


async def test_a_refused_motion_detect_table_reads_as_disabled():
    """Not an error. A device that will not serve the table does not have the feature,
    so the switch shows off rather than the whole device going unavailable."""
    assert await _client(_refused()).async_get_config_motion_detection() == {
        "table.MotionDetect[0].Enable": "false"
    }


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
async def test_any_refusal_reads_as_disabled(status):
    """Every status, deliberately. This one is unlike `async_get_config_lighting`, which
    singles out 400 and re-raises the rest; pinned as measured so the difference between
    the two is visible rather than looking like an oversight in one of them."""
    answer = await _client(_refused(status)).async_get_config_motion_detection()

    assert answer == {"table.MotionDetect[0].Enable": "false"}


async def test_a_table_the_device_does_serve_is_returned_untouched():
    """The control. Without it every assertion above could hold for a method that
    always answers "false"."""
    real = {
        "table.MotionDetect[0].Enable": "true",
        "table.MotionDetect[0].DetectVersion": "V3.0",
    }

    assert await _client(real).async_get_config_motion_detection() == real


async def test_a_refused_amcrest_rule_reads_as_disabled():
    assert await _client(_refused()).async_get_video_analyse_rules_for_amcrest() == {
        "table.VideoAnalyseRule[0][0].Enable": "false"
    }


async def test_an_amcrest_rule_that_answers_is_returned_untouched():
    real = {"table.VideoAnalyseRule[0][0].Enable": "true"}

    assert await _client(real).async_get_video_analyse_rules_for_amcrest() == real


# --- a table that is present but wrong -------------------------------------


def _scheme(mode="NightMode"):
    return [[{"LightingMode": mode}]]


def _lighting(**overrides):
    row = {
        "LightType": "WhiteLight",
        # Manual, not Off: a fixture that starts Off cannot show whether the
        # emitter was stopped, which is how two tests here passed vacuously.
        "Mode": "Manual",
        "PercentOfMaxBrightness": 0,
        "NearLight": [{"Light": 0}],
        "MiddleLight": [{"Light": 0}],
        "FarLight": [{"Light": 0}],
    }
    row.update(overrides)
    # [channel][profile][light]: three levels, and the light under test is
    # index 1 so that a fixture which flattened a level cannot pass by luck.
    return [[[{}, row]]]


def _build(scheme=None, lighting=None, enabled=True, brightness=50, restore_mode=None):
    return lighting_scheme_illuminator_tables(
        _scheme() if scheme is None else scheme,
        _lighting() if lighting is None else lighting,
        CHANNEL,
        PROFILE,
        LIGHT,
        enabled,
        brightness,
        restore_mode,
    )


def test_a_valid_pair_builds_both_tables():
    """The control for everything below: each guard test uses an otherwise valid pair,
    so a raise has to come from the one thing that test broke."""
    scheme, lighting = _build()

    assert scheme[CHANNEL][PROFILE]["LightingMode"] == "WhiteMode"
    assert lighting[CHANNEL][PROFILE][LIGHT]["Mode"] == "Manual"


def test_the_inputs_are_not_modified():
    """It deep-copies, because the caller holds the polled tables and a half-built
    write left in them would be sent by the next unrelated call."""
    scheme_in, lighting_in = _scheme(), _lighting()

    _build(scheme_in, lighting_in)

    assert scheme_in[CHANNEL][PROFILE]["LightingMode"] == "NightMode"
    assert lighting_in[CHANNEL][PROFILE][LIGHT]["Mode"] == "Manual"


def test_a_light_the_tables_do_not_contain_is_named_as_such():
    with pytest.raises(ValueError, match="do not contain"):
        lighting_scheme_illuminator_tables(
            _scheme(), _lighting(), CHANNEL, PROFILE, 99, True, 50
        )


@pytest.mark.parametrize("row", ["not a dict", 5, None, []])
def test_a_scheme_row_that_is_not_a_row_is_refused(row):
    """Reaching `.get` on it would be an AttributeError from inside the builder, which
    is the same failure with none of the explanation."""
    with pytest.raises(ValueError, match="malformed rows"):
        _build(scheme=[[row]])


@pytest.mark.parametrize("mode", [None, "", 5, []])
def test_a_scheme_with_no_usable_lighting_mode_is_refused(mode):
    """The mode is what gets restored when the light is turned off again, so building a
    write without knowing it means a camera that cannot be put back."""
    with pytest.raises(ValueError, match="missing LightingMode"):
        _build(scheme=[[{"LightingMode": mode}]])


def test_a_light_that_is_not_the_white_emitter_is_refused():
    """Writing WhiteMode against the infrared emitter would configure the wrong light
    and leave the white one dark, which reads as the feature not working."""
    with pytest.raises(ValueError, match="not the white emitter"):
        _build(lighting=_lighting(LightType="IRLight"))


@pytest.mark.parametrize("bank_name", ["NearLight", "MiddleLight", "FarLight"])
def test_a_bank_that_is_not_a_list_is_refused(bank_name):
    """All three, because each is read in the same loop and only one wrong shape is
    needed to garble the table."""
    with pytest.raises(ValueError, match="bank is malformed"):
        _build(lighting=_lighting(**{bank_name: "not a list"}))


@pytest.mark.parametrize("bank_name", ["NearLight", "MiddleLight", "FarLight"])
def test_an_emitter_that_is_not_a_row_is_refused(bank_name):
    with pytest.raises(ValueError, match="entry is malformed"):
        _build(lighting=_lighting(**{bank_name: ["not a dict"]}))


def test_a_missing_bank_is_not_an_error():
    """Absent is different from malformed: not every model carries all three banks, and
    refusing on that would lose the feature on the ones that do not."""
    lighting = _lighting()
    del lighting[CHANNEL][PROFILE][LIGHT]["FarLight"]

    _scheme_out, built = _build(lighting=lighting)

    assert built[CHANNEL][PROFILE][LIGHT]["Mode"] == "Manual"


def test_the_brightness_reaches_every_emitter():
    _scheme_out, built = _build(brightness=70)

    row = built[CHANNEL][PROFILE][LIGHT]
    assert row["PercentOfMaxBrightness"] == 70
    for bank_name in ("NearLight", "MiddleLight", "FarLight"):
        assert [e["Light"] for e in row[bank_name]] == [70], bank_name


# --- and turning it off again ----------------------------------------------


def test_turning_it_off_restores_the_captured_mode():
    """The captured mode must differ from the one the scheme is already on, or the
    assertion holds whether or not it was restored."""
    scheme, _lighting_out = _build(
        scheme=_scheme("WhiteMode"), enabled=False, restore_mode="DoubleMode"
    )

    assert scheme[CHANNEL][PROFILE]["LightingMode"] == "DoubleMode"


def test_with_no_captured_mode_only_a_still_selected_white_light_is_stopped():
    """Do not guess a mode. Stopping the emitter without changing the scheme is the
    conservative half of the pair."""
    scheme, lighting = _build(scheme=_scheme("WhiteMode"), enabled=False)

    assert scheme[CHANNEL][PROFILE]["LightingMode"] == "WhiteMode"
    assert lighting[CHANNEL][PROFILE][LIGHT]["Mode"] == "Off"


def test_a_scheme_on_another_mode_is_left_alone_entirely():
    """Somebody else selected NightMode since. Neither the scheme nor the emitter is
    ours to change, and writing either would fight the other setting."""
    scheme, lighting = _build(scheme=_scheme("NightMode"), enabled=False)

    assert scheme[CHANNEL][PROFILE]["LightingMode"] == "NightMode"
    assert (
        lighting[CHANNEL][PROFILE][LIGHT]["Mode"] == "Manual"
    ), "the emitter was stopped even though the scheme is on another mode"
