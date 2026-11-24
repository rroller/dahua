"""Adding a channel to a recorder through the subentry flow (#947).

`async_get_supported_subentry_types` registers the channel subentry flow, so Home
Assistant shows an "Add a channel" button — but the flow only had reconfigure, so
pressing it raised "DahuaChannelSubentryFlow doesn't support step user". This covers
the new step: it builds the same subentry shape setup does for an extra channel,
inheriting the recorder's connection, and refuses a channel that already exists.

Built like test_form_labels_match_the_form.py: a bare flow with `_get_entry` and
`async_create_entry` stubbed, so it needs no Home Assistant.
"""

from types import SimpleNamespace

from custom_components.dahua.config_flow import DahuaChannelSubentryFlow
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_AREA,
    CONF_CHANNEL,
    CONF_DISABLE_BACKCHANNEL,
    CONF_EVENTS,
    CONF_LABELS,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_USE_RPC2,
    DEFAULT_EVENTS,
)
from custom_components.dahua.migrate import CHANNEL_SUBENTRY

BASE = {
    CONF_ADDRESS: "10.0.0.1",
    "username": "admin",
    "password": "secret",
    "port": "80",
    "rtsp_port": "554",
    CONF_CHANNEL: 0,
    CONF_NAME: "Front Door",
}


def _entry(data, channels=(), options=None, title=None):
    subs = {
        str(i): SimpleNamespace(data={CONF_CHANNEL: c}) for i, c in enumerate(channels)
    }
    return SimpleNamespace(
        data=data,
        subentries=subs,
        options=dict(options or {}),
        title=title or data.get(CONF_NAME),
    )


def _flow(entry):
    flow = DahuaChannelSubentryFlow()
    flow._get_entry = lambda: entry

    def create_entry(**kwargs):
        return {"type": "create_entry", **kwargs}

    flow.async_create_entry = create_entry
    return flow


async def test_adding_a_new_channel_inherits_the_connection():
    res = await _flow(_entry(BASE, channels=[0])).async_step_user(
        {
            CONF_CHANNEL: 3,
            CONF_NAME: "Back Gate",
            CONF_AREA: "",
            CONF_EVENTS: ["VideoMotion"],
        }
    )

    assert res["type"] == "create_entry"
    assert res["data"][CONF_CHANNEL] == 3
    assert res["data"][CONF_NAME] == "Back Gate"
    assert res["data"][CONF_ADDRESS] == "10.0.0.1"  # inherited
    assert res["data"]["password"] == "secret"  # inherited
    assert res["data"][CONF_EVENTS] == ["VideoMotion"]
    assert res["unique_id"] == "10.0.0.1_3"
    assert res["title"] == "Back Gate"


async def test_a_channel_that_already_has_a_subentry_is_refused():
    res = await _flow(_entry(BASE, channels=[0, 3])).async_step_user(
        {CONF_CHANNEL: 3, CONF_NAME: "dup", CONF_AREA: ""}
    )

    assert res["type"] != "create_entry"
    assert res["errors"] == {CONF_CHANNEL: "channel_already_added"}


async def test_the_primary_of_a_flat_entry_is_taken():
    """A single-camera entry has no subentries; its one channel lives in the entry
    data, and must not be re-addable as a duplicate."""
    res = await _flow(_entry({**BASE, CONF_CHANNEL: 0})).async_step_user(
        {CONF_CHANNEL: 0, CONF_NAME: "dup", CONF_AREA: ""}
    )

    assert res["errors"] == {CONF_CHANNEL: "channel_already_added"}


async def test_no_area_is_not_stored():
    res = await _flow(_entry(BASE, channels=[0])).async_step_user(
        {CONF_CHANNEL: 2, CONF_NAME: "x", CONF_AREA: ""}
    )

    assert CONF_AREA not in res["data"]


async def test_an_area_moves_the_new_channel():
    res = await _flow(_entry(BASE, channels=[0])).async_step_user(
        {CONF_CHANNEL: 2, CONF_NAME: "x", CONF_AREA: "garage"}
    )

    assert res["data"][CONF_AREA] == "garage"


async def test_events_default_when_not_chosen():
    res = await _flow(_entry(BASE, channels=[0])).async_step_user(
        {CONF_CHANNEL: 2, CONF_NAME: "x", CONF_AREA: ""}
    )

    assert res["data"][CONF_EVENTS] == DEFAULT_EVENTS


async def test_no_input_shows_the_form_defaulting_to_the_next_free_channel():
    res = await _flow(_entry(BASE, channels=[0, 1])).async_step_user()

    assert res["type"] != "create_entry"
    assert res.get("data_schema") is not None
    defaults = {
        str(k): k.default()
        for k in res["data_schema"].schema
        if hasattr(k, "default") and callable(k.default)
    }
    assert defaults.get(CONF_CHANNEL) == 2


async def test_adding_a_channel_to_a_flat_entry_promotes_the_primary_with_its_options():
    """Adding a second channel to a single-camera entry must promote channel 0
    to its own subentry carrying per-channel options and no host-wide ones (#1236)."""
    entry = _entry(
        {**BASE, CONF_CHANNEL: 0, CONF_LABELS: ["front"]},
        options={
            CONF_EVENTS: ["SmartMotionHuman"],
            CONF_AREA: "porch",
            CONF_DISABLE_BACKCHANNEL: True,
            CONF_SCAN_INTERVAL: 60,
            CONF_USE_RPC2: True,
        },
    )
    added = []

    def add_subentry(target, subentry):
        assert target is entry
        if any(s.unique_id == subentry.unique_id for s in target.subentries.values()):
            raise ValueError(f"duplicate subentry unique_id {subentry.unique_id}")
        added.append(subentry)
        target.subentries[subentry.subentry_id] = subentry
        return True

    flow = _flow(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_add_subentry=add_subentry)
    )

    res = await flow.async_step_user(
        {
            CONF_CHANNEL: 1,
            CONF_NAME: "Side Gate",
            CONF_AREA: "",
        }
    )

    assert len(added) == 1
    primary = added[0]
    assert primary.subentry_type == CHANNEL_SUBENTRY
    assert primary.unique_id == "10.0.0.1_0"
    assert primary.title == "Front Door"
    assert primary.data[CONF_CHANNEL] == 0
    assert primary.data[CONF_EVENTS] == ["SmartMotionHuman"]
    assert primary.data[CONF_AREA] == "porch"
    assert primary.data[CONF_DISABLE_BACKCHANNEL] is True
    assert CONF_SCAN_INTERVAL not in primary.data
    assert CONF_USE_RPC2 not in primary.data
    assert CONF_LABELS not in primary.data

    assert res["type"] == "create_entry"
    assert res["unique_id"] == "10.0.0.1_1"
    assert res["data"][CONF_CHANNEL] == 1
    assert CONF_LABELS not in res["data"]

    # Persist the newly created channel 1 subentry; the next "Add a channel"
    # form must default to channel 2, not offer channel 0 again.
    entry.subentries["sub_1"] = SimpleNamespace(
        data=res["data"], unique_id=res["unique_id"]
    )
    next_form = await flow.async_step_user()
    next_defaults = {
        str(k): k.default()
        for k in next_form["data_schema"].schema
        if hasattr(k, "default") and callable(k.default)
    }
    assert next_defaults.get(CONF_CHANNEL) == 2


async def test_adding_a_channel_when_subentries_exist_does_not_re_add_primary():
    entry = _entry(BASE, channels=[0])
    flow = _flow(entry)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("async_add_subentry must not be called for primary again")

    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_add_subentry=fail_if_called)
    )

    res = await flow.async_step_user(
        {CONF_CHANNEL: 1, CONF_NAME: "Channel 2", CONF_AREA: ""}
    )

    assert res["type"] == "create_entry"
    assert res["unique_id"] == "10.0.0.1_1"


async def test_string_primary_channel_is_taken_and_promoted_as_int():
    zero_entry = _entry({**BASE, CONF_CHANNEL: "0"})
    dup = await _flow(zero_entry).async_step_user(
        {CONF_CHANNEL: 0, CONF_NAME: "dup", CONF_AREA: ""}
    )
    assert dup["errors"] == {CONF_CHANNEL: "channel_already_added"}

    form = await _flow(zero_entry).async_step_user()
    defaults = {
        str(k): k.default()
        for k in form["data_schema"].schema
        if hasattr(k, "default") and callable(k.default)
    }
    assert defaults.get(CONF_CHANNEL) == 1

    non_zero_entry = _entry({**BASE, CONF_CHANNEL: "2", CONF_NAME: "Side Door"})
    added = []
    flow = _flow(non_zero_entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_add_subentry=lambda _e, sub: added.append(sub) or True
        )
    )
    res = await flow.async_step_user(
        {CONF_CHANNEL: 3, CONF_NAME: "Back Door", CONF_AREA: ""}
    )

    assert res["type"] == "create_entry"
    assert len(added) == 1
    assert added[0].data[CONF_CHANNEL] == 2
    assert added[0].unique_id == "10.0.0.1_2"
