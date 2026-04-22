"""Tests for custom_components.dahua.dahua_utils."""
from custom_components.dahua.dahua_utils import parse_event


def _wrap_event(event_body: str) -> str:
    """Wrap a raw event body in the --myboundary framing that parse_event expects."""
    return (
        "--myboundary\n"
        "Content-Type: text/plain\n"
        "Content-Length: 999\n"
        "\n"
        f"{event_body}\n"
    )


class TestParseEvent:
    """Tests for parse_event."""

    def test_simple_event_without_data(self):
        """Events with no data payload parse correctly."""
        raw = _wrap_event("Code=VideoMotion;action=Start;index=0")
        events = parse_event(raw)

        assert len(events) == 1
        assert events[0]["Code"] == "VideoMotion"
        assert events[0]["action"] == "Start"
        assert events[0]["index"] == "0"

    def test_event_with_json_data(self):
        """Events with a JSON data payload parse data into a dict."""
        event_body = (
            'Code=VideoMotion;action=Start;index=0;data={\n'
            '   "Id" : [ 0 ],\n'
            '   "RegionName" : [ "Region1" ],\n'
            '   "SmartMotionEnable" : true\n'
            '}'
        )
        raw = _wrap_event(event_body)
        events = parse_event(raw)

        assert len(events) == 1
        assert events[0]["Code"] == "VideoMotion"
        assert isinstance(events[0]["data"], dict)
        assert events[0]["data"]["RegionName"] == ["Region1"]

    def test_unparseable_data_stays_as_string(self):
        """When data is not valid JSON, it remains as a string."""
        raw = _wrap_event("Code=VideoMotion;action=Start;index=0;data=notjson")
        events = parse_event(raw)

        assert len(events) == 1
        assert events[0]["data"] == "notjson"

    def test_empty_input_returns_no_events(self):
        """Empty or non-event input returns an empty list."""
        assert parse_event("") == []
        assert parse_event("some random text") == []

    def test_equals_in_data_value_does_not_crash(self):
        """Bug #477: values containing '=' must not crash parse_event."""
        event_body = (
            'Code=CrossRegionDetection;action=Start;index=0;data={\n'
            '   "Name" : "Rule1",\n'
            '   "Encoded" : "dGVzdA=="\n'
            '}'
        )
        raw = _wrap_event(event_body)
        events = parse_event(raw)

        assert len(events) == 1
        assert events[0]["Code"] == "CrossRegionDetection"

    def test_equals_in_non_json_data_preserved(self):
        """Bug #477: non-JSON data containing '=' is preserved intact."""
        raw = _wrap_event("Code=VideoMotion;action=Start;index=0;data=key=value")
        events = parse_event(raw)

        assert len(events) == 1
        assert events[0]["data"] == "key=value"
