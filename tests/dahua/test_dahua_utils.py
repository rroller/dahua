from custom_components.dahua.dahua_utils import parse_event, extract_plate_data


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


class TestExtractPlateData:
    """Tests for extract_plate_data."""

    def test_extract_plate_wizmind_object(self):
        """Extract plate from WizMind Object schema with confidence and attributes."""
        event = {
            "Code": "TrafficSnapshot",
            "data": {
                "Object": {
                    "ObjectType": "Plate",
                    "Text": "AB123CD",
                    "Confidence": 98,
                },
                "Vehicle": {
                    "Category": "SaloonCar",
                    "MainColor": "Blue",
                    "Brand": "BMW",
                    "SubBrand": "3-Series",
                },
                "Direction": "Approach",
            },
        }
        res = extract_plate_data(event)
        assert res is not None
        assert res["plate"] == "AB123CD"
        assert res["confidence"] == 98
        assert res["vehicle_type"] == "SaloonCar"
        assert res["vehicle_color"] == "Blue"
        assert res["vehicle_brand"] == "BMW"
        assert res["vehicle_series"] == "3-Series"
        assert res["direction"] == "Approach"

    def test_extract_plate_traffic_car_schema(self):
        """Extract plate from TrafficCar schema."""
        event = {
            "Code": "TrafficParkingSpaceParking",
            "data": {
                "TrafficCar": {
                    "PlateNumber": "XYZ-9988",
                    "VehicleType": "SUV",
                    "VehicleColor": "Black",
                    "Brand": "Volkswagen",
                    "Direction": "Approach",
                }
            },
        }
        res = extract_plate_data(event)
        assert res is not None
        assert res["plate"] == "XYZ9988"
        assert res["vehicle_type"] == "SUV"
        assert res["vehicle_color"] == "Black"
        assert res["vehicle_brand"] == "Volkswagen"

    def test_homoglyph_conversion(self):
        """Greek/Cyrillic characters visually matching Latin are normalized."""
        # Greek letters: Chi (Χ), Zeta (Ζ), Omicron (Ο)
        event = {
            "Code": "Traffic",
            "data": {
                "PlateNumber": "ΧΖΟ3314",
            },
        }
        res = extract_plate_data(event)
        assert res is not None
        assert res["plate"] == "XZO3314"

    def test_unlicensed_and_empty_ignored(self):
        """Placeholder values like 'unlicensed', 'unknown', or non-plate objects return None."""
        assert extract_plate_data({"data": {"PlateNumber": "unlicensed"}}) is None
        assert extract_plate_data({"data": {"PlateNumber": "unknown"}}) is None
        assert extract_plate_data({"data": {"PlateNumber": "null"}}) is None
        assert extract_plate_data({"data": {"PlateNumber": "--"}}) is None
        assert extract_plate_data({"data": {"PlateNumber": ""}}) is None
        assert extract_plate_data({"data": {"Object": {"ObjectType": "Human", "Text": "Unknown"}}}) is None
        assert extract_plate_data({"data": {"Object": {"ObjectType": "Vehicle", "Text": "Unknown"}}}) is None
        assert extract_plate_data({"data": {}}) is None
        assert extract_plate_data("not a dict") is None

    def test_traffic_junction_schema_issue_215(self):
        """Schema from community issue #215 parses correctly."""
        event = {
            "Code": "TrafficJunction",
            "action": "Stop",
            "data": {
                "Object": {
                    "ObjectType": "Plate",
                    "Text": "TOY1234",
                },
                "Vehicle": {
                    "Category": "SaloonCar",
                    "Text": "Toyota",
                    "MainColor": [128, 128, 128, 0],
                },
            },
        }
        res = extract_plate_data(event)
        assert res is not None
        assert res["plate"] == "TOY1234"
        assert res["vehicle_type"] == "SaloonCar"
        assert res["vehicle_brand"] == "Toyota"

