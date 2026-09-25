"""The preset dropdown should offer what the camera has, not ten of everything.

#713. The list was a fixed `["Manual", "1" ... "10"]` for every camera that is
not the one model asked over RPC2. So a camera with two presets still offered
ten, picking the third sent `GotoPreset` for a preset that is not there, and the
device answered 400. In Home Assistant that reads as a failed action rather than
as a preset that does not exist, which is why #713's reporter concluded the
integration could not drive their camera at all.

The device will say. `ptz.cgi?action=getPresets` returns a row per preset, and
only `Index` matters, because the numbers need not be contiguous: deleting
preset 2 leaves 1 and 3.

**An empty answer is not "no presets".** A camera that does not implement the
query answers exactly like a camera with none, and the two cannot be told apart.
Measured on a DHI-NVR5464-16P-EI, whose fixed cameras answer 200 with an empty
body. So an empty answer keeps the old ten, because removing a control from
somebody who is using it is far worse than offering one preset too many.
"""
from types import SimpleNamespace

from custom_components.dahua import dahua_utils
from custom_components.dahua.select import (
    DahuaCameraPresetPositionSelect,
    _async_preset_ids,
)


def _reply(*indexes, named=True):
    """A getPresets reply listing the given preset numbers."""
    data = {}
    for row, index in enumerate(indexes):
        data["presets[{0}].Index".format(row)] = str(index)
        if named:
            data["presets[{0}].Name".format(row)] = "Preset {0}".format(index)
    return data


def _coordinator(answer):
    """A coordinator whose camera answers getPresets with `answer`."""
    async def get_presets(channel):
        if isinstance(answer, Exception):
            raise answer
        return answer

    return SimpleNamespace(
        client=SimpleNamespace(async_get_ptz_presets=get_presets),
        get_channel_number=lambda: 1,
    )


# --- reading what the camera reports -----------------------------------------

def test_the_presets_a_camera_lists_are_read():
    assert dahua_utils.parse_ptz_presets(_reply(1, 2)) == [1, 2]


def test_gaps_are_kept():
    """Deleting preset 2 leaves 1 and 3, and 3 must still be offered."""
    assert dahua_utils.parse_ptz_presets(_reply(1, 3)) == [1, 3]


def test_the_order_is_by_number_not_by_row():
    """9 before 2 on purpose.

    These are collected in a set, and a set of small integers usually happens
    to come out in ascending order anyway, so most pairs prove nothing about
    sorting. 9 and 2 do not: unsorted they come back the wrong way round.
    """
    assert dahua_utils.parse_ptz_presets(_reply(9, 2)) == [2, 9]


def test_a_repeated_index_is_counted_once():
    assert dahua_utils.parse_ptz_presets(_reply(2, 2)) == [2]


def test_preset_zero_is_not_a_preset():
    """0 is what the device reports when it is not at a preset."""
    assert dahua_utils.parse_ptz_presets(_reply(0, 2)) == [2]


def test_names_are_not_mistaken_for_numbers():
    assert dahua_utils.parse_ptz_presets(_reply(1, named=True)) == [1]


def test_only_the_index_field_counts():
    """A row carries more than its number, and some of it is numeric.

    Matching any key in the row would read a speed or a dwell time as a preset
    that does not exist. A name cannot be mistaken for one because it will not
    parse as a number; these will.
    """
    assert dahua_utils.parse_ptz_presets({
        "presets[0].Index": "1",
        "presets[0].Speed": "7",
        "presets[0].Dwell": "30",
    }) == [1]


def test_a_refusal_reads_as_nothing_rather_than_raising():
    for refusal in ({}, None, "Error", []):
        assert dahua_utils.parse_ptz_presets(refusal) == []


def test_a_non_numeric_index_is_skipped():
    assert dahua_utils.parse_ptz_presets({"presets[0].Index": "x"}) == []


# --- what the entity is then given -------------------------------------------

async def test_a_camera_that_lists_presets_reports_exactly_those():
    assert await _async_preset_ids(_coordinator(_reply(1, 3))) == [1, 3]


async def test_an_empty_answer_keeps_the_old_list():
    """The important one. Empty cannot be told from unsupported."""
    assert await _async_preset_ids(_coordinator({})) == []


async def test_a_camera_that_refuses_keeps_the_old_list():
    assert await _async_preset_ids(_coordinator(RuntimeError("400"))) == []


async def test_a_timeout_does_not_stop_the_platform_loading():
    """One optional read must never cost the camera its other entities."""
    assert await _async_preset_ids(_coordinator(TimeoutError())) == []


async def test_the_channel_number_is_used_not_the_index():
    """A recorder's channel 4 must be asked about channel 4."""
    asked = []

    async def get_presets(channel):
        asked.append(channel)
        return _reply(1)

    coordinator = SimpleNamespace(
        client=SimpleNamespace(async_get_ptz_presets=get_presets),
        get_channel_number=lambda: 4,
    )

    await _async_preset_ids(coordinator)

    assert asked == [4]


# --- the options the entity ends up with -------------------------------------

def _entity(monkeypatch, preset_ids):
    """Build the real select, with only the Home Assistant base stubbed out."""
    import custom_components.dahua.select as select_module

    monkeypatch.setattr(select_module.DahuaBaseEntity, "__init__",
                        lambda self, coordinator, config_entry: None)
    coordinator = SimpleNamespace(
        get_device_name=lambda: "Front Gate",
        get_serial_number=lambda: "SER1",
    )
    return select_module.DahuaCameraPresetPositionSelect(
        coordinator, None, preset_ids=preset_ids)


def test_a_camera_that_said_nothing_still_gets_the_old_ten(monkeypatch):
    """No regression: a camera that cannot answer keeps what it has today."""
    entity = _entity(monkeypatch, None)

    assert entity._attr_options == [
        "Manual", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]


def test_the_options_are_the_presets_the_camera_reported(monkeypatch):
    entity = _entity(monkeypatch, [1, 3])

    assert entity._attr_options == ["Manual", "1", "3"]


def test_manual_is_always_offered(monkeypatch):
    """Manual is how you say "stop following a preset", so it is not optional."""
    assert _entity(monkeypatch, [2])._attr_options[0] == "Manual"
