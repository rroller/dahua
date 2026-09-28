"""Adding ten channels should not leave ten devices to file by hand.

#746 asks a recorder which of its channels have a camera and offers them for
adding together; #776 lets the user take the lot in one click. Both stop short of
the other half of the chore: every device arrives with **no area**, so the user
goes to Settings and files ten of them one at a time, immediately after being
shown a list that already named every one of them.

The recorder knows its channels are `BACKYARD`, `DRIVEWAY`, `SHED`. This asks
where each of them is while it has the user's attention.

**Why the field keys are the labels.** Home Assistant renders a schema key
verbatim when there is no translation string for it, which is what lets a form
whose fields depend on what the recorder reported read properly without inventing
a translation key per channel. The channel number is in every label, so two
channels sharing a title cannot collide.

**The bug this shape can have.** `_queue_extra_channels` copies `init_info` for
each extra channel, and `init_info` carries the *primary's* area. Without an
explicit override every other channel would silently be filed in the primary's
area, which is worse than filing nothing. That is what
`test_an_area_reaches_only_the_channel_it_was_chosen_for` exists for.
"""
from types import SimpleNamespace

from custom_components.dahua.config_flow import DahuaFlowHandler
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_ALL_CHANNELS,
    CONF_AREA,
    CONF_CHANNEL,
    CONF_EXTRA_CHANNELS,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
)


def _entry_data(channel=0):
    return {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw",
        CONF_ADDRESS: "10.0.0.5",
        CONF_PORT: "80",
        CONF_RTSP_PORT: "554",
        CONF_CHANNEL: channel,
        CONF_NAME: "Front",
    }


def _flow(found=None, chosen=None, channel=0):
    flow = DahuaFlowHandler()
    flow.init_info = _entry_data(channel)
    flow._found_channels = dict(found or {})
    flow._extra_channels = list(chosen or [])
    flow._errors = {}
    return flow


async def _form(flow):
    return await flow.async_step_areas()


def _labels(result):
    return [str(key) for key in result["data_schema"].schema]


async def _submit(flow, answers):
    """Show the form, then answer it, as the frontend does."""
    await _form(flow)

    async def show(data):
        return None

    flow._show_config_form_name = show
    await flow.async_step_areas(answers)
    return flow


# --- the form -----------------------------------------------------------------

async def test_the_form_asks_about_every_device_being_added():
    """The one being set up, and each channel ticked alongside it."""
    flow = _flow(found={1: "BACKYARD", 4: "DRIVEWAY"}, chosen=[1, 4])

    assert _labels(await _form(flow)) == [
        "Front (this device)",
        "Channel 2: BACKYARD",
        "Channel 5: DRIVEWAY",
    ]


async def test_the_channel_number_shown_is_the_one_the_recorder_shows():
    """The index is 0 based and every NVR UI counts from 1, so channel index 4
    is the recorder's channel 5."""
    flow = _flow(found={4: "DRIVEWAY"}, chosen=[4])

    assert "Channel 5: DRIVEWAY" in _labels(await _form(flow))


async def test_the_device_being_added_is_named_not_numbered():
    flow = _flow(found={1: "BACKYARD"}, chosen=[1])

    assert _labels(await _form(flow))[0] == "Front (this device)"


async def test_two_channels_with_the_same_title_still_get_their_own_field():
    """Two cameras called GARAGE is ordinary. Colliding keys would silently
    drop one of them from the form."""
    flow = _flow(found={1: "GARAGE", 2: "GARAGE"}, chosen=[1, 2])

    labels = _labels(await _form(flow))

    assert len(labels) == 3
    assert len(set(labels)) == 3, labels


async def test_a_channel_with_no_title_still_gets_a_field():
    flow = _flow(found={}, chosen=[6])

    assert "Channel 7" in _labels(await _form(flow))


# --- what the answers do ------------------------------------------------------

async def test_the_area_for_the_device_being_added_goes_into_its_entry():
    flow = _flow(found={1: "BACKYARD"}, chosen=[1])

    await _submit(flow, {"Front (this device)": "front_garden"})

    assert flow.init_info[CONF_AREA] == "front_garden"


async def test_an_area_reaches_only_the_channel_it_was_chosen_for():
    """The leak this shape can have.

    Every extra channel inherits init_info, which carries the primary's area, so
    an override that did not clear it would file all of them in the primary's
    area.
    """
    flow = _flow(found={1: "BACKYARD", 2: "DRIVEWAY"}, chosen=[1, 2])

    await _submit(flow, {
        "Front (this device)": "front_garden",
        "Channel 2: BACKYARD": "back_garden",
    })

    started = _queued(flow)
    assert flow.init_info[CONF_AREA] == "front_garden"
    assert started[0][CONF_AREA] == "back_garden"
    assert CONF_AREA not in started[1], (
        "channel 2 was left blank, so it must not inherit the primary's area")


def _queued(flow):
    """Run _queue_extra_channels and return the data each started flow got."""
    started = []
    flow.hass = SimpleNamespace(
        async_create_task=lambda coro: None,
        config_entries=SimpleNamespace(flow=SimpleNamespace(
            async_init=lambda domain, context=None, data=None: started.append(data))),
    )
    flow._queue_extra_channels()
    return started


async def test_a_blank_answer_stores_no_area_at_all():
    """Somebody who has not made their areas yet must still be able to finish."""
    flow = _flow(found={1: "BACKYARD"}, chosen=[1])

    await _submit(flow, {})

    assert CONF_AREA not in flow.init_info
    assert CONF_AREA not in _queued(flow)[0]


async def test_an_empty_string_is_treated_as_no_area():
    """A cleared picker submits "" rather than omitting the field."""
    flow = _flow(found={1: "BACKYARD"}, chosen=[1])

    await _submit(flow, {"Front (this device)": "",
                         "Channel 2: BACKYARD": ""})

    assert CONF_AREA not in flow.init_info
    assert CONF_AREA not in _queued(flow)[0]


async def test_the_areas_step_goes_on_to_naming():
    flow = _flow(found={1: "BACKYARD"}, chosen=[1])
    await _form(flow)
    shown = {}

    async def show(data):
        shown["data"] = data

    flow._show_config_form_name = show

    await flow.async_step_areas({})

    assert shown["data"] is flow.init_info


# --- when it is offered at all ------------------------------------------------

async def test_choosing_channels_leads_to_the_areas_step():
    flow = _flow(found={1: "BACKYARD"})
    reached = {}

    async def areas():
        reached["yes"] = True

    flow.async_step_areas = areas

    await flow.async_step_channels({CONF_EXTRA_CHANNELS: ["1"]})

    assert reached.get("yes"), "ticking a channel must lead to the areas step"


async def test_choosing_no_channels_skips_it():
    """A single camera gains no step. Its area is in the options flow, and Home
    Assistant's own device page is one click away."""
    flow = _flow(found={1: "BACKYARD"})
    reached = {}
    shown = {}

    async def areas():
        reached["yes"] = True

    async def show(data):
        shown["data"] = data

    flow.async_step_areas = areas
    flow._show_config_form_name = show

    await flow.async_step_channels({CONF_EXTRA_CHANNELS: []})

    assert not reached, "nothing to place, so do not ask"
    assert shown["data"] is flow.init_info


async def test_taking_all_the_channels_also_leads_to_the_areas_step():
    flow = _flow(found={1: "BACKYARD", 2: "DRIVEWAY"})
    reached = {}

    async def areas():
        reached["yes"] = True

    flow.async_step_areas = areas

    await flow.async_step_channels({CONF_ALL_CHANNELS: True})

    assert reached.get("yes")
