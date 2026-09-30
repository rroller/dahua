"""The area a channel was given, converted from an id to the name device_info needs.

There is one conversion in the middle of adding a device, and it was never executed.

The picker in the config flow returns an **area_id**. `device_info`'s `suggested_area`
is matched on the area **name**. `configured_area_name` is the only thing between them,
so if it answers wrongly the channel is filed in the wrong place, or worse: **passing a
stale id straight through would have Home Assistant create a brand new area named after
the id**, which is how you end up with an area called
`1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d` in someone's house.

That is the case this pins hardest. An area the user has since deleted must come back as
`None`, not as its own id, and the only way to see the difference is to ask with an id the
registry no longer knows.

The area is also per channel since #827, so the precedence matters: a channel's own answer
wins over the entry's. Getting that backwards files ten channels of a recorder into
whichever area the primary channel was given, which is the fault
[[a-redesigns-assumptions-hide-in-fakes]] is about.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.const import CONF_AREA

AREA_ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
AREA_NAME = "Front Garden"


def _coordinator(*, channel_config=None, options=None, entry_data=None):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.hass = SimpleNamespace()
    coordinator._channel_config = {} if channel_config is None else channel_config
    coordinator.config_entry = SimpleNamespace(
        data={} if entry_data is None else entry_data,
        options={} if options is None else options,
    )
    return coordinator


@pytest.fixture
def registry(monkeypatch):
    """Stands in for the area registry, and records what it was asked for.

    Patched as a whole rather than one function deep, so nothing else in the module
    reaches a real registry by accident.
    """
    from custom_components.dahua import coordinator as coordinator_module

    known = {}
    asked = []

    def async_get_area(area_id):
        asked.append(area_id)
        return known.get(area_id)

    monkeypatch.setattr(
        coordinator_module, "ar",
        SimpleNamespace(async_get=lambda hass: SimpleNamespace(
            async_get_area=async_get_area)))
    return SimpleNamespace(known=known, asked=asked)


# --- the conversion -----------------------------------------------------------


def test_the_id_is_converted_to_the_name(registry):
    """`suggested_area` is matched on the name, and the flow stored an id."""
    registry.known[AREA_ID] = SimpleNamespace(name=AREA_NAME)
    coordinator = _coordinator(channel_config={CONF_AREA: AREA_ID})

    assert coordinator.configured_area_name() == AREA_NAME
    assert registry.asked == [AREA_ID]


def test_an_area_the_user_deleted_is_forgotten_rather_than_invented(registry):
    """The one that matters. Returning the id here would have Home Assistant create a
    new area *named after the id*, so the failure is not a missing area, it is a
    junk one appearing in the user's house."""
    coordinator = _coordinator(channel_config={CONF_AREA: AREA_ID})

    answer = coordinator.configured_area_name()

    assert answer is None, answer
    assert answer != AREA_ID, "the stale id was passed through as a name"


def test_no_area_configured_does_not_touch_the_registry(registry):
    """Nothing to look up, and asking anyway would need a registry to exist at a point
    in setup where it may not."""
    assert _coordinator().configured_area_name() is None
    assert registry.asked == []


@pytest.mark.parametrize("stored", ["", None])
def test_an_empty_area_is_treated_as_none(stored):
    """The options flow can store a cleared picker as either."""
    coordinator = _coordinator(channel_config={CONF_AREA: stored})

    assert coordinator.configured_area_name() is None


# --- and whose answer is used ------------------------------------------------


def test_the_channel_beats_the_entry(registry):
    """Since #827 an entry is a host and each channel is a subentry. Reading the
    entry's answer for a channel that has its own files every channel of a recorder
    into one area."""
    registry.known["own"] = SimpleNamespace(name="Side Gate")
    registry.known["entrys"] = SimpleNamespace(name="Everything Else")
    coordinator = _coordinator(
        channel_config={CONF_AREA: "own"}, options={CONF_AREA: "entrys"})

    assert coordinator.configured_area_name() == "Side Gate"


def test_the_entrys_options_are_used_when_the_channel_has_no_answer(registry):
    """Deliberately kept as a fallback: an area set in the options flow is an
    entry-wide answer the user gave, so applying it to a channel that has not chosen
    one is coherent.

    "No answer" means the key is **absent**, not present and empty. See below.
    """
    registry.known["entrys"] = SimpleNamespace(name="Everything Else")
    coordinator = _coordinator(
        channel_config={"something_else": True}, options={CONF_AREA: "entrys"})

    assert coordinator.configured_area_name() == "Everything Else"


def test_an_area_explicitly_cleared_on_a_channel_blocks_the_entrys_answer(registry):
    """Measured, and surprising enough to pin. `channel_option` tests whether the key
    is *present*, not whether it holds anything, so a channel whose area was cleared
    to None keeps its own empty answer instead of inheriting the entry's.

    That is arguably the right behaviour -- clearing a channel's area should not hand
    it the entry's -- but it is not what the code looks like it does, and the two
    readings differ for every channel a user has ever cleared.
    """
    registry.known["entrys"] = SimpleNamespace(name="Everything Else")
    coordinator = _coordinator(
        channel_config={CONF_AREA: None}, options={CONF_AREA: "entrys"})

    assert coordinator.configured_area_name() is None
    assert registry.asked == [], "the entry's area was looked up anyway"


def test_a_single_camera_falls_back_to_its_own_entry_data(registry):
    """With no channel config the entry's data *is* this channel's, which is what a
    single camera looks like, so the area chosen while adding it applies."""
    registry.known["added"] = SimpleNamespace(name="Driveway")
    coordinator = _coordinator(entry_data={CONF_AREA: "added"})

    assert coordinator.configured_area_name() == "Driveway"


def test_a_recorder_channel_does_not_inherit_the_primary_channels_area(registry):
    """`entry.data` on a merged recorder is the *primary* channel's config, so using
    it as the fallback filed every blank channel into the primary's area. Having a
    channel config at all is what marks this case."""
    registry.known["primarys"] = SimpleNamespace(name="Primary Channel Area")
    coordinator = _coordinator(
        channel_config={"something_else": True}, entry_data={CONF_AREA: "primarys"})

    assert coordinator.configured_area_name() is None
    assert registry.asked == [], "the primary channel's area was looked up"
