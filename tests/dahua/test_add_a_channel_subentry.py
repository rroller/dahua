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
    CONF_EVENTS,
    CONF_NAME,
    DEFAULT_EVENTS,
)

BASE = {
    CONF_ADDRESS: "10.0.0.1",
    "username": "admin",
    "password": "secret",
    "port": "80",
    "rtsp_port": "554",
    CONF_CHANNEL: 0,
    CONF_NAME: "Front Door",
}


def _entry(data, channels=()):
    subs = {
        str(i): SimpleNamespace(data={CONF_CHANNEL: c}) for i, c in enumerate(channels)
    }
    return SimpleNamespace(data=data, subentries=subs)


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
