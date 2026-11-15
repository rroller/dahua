"""The two OSD title text fields: what they read, what they write, and when.

The titles already had services; these are the editable fields the device page
was missing. Two things matter and neither is visible from the entity alone:

* The channel title lives in `ChannelTitle[ch].Name` and the custom title in
  `VideoWidget[ch].CustomTitle[1].Text`. Both tables are host-wide, so a 200
  says nothing about whether *this* channel is in them.
* An empty title is stored as `none` or `||` by some firmware. The field must
  read that as empty rather than show the literal marker, and must keep
  `available` true because the device did answer.
"""

from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua import text as text_module
from custom_components.dahua.coordinator import (
    DahuaDataUpdateCoordinator,
    video_widget_fields,
)
from custom_components.dahua.text import (
    DahuaChannelTitleText,
    DahuaCustomTitleText,
)

from . import adds_entities


def _client(table):
    """A client double with the one read these probes make.

    A namespace rather than a class: the suite's `_looks_like_a_coordinator`
    heuristic matches any class body containing `get_channel`, and
    `async_get_channel_title` would trip it. The namespace is the shape the
    test suite already uses for client doubles.
    """
    asked = []

    async def read_channel_title():
        asked.append("channel_title")
        if isinstance(table, Exception):
            raise table
        return table

    return SimpleNamespace(
        async_get_channel_title=read_channel_title,
        asked=asked,
    )


class _Coordinator:
    subentry_id = None

    def __init__(
        self,
        *,
        channel=0,
        channel_title=None,
        custom_title=None,
        channel_title_supported=True,
        custom_title_supported=True,
        no_video=False,
    ):
        self.client = _client({})
        self._channel = channel
        self._channel_title = channel_title
        self._custom_title = custom_title
        self._channel_title_supported = channel_title_supported
        self._custom_title_supported = custom_title_supported
        self._no_video = no_video
        self.data = {"id": 1}
        self.last_update_success = True
        self.refreshed = 0

    def get_serial_number(self):
        return "SERIAL1"

    def get_address(self):
        return "10.0.0.5"

    def get_channel(self):
        return self._channel

    def supports_channel_title(self):
        return self._channel_title_supported and not self._no_video

    def supports_custom_title(self):
        return self._custom_title_supported and not self._no_video

    def get_channel_title(self):
        return self._channel_title

    def get_custom_title(self):
        return self._custom_title

    async def async_set_channel_title(self, value):
        self.client.asked.append(("set_channel_title", value))

    async def async_set_custom_title(self, value):
        self.client.asked.append(("set_custom_title", value))

    async def async_refresh(self):
        self.refreshed += 1


def _entity(cls, coordinator):
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


# --- the entities -----------------------------------------------------------


def test_the_channel_title_reads_the_coordinators_value():
    entity = _entity(DahuaChannelTitleText, _Coordinator(channel_title="Front Gate"))

    assert entity.native_value == "Front Gate"


def test_the_custom_title_reads_the_coordinators_value():
    entity = _entity(DahuaCustomTitleText, _Coordinator(custom_title="Driveway"))

    assert entity.native_value == "Driveway"


def test_the_fields_are_configuration_with_the_devices_limits():
    channel = _entity(DahuaChannelTitleText, _Coordinator())
    custom = _entity(DahuaCustomTitleText, _Coordinator())

    assert channel.entity_category.value == "config"
    assert channel.native_min == 0
    assert channel.native_max == 64
    assert custom.native_min == 0
    assert custom.native_max == 128


def test_the_unique_ids_are_stable_and_distinct():
    channel = _entity(DahuaChannelTitleText, _Coordinator())
    custom = _entity(DahuaCustomTitleText, _Coordinator())

    assert channel.unique_id == "SERIAL1_channel_title"
    assert custom.unique_id == "SERIAL1_custom_title"


def test_a_field_is_unavailable_until_the_poll_has_a_value():
    c = _Coordinator(channel_title=None)
    assert _entity(DahuaChannelTitleText, c).available is False

    c = _Coordinator(channel_title="")
    assert _entity(DahuaChannelTitleText, c).available is True


async def test_writing_the_channel_title_calls_the_coordinator_and_refreshes():
    c = _Coordinator()
    entity = _entity(DahuaChannelTitleText, c)

    await entity.async_set_value("Front Gate")

    assert c.client.asked == [("set_channel_title", "Front Gate")]
    assert c.refreshed == 1


async def test_writing_the_custom_title_calls_the_coordinator_and_refreshes():
    c = _Coordinator()
    entity = _entity(DahuaCustomTitleText, c)

    await entity.async_set_value("Driveway")

    assert c.client.asked == [("set_custom_title", "Driveway")]
    assert c.refreshed == 1


def test_the_custom_title_text_is_part_of_the_video_widget_capability():
    """The text field shares the VideoWidget probe with the overlay switches,
    so its key has to be part of the same per-channel answer."""
    table = {
        "table.VideoWidget[0].CustomTitle[1].Text": "Driveway",
        "table.VideoWidget[1].CustomTitle[1].Text": "Side Gate",
    }

    assert video_widget_fields(table, 0) == frozenset({"CustomTitle[1].Text"})
    assert video_widget_fields(table, 2) == frozenset()


# --- the coordinator's readers ----------------------------------------------


def _coordinator(**attrs):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 0
    coordinator.data = {}
    coordinator.is_indoor_monitor_without_video = lambda: False
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


def test_the_channel_title_is_this_channels_row():
    coordinator = _coordinator(
        data={
            "table.ChannelTitle[0].Name": "Front Gate",
            "table.ChannelTitle[1].Name": "Back Yard",
        }
    )

    assert coordinator.get_channel_title() == "Front Gate"


def test_a_missing_channel_title_row_is_none_not_a_blank_title():
    assert _coordinator(data={}).get_channel_title() is None


def test_an_empty_channel_title_is_an_empty_string():
    coordinator = _coordinator(data={"table.ChannelTitle[0].Name": ""})

    assert coordinator.get_channel_title() == ""


def test_the_custom_title_is_this_channels_row():
    coordinator = _coordinator(
        data={
            "table.VideoWidget[0].CustomTitle[1].Text": "Driveway",
            "table.VideoWidget[1].CustomTitle[1].Text": "Side Gate",
        }
    )

    assert coordinator.get_custom_title() == "Driveway"


@pytest.mark.parametrize("marker", ["none", "||", " NONE "])
def test_a_device_empty_marker_reads_as_an_empty_title(marker):
    coordinator = _coordinator(
        data={"table.VideoWidget[0].CustomTitle[1].Text": marker}
    )

    assert coordinator.get_custom_title() == ""


def test_a_missing_custom_title_row_is_none_not_a_blank_title():
    assert _coordinator(data={}).get_custom_title() is None


def test_a_monitor_without_a_camera_has_no_title_fields():
    coordinator = _coordinator(
        _supports_channel_title=True,
        _video_widget_fields=frozenset({"CustomTitle[1].Text"}),
        is_indoor_monitor_without_video=lambda: True,
    )

    assert coordinator.supports_channel_title() is False
    assert coordinator.supports_custom_title() is False


# --- the probes -------------------------------------------------------------


def _probe_coordinator(table):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 0
    coordinator.client = _client(table)
    coordinator._probe_refusals = {}
    coordinator._supports_channel_title = False
    return coordinator


async def test_the_probe_reads_this_channels_title_row():
    coordinator = _probe_coordinator({"table.ChannelTitle[0].Name": "Front Gate"})

    assert await coordinator._async_probe_channel_title() is True
    assert coordinator._supports_channel_title is True


async def test_a_table_without_this_channels_row_is_an_answer():
    coordinator = _probe_coordinator({"table.ChannelTitle[1].Name": "Back Yard"})

    assert await coordinator._async_probe_channel_title() is False
    assert coordinator._supports_channel_title is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_an_http_refusal_is_recorded_and_not_retried():
    coordinator = _probe_coordinator(
        ClientResponseError(
            request_info=SimpleNamespace(real_url="http://10.0.0.5/cgi-bin/x.cgi"),
            history=(),
            status=403,
        )
    )

    assert await coordinator._async_probe_channel_title() is False
    assert coordinator._probe_refusals["channel_title"]["status"] == 403
    assert coordinator.probes_that_did_not_answer() == set()


def test_the_channel_title_probe_is_retryable():
    assert "channel_title" in coordinator_module.RETRYABLE_CAPABILITIES


async def test_the_channel_title_fetch_keeps_the_last_answer_on_a_refusal():
    coordinator = _probe_coordinator({})
    coordinator.data = {
        "table.ChannelTitle[0].Name": "Front Gate",
        "unrelated": "x",
    }

    async def _refuse():
        raise TimeoutError()

    coordinator.client.async_get_channel_title = _refuse

    assert await coordinator._async_fetch_channel_title() == {
        "table.ChannelTitle[0].Name": "Front Gate"
    }


# --- which fields an entry creates ------------------------------------------


async def _added_for(coordinator):
    entry = SimpleNamespace(
        entry_id="e1",
        options={},
        runtime_data={0: coordinator},
        async_on_unload=lambda unsub: None,
    )
    added = []
    await text_module.async_setup_entry(
        SimpleNamespace(data={}), entry, adds_entities(added)
    )
    return added


async def test_a_channel_that_reports_both_titles_gets_both_fields():
    added = await _added_for(_Coordinator())

    assert [type(entity) for entity in added] == [
        DahuaChannelTitleText,
        DahuaCustomTitleText,
    ]


async def test_each_field_follows_its_own_capability():
    only_channel = await _added_for(_Coordinator(custom_title_supported=False))
    assert [type(entity) for entity in only_channel] == [DahuaChannelTitleText]

    only_custom = await _added_for(_Coordinator(channel_title_supported=False))
    assert [type(entity) for entity in only_custom] == [DahuaCustomTitleText]


async def test_a_monitor_without_a_camera_gets_no_title_fields():
    assert await _added_for(_Coordinator(no_video=True)) == []
