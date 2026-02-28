"""Tests for dahua_utils module."""

from custom_components.dahua.dahua_utils import (
    dahua_brightness_to_hass_brightness,
    hass_brightness_to_dahua_brightness,
    parse_event,
)


class TestDahuaBrightnessToHass:
    def test_max_brightness(self):
        assert dahua_brightness_to_hass_brightness("100") == 255

    def test_zero_brightness(self):
        assert dahua_brightness_to_hass_brightness("0") == 0

    def test_mid_brightness(self):
        assert dahua_brightness_to_hass_brightness("50") == 127

    def test_none_defaults_to_100(self):
        assert dahua_brightness_to_hass_brightness(None) == 255

    def test_empty_string_defaults_to_100(self):
        assert dahua_brightness_to_hass_brightness("") == 255


class TestHassBrightnessToDahua:
    def test_max_brightness(self):
        assert hass_brightness_to_dahua_brightness(255) == 100

    def test_zero_brightness(self):
        assert hass_brightness_to_dahua_brightness(0) == 0

    def test_none_defaults_to_100(self):
        assert hass_brightness_to_dahua_brightness(None) == 39

    def test_mid_brightness(self):
        assert hass_brightness_to_dahua_brightness(128) == 50


class TestParseEvent:
    def test_single_event(self):
        data = (
            "--myboundary\n"
            "Content-Type: text/plain\n"
            "Content-Length: 50\n"
            "\n"
            "Code=VideoMotion;action=Start;index=0"
        )
        events = parse_event(data)
        assert len(events) == 1
        assert events[0]["Code"] == "VideoMotion"
        assert events[0]["action"] == "Start"
        assert events[0]["index"] == "0"

    def test_event_with_json_data(self):
        data = (
            "--myboundary\n"
            "Content-Type: text/plain\n"
            "Content-Length: 100\n"
            "\n"
            "Code=VideoMotion;action=Start;index=0;data={\n"
            '   "Id" : [ 0 ],\n'
            '   "RegionName" : [ "Region1" ]\n'
            "}"
        )
        events = parse_event(data)
        assert len(events) == 1
        assert events[0]["data"]["Id"] == [0]

    def test_empty_string(self):
        events = parse_event("")
        assert events == []

    def test_no_boundary(self):
        events = parse_event("some random text")
        assert events == []

    def test_malformed_no_code(self):
        data = (
            "--myboundary\n"
            "Content-Type: text/plain\n"
            "Content-Length: 10\n"
            "\n"
            "NotACode"
        )
        events = parse_event(data)
        assert events == []

    def test_event_with_invalid_json_data(self):
        """Invalid JSON in data field is silently ignored (lines 79-80)."""
        data = (
            "--myboundary\n"
            "Content-Type: text/plain\n"
            "Content-Length: 80\n"
            "\n"
            "Code=VideoMotion;action=Start;index=0;data={invalid json"
        )
        events = parse_event(data)
        assert len(events) == 1
        assert events[0]["Code"] == "VideoMotion"
        # data remains as the invalid string, not parsed as JSON
        assert isinstance(events[0]["data"], str)
