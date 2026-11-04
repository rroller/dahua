"""The picture controls that live beside Day/Night, and the overlay switches.

Ported from the Intelbras fork's 1.14.0/1.14.1 work. Two things are worth
pinning here that a per-entity test would miss:

* VideoInOptions and VideoWidget are host-wide tables indexed by channel, so a
  200 for the table says nothing about whether *this* channel is in it. Both
  readers are judged on this channel's own keys, and the neighbouring-channel
  case is the realistic defect.
* A value the mapping does not recognise is None rather than a confident word.
  WDR mode 2 is not called Auto because the vendor enum was not confirmed.
"""

from types import SimpleNamespace

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.coordinator import (
    DahuaDataUpdateCoordinator,
    WIDE_DYNAMIC_RANGE_OPTIONS,
    image_option_fields,
    video_widget_fields,
)


def _coordinator(**attrs):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 0
    coordinator.data = {}
    coordinator.is_indoor_monitor_without_video = lambda: False
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


# --- the pure readers -------------------------------------------------------


def test_image_options_are_read_from_this_channels_own_row():
    table = {
        "table.VideoInOptions[0].WideDynamicRangeMode": "1",
        "table.VideoInOptions[0].WideDynamicRange": "50",
        "table.VideoInOptions[0].ExposureMode": "0",
        "table.VideoInOptions[1].WideDynamicRangeMode": "2",
        "table.VideoInOptions[1].ExposureMode": "1",
    }

    assert image_option_fields(table, 0) == frozenset(
        {"WideDynamicRangeMode", "WideDynamicRange", "ExposureMode"}
    )
    assert image_option_fields(table, 2) == frozenset()


def test_image_options_are_per_field():
    """A camera can serve WDR without an exposure mode, and the WDR mode
    without the level it needs. One yes cannot say which entity it warrants."""
    table = {"table.VideoInOptions[0].WideDynamicRangeMode": "1"}

    assert image_option_fields(table, 0) == frozenset({"WideDynamicRangeMode"})


def test_an_empty_video_in_options_table_names_no_controls():
    assert image_option_fields({}, 0) == frozenset()


def test_video_widget_fields_are_read_from_this_channels_own_row():
    table = {
        "table.VideoWidget[0].TimeTitle.EncodeBlend": "true",
        "table.VideoWidget[0].ChannelTitle.EncodeBlend": "false",
        "table.VideoWidget[1].TimeTitle.EncodeBlend": "false",
    }

    assert video_widget_fields(table, 0) == frozenset(
        {"TimeTitle.EncodeBlend", "ChannelTitle.EncodeBlend"}
    )
    assert video_widget_fields(table, 2) == frozenset()


def test_the_custom_title_field_is_group_one():
    table = {"table.VideoWidget[0].CustomTitle[1].EncodeBlend": "true"}

    assert video_widget_fields(table, 0) == frozenset({"CustomTitle[1].EncodeBlend"})


# --- the coordinator's words ------------------------------------------------


def test_the_wdr_option_is_the_word_the_select_offers():
    coordinator = _coordinator(
        data={"table.VideoInOptions[0].WideDynamicRangeMode": "2"}
    )

    assert (
        coordinator.get_wide_dynamic_range_option() == WIDE_DYNAMIC_RANGE_OPTIONS["2"]
    )
    assert coordinator.get_wide_dynamic_range_option() == "Mode 2"


def test_an_unrecognised_wdr_mode_is_not_given_a_word():
    coordinator = _coordinator(
        data={"table.VideoInOptions[0].WideDynamicRangeMode": "4"}
    )

    assert coordinator.get_wide_dynamic_range_option() is None


def test_the_exposure_option_is_the_word_the_select_offers():
    coordinator = _coordinator(data={"table.VideoInOptions[0].ExposureMode": "1"})

    assert coordinator.get_exposure_mode_option() == "Gain Priority"


def test_an_unrecognised_exposure_mode_is_not_given_a_word():
    coordinator = _coordinator(data={"table.VideoInOptions[0].ExposureMode": "9"})

    assert coordinator.get_exposure_mode_option() is None


def test_the_wdr_level_is_an_integer_and_none_when_absent():
    assert (
        _coordinator(
            data={"table.VideoInOptions[0].WideDynamicRange": "50"}
        ).get_wide_dynamic_range_level()
        == 50
    )
    assert _coordinator(data={}).get_wide_dynamic_range_level() is None


def test_a_control_is_only_supported_on_the_channel_that_reported_it():
    coordinator = _coordinator(_image_option_fields=frozenset({"WideDynamicRangeMode"}))

    assert coordinator.supports_image_option("WideDynamicRangeMode") is True
    assert coordinator.supports_image_option("ExposureMode") is False
    assert coordinator.supports_image_options() is True


def test_a_monitor_without_a_camera_reports_no_picture_controls():
    coordinator = _coordinator(
        _image_option_fields=frozenset({"WideDynamicRangeMode"}),
        is_indoor_monitor_without_video=lambda: True,
    )

    assert coordinator.supports_image_option("WideDynamicRangeMode") is False
    assert coordinator.supports_image_options() is False


# --- the overlay visibility readers -----------------------------------------


def test_an_overlay_reads_as_on_only_for_true():
    coordinator = _coordinator(
        data={"table.VideoWidget[0].TimeTitle.EncodeBlend": "true"}
    )

    assert coordinator.is_video_widget_overlay_on("TimeTitle.EncodeBlend") is True
    assert coordinator.is_video_widget_overlay_on("ChannelTitle.EncodeBlend") is None


def test_an_overlay_reads_as_off_for_false():
    coordinator = _coordinator(
        data={"table.VideoWidget[0].TimeTitle.EncodeBlend": "false"}
    )

    assert coordinator.is_video_widget_overlay_on("TimeTitle.EncodeBlend") is False


def test_an_overlay_reads_as_unknown_when_the_poll_has_no_value():
    assert _coordinator().is_video_widget_overlay_on("TimeTitle.EncodeBlend") is None


def test_an_unrecognised_overlay_spelling_is_unknown_rather_than_off():
    """A value this mapping does not cover is None, not a confident "hidden".
    `get_video_image_control` reads its booleans the same way."""
    coordinator = _coordinator(data={"table.VideoWidget[0].TimeTitle.EncodeBlend": "1"})

    assert coordinator.is_video_widget_overlay_on("TimeTitle.EncodeBlend") is None


def test_only_the_probed_overlay_fields_are_supported():
    coordinator = _coordinator(
        _video_widget_fields=frozenset({"TimeTitle.EncodeBlend"})
    )

    assert coordinator.supports_video_widget_overlay("TimeTitle.EncodeBlend") is True
    assert (
        coordinator.supports_video_widget_overlay("ChannelTitle.EncodeBlend") is False
    )


def test_a_monitor_without_a_camera_reports_no_overlay_switches():
    coordinator = _coordinator(
        _video_widget_fields=frozenset({"TimeTitle.EncodeBlend"}),
        is_indoor_monitor_without_video=lambda: True,
    )

    assert coordinator.supports_video_widget_overlay("TimeTitle.EncodeBlend") is False
    assert coordinator.reads_video_widget() is False


def test_a_channel_with_overlays_reads_them():
    coordinator = _coordinator(
        _video_widget_fields=frozenset({"TimeTitle.EncodeBlend"})
    )

    assert coordinator.reads_video_widget() is True


# --- the probes -------------------------------------------------------------


class _Client:
    def __init__(self, table):
        self.table = table
        self.asked = []

    async def async_probe_video_in_options(self):
        self.asked.append("video_in_options")
        return self.table

    async def async_get_video_in_options(self):
        self.asked.append("video_in_options_poll")
        return self.table

    async def async_get_video_widget(self):
        self.asked.append("video_widget")
        return self.table


def _probe_coordinator(table):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 0
    coordinator.client = _Client(table)
    coordinator._probe_refusals = {}
    coordinator._supports_day_night_color = False
    return coordinator


async def test_the_day_night_probe_also_collects_the_image_options():
    """One read, two answers: the same VideoInOptions table carries both, so the
    WDR and exposure selects cost no extra request."""
    coordinator = _probe_coordinator(
        {
            "table.VideoInOptions[0].DayNightColor": "0",
            "table.VideoInOptions[0].WideDynamicRangeMode": "1",
            "table.VideoInOptions[0].WideDynamicRange": "50",
            "table.VideoInOptions[0].ExposureMode": "0",
        }
    )

    await coordinator._async_probe_day_night_color()

    assert coordinator._supports_day_night_color is True
    assert coordinator._image_option_fields == frozenset(
        {"WideDynamicRangeMode", "WideDynamicRange", "ExposureMode"}
    )
    assert coordinator.client.asked == ["video_in_options"]


async def test_the_day_night_probe_clears_image_options_when_refused():
    from aiohttp import ClientResponseError

    coordinator = _probe_coordinator({})
    coordinator._image_option_fields = frozenset({"WideDynamicRangeMode"})

    async def _refuse():
        raise ClientResponseError(
            request_info=SimpleNamespace(real_url="http://10.0.0.5/cgi-bin/x.cgi"),
            history=(),
            status=400,
        )

    coordinator.client.async_probe_video_in_options = _refuse

    assert await coordinator._async_probe_day_night_color() is False
    assert coordinator._image_option_fields == frozenset()
    assert coordinator._probe_refusals["day_night_color"]["status"] == 400


async def test_the_video_widget_probe_keeps_only_this_channels_fields():
    coordinator = _probe_coordinator(
        {
            "table.VideoWidget[0].TimeTitle.EncodeBlend": "true",
            "table.VideoWidget[1].ChannelTitle.EncodeBlend": "true",
        }
    )

    assert await coordinator._async_probe_video_widget() is True
    assert coordinator._video_widget_fields == frozenset({"TimeTitle.EncodeBlend"})


# --- the retry registry and the poll wrappers --------------------------------


def test_the_video_widget_probe_is_retryable():
    """A device that refused VideoWidget at setup must be asked again later,
    or the switches only appear after a reload (#1043/#1057)."""
    assert "video_widget" in coordinator_module.RETRYABLE_CAPABILITIES


async def test_the_video_widget_retry_does_not_announce_the_same_answer_twice():
    coordinator = _probe_coordinator(
        {"table.VideoWidget[0].TimeTitle.EncodeBlend": "true"}
    )
    coordinator._video_widget_fields = frozenset({"TimeTitle.EncodeBlend"})

    assert await coordinator._async_retry_video_widget() is False


async def test_the_day_night_retry_sees_a_new_image_option_beside_an_old_answer():
    """The two families share one probe but are compared separately: a channel
    that already had Day/Night and now reports ExposureMode must still be
    announced, or its select is never built until a reload."""
    coordinator = _probe_coordinator(
        {
            "table.VideoInOptions[0].DayNightColor": "0",
            "table.VideoInOptions[0].WideDynamicRangeMode": "1",
            "table.VideoInOptions[0].WideDynamicRange": "50",
            "table.VideoInOptions[0].ExposureMode": "0",
        }
    )
    coordinator._supports_day_night_color = True
    coordinator._image_option_fields = frozenset({"WideDynamicRangeMode"})

    assert await coordinator._async_retry_day_night_color() is True


async def test_the_day_night_retry_does_not_announce_an_unchanged_answer():
    coordinator = _probe_coordinator(
        {
            "table.VideoInOptions[0].DayNightColor": "0",
            "table.VideoInOptions[0].WideDynamicRangeMode": "1",
            "table.VideoInOptions[0].WideDynamicRange": "50",
        }
    )
    coordinator._supports_day_night_color = True
    coordinator._image_option_fields = frozenset(
        {"WideDynamicRangeMode", "WideDynamicRange"}
    )

    assert await coordinator._async_retry_day_night_color() is False


async def test_the_video_in_options_fetch_keeps_the_last_answer_on_a_refusal():
    coordinator = _probe_coordinator({})
    coordinator.data = {
        "table.VideoInOptions[0].WideDynamicRangeMode": "1",
        "table.VideoInOptions[0].WideDynamicRange": "50",
        "unrelated": "x",
    }

    async def _refuse():
        raise TimeoutError()

    coordinator.client.async_get_video_in_options = _refuse

    assert await coordinator._async_fetch_video_in_options() == {
        "table.VideoInOptions[0].WideDynamicRangeMode": "1",
        "table.VideoInOptions[0].WideDynamicRange": "50",
    }


async def test_the_video_widget_fetch_keeps_the_last_answer_on_a_refusal():
    coordinator = _probe_coordinator({})
    coordinator.data = {
        "table.VideoWidget[0].TimeTitle.EncodeBlend": "true",
        "unrelated": "x",
    }

    async def _refuse():
        raise TimeoutError()

    coordinator.client.async_get_video_widget = _refuse

    assert await coordinator._async_fetch_video_widget() == {
        "table.VideoWidget[0].TimeTitle.EncodeBlend": "true"
    }


async def test_a_fetch_with_nothing_to_carry_returns_none():
    coordinator = _probe_coordinator({})

    async def _refuse():
        raise TimeoutError()

    coordinator.client.async_get_video_widget = _refuse

    assert await coordinator._async_fetch_video_widget() is None
