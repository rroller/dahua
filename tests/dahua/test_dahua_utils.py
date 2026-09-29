from custom_components.dahua.dahua_utils import (
    parse_event,
    extract_plate_data,
    normalize_plate,
    parse_authorized_plates,
)


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
        assert events[0]["data"]["Encoded"] == "dGVzdA=="

    def test_equals_in_non_json_data_preserved(self):
        """Bug #477: non-JSON data containing '=' is preserved intact."""
        raw = _wrap_event("Code=VideoMotion;action=Start;index=0;data=key=value")
        events = parse_event(raw)

        assert len(events) == 1
        assert events[0]["data"] == "key=value"

    # --- a block the stream cut short --------------------------------------
    #
    # stream_events passes on whatever response.content.iter_chunks() hands it,
    # so a chunk boundary can fall anywhere -- including immediately after a
    # part's headers. Such a block has three newline-separated pieces, and the
    # guard that admitted it asked for three before reading the fourth.

    def test_a_block_that_ends_after_its_headers_is_skipped(self):
        """It used to raise IndexError, which ended the event stream."""
        raw = "--myboundary\nContent-Type: text/plain\nContent-Length: 999\n"

        assert parse_event(raw) == []

    def test_a_truncated_block_does_not_discard_the_events_before_it(self):
        """One bad block must cost one block, not the whole batch."""
        raw = (_wrap_event("Code=VideoMotion;action=Start;index=0")
               + "--myboundary\nContent-Type: text/plain\nContent-Length: 999\n")

        events = parse_event(raw)

        assert len(events) == 1
        assert events[0]["Code"] == "VideoMotion"

    # --- a fragment that is not key=value -----------------------------------

    def test_a_semicolon_inside_the_json_does_not_end_the_stream(self):
        """Users name rules and regions freely, and the payload is split on ';'."""
        event_body = (
            'Code=CrossRegionDetection;action=Start;index=0;data={\n'
            '   "Name" : "Drive; Gate"\n'
            '}'
        )

        events = parse_event(_wrap_event(event_body))

        assert len(events) == 1
        assert events[0]["Code"] == "CrossRegionDetection"
        assert events[0]["action"] == "Start"
        assert events[0]["index"] == "0"

    def test_a_fragment_that_is_not_a_pair_is_skipped(self):
        raw = _wrap_event("Code=VideoMotion;action=Start;garbage;index=0")

        events = parse_event(raw)

        assert len(events) == 1
        assert events[0] == {"Code": "VideoMotion", "action": "Start", "index": "0"}


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
        # Greek letters: Alpha (Α), Beta (Β), Epsilon (Ε)
        event = {
            "Code": "Traffic",
            "data": {
                "PlateNumber": "ΑΒΕ1234",
            },
        }
        res = extract_plate_data(event)
        assert res is not None
        assert res["plate"] == "ABE1234"

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


class TestNormalizePlate:
    """Tests for normalize_plate."""

    def test_basic_normalization(self):
        assert normalize_plate("abc-1234") == "ABC1234"
        assert normalize_plate("  ABC 1234  ") == "ABC1234"
        assert normalize_plate("XYZ_5678") == "XYZ5678"

    def test_homoglyph_replacement(self):
        # Greek letters: Alpha, Beta, Epsilon, Zeta, Eta, Iota, Kappa, Mu, Nu, Omicron, Rho (looks like P), Tau, Upsilon, Chi
        assert normalize_plate("ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ") == "ABEZHIKMNOPTYX"
        assert normalize_plate("ΑΒΟ-1234") == "ABO1234"
        assert normalize_plate("ΧΥΖ 5678") == "XYZ5678"

    def test_empty_and_none(self):
        assert normalize_plate("") == ""
        assert normalize_plate(None) == ""
        assert normalize_plate("---") == ""


    def test_greek_zero_omicron_positional_normalization(self):
        # 3 letters + 4 digits: zero in letter section becomes O, O in digit section becomes 0
        assert normalize_plate("AB01234") == "ABO1234"
        assert normalize_plate("XYZ567O") == "XYZ5670"
        assert normalize_plate("0BC1234") == "OBC1234"
        assert normalize_plate("AB05678") == "ABO5678"


class TestParseAuthorizedPlates:
    """Tests for parse_authorized_plates."""

    def test_comma_separated_string(self):
        raw = "ABC1234, XYZ-5678, MNO9999"
        result = parse_authorized_plates(raw)
        assert result == ["ABC1234", "XYZ5678", "MNO9999"]

    def test_deduplication_and_normalization(self):
        raw = "ABC-1234, abc1234,   ABC 1234, ΑΒΟ1234 "
        result = parse_authorized_plates(raw)
        assert result == ["ABC1234", "ABO1234"]

    def test_list_input(self):
        plates = ["ABC-1234", "xyz-5678"]
        result = parse_authorized_plates(plates)
        assert result == ["ABC1234", "XYZ5678"]

    def test_empty_input(self):
        assert parse_authorized_plates("") == []
        assert parse_authorized_plates(None) == []
        assert parse_authorized_plates("  ,  ,  ") == []


class TestExtractPlateStringFallback:
    """Tests for string fallback and candidate ranking in extract_plate_data."""

    def test_extract_from_valid_json_string(self):
        event = {
            "Code": "TrafficParkingSpaceParking",
            "data": '{"Object": {"ObjectType": "Plate", "Text": "AB01234", "Confidence": 94}}'
        }
        res = extract_plate_data(event)
        assert res is not None
        assert res["plate"] == "ABO1234"
        assert res["confidence"] == 94

    def test_extract_from_truncated_json_string(self):
        # Truncated string simulating TCP packet split
        truncated = '{"Object": {"ObjectType": "Plate", "Text": "AB01234", "Confidence": 90}, "TrafficCa'
        event = {
            "Code": "TrafficParkingSpaceParking",
            "data": truncated
        }
        res = extract_plate_data(event)
        assert res is not None
        assert res["plate"] == "ABO1234"
        assert res["confidence"] == 90

    def test_extract_from_rtl_reversed_candidates(self):
        # When Dahua includes both RTL scrambled and standard order
        raw = '{"CurrentPlateInfo": [{"Text": "12340BA"}], "Object": {"Text": "AB01234", "Confidence": 91}}'
        event = {
            "Code": "TrafficParkingSpaceParking",
            "data": raw
        }
        res = extract_plate_data(event)
        assert res is not None
        assert res["plate"] == "ABO1234"




class TestTruncatedPlateFallback:
    """The regex fallback, reached only when the payload is not valid JSON.

    An ANPR event carries a lot of JSON and arrives over a stream that splits on TCP
    boundaries, so a truncated payload is ordinary rather than exceptional. When
    json.loads fails, extract_plate_data hands the raw text to a regex extractor instead
    of giving up, and that extractor had almost no coverage.

    Worth noting why: the RTL test above passes *valid* JSON, so it goes down the parsed
    path and never reaches the regex at all. Everything here is deliberately unparseable.
    """

    def test_a_truncated_payload_still_yields_the_plate_number(self):
        truncated = '{"TrafficCar": {"PlateNumber": "ABC1234", "Speed": 4'
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res is not None
        assert res["plate"] == "ABC1234"

    def test_the_lowercase_spelling_is_accepted_too(self):
        """Firmware differs on the case of this key."""
        truncated = '{"trafficCar": {"plateNumber": "ABC1234", "Speed'
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res is not None
        assert res["plate"] == "ABC1234"

    def test_a_greek_format_candidate_beats_one_found_earlier(self):
        """Three letters then four digits is the exact shape, so it wins outright.

        The order is the whole test. "AB12" comes first and the last-resort branch
        accepts it, but it does not stop looking; the exact shape later does, and takes
        over. Without that preference the first plausible candidate in the payload wins,
        and a payload has several -- ObjectType, colours and brands all arrive as Text.
        """
        truncated = '{"a": {"Text": "AB12"}, "b": {"Text": "ABC1234"}, "c'
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res is not None
        assert res["plate"] == "ABC1234"

    def test_the_exact_shape_stops_the_search(self):
        """And nothing after it can take it back."""
        truncated = '{"a": {"Text": "ABC1234"}, "b": {"Text": "ZZ99"}, "c'
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res["plate"] == "ABC1234"

    def test_a_plate_the_device_sent_back_to_front_is_turned_round(self):
        """Some firmware emits the plate right to left, so four digits arrive before
        three letters. Flipped back, it is a valid plate; left alone it is not, and the
        authorized list would never match it."""
        truncated = '{"CurrentPlateInfo": [{"Text": "1234ABC"}], "Vehicl'
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res is not None
        assert res["plate"] == "ABC1234"

    def test_something_that_is_neither_shape_is_still_used(self):
        """Last resort: anything with both a letter and a digit in it beats reporting no
        plate at all, because plate formats vary by country and this integration is not
        the place to enumerate them."""
        truncated = '{"x": {"Text": "AB12"}, "y'
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res is not None
        assert res["plate"] == "AB12"

    def test_a_candidate_with_no_digits_is_not_a_plate(self):
        """Otherwise every "Text" in the payload is a candidate, and they are not all
        plates: ObjectType, vehicle colours and brands all arrive as Text."""
        truncated = '{"x": {"Text": "Sedan"}, "y": {"Text": "Black"}, "z'
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res is None

    def test_a_truncated_payload_with_nothing_plate_shaped_gives_nothing(self):
        res = extract_plate_data({"Code": "TrafficJunction", "data": '{"Speed": 4'})

        assert res is None

    def test_the_confidence_comes_out_of_a_truncated_payload_too(self):
        truncated = '{"TrafficCar": {"PlateNumber": "ABC1234"}, "Confidence": 87, "Ve'
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res["confidence"] == 87

    def test_the_brand_and_colour_survive_truncation(self):
        truncated = ('{"TrafficCar": {"PlateNumber": "ABC1234"}, "VehicleSign": "Audi",'
                     ' "VehicleColor": "Black", "Vehic')
        res = extract_plate_data({"Code": "TrafficJunction", "data": truncated})

        assert res["vehicle_brand"] == "Audi"
        assert res["vehicle_color"] == "Black"


class TestPlateInItsOtherShapes:
    """`Plate` arrives as a dict on some firmware and as a bare string on others, and
    neither had a test. Both are checked only when Object did not supply a plate, so
    getting the order wrong silently prefers the wrong field."""

    def test_plate_as_a_dict_with_its_own_confidence(self):
        event = {"Code": "TrafficJunction",
                 "data": {"Plate": {"Text": "ABC1234", "Confidence": 77}}}
        res = extract_plate_data(event)

        assert res["plate"] == "ABC1234"
        assert res["confidence"] == 77

    def test_plate_as_a_bare_string(self):
        res = extract_plate_data(
            {"Code": "TrafficJunction", "data": {"Plate": "ABC1234"}})

        assert res["plate"] == "ABC1234"

    def test_the_lowercase_key_is_accepted(self):
        res = extract_plate_data(
            {"Code": "TrafficJunction", "data": {"plate": "ABC1234"}})

        assert res["plate"] == "ABC1234"

    def test_a_plate_field_holding_a_placeholder_is_not_a_plate(self):
        """The same ignored list the Object branch uses, so a device that fills the field
        in with "unlicensed" does not produce a sensor reading of that."""
        for placeholder in ("unlicensed", "unknown", "none", "--", ""):
            res = extract_plate_data(
                {"Code": "TrafficJunction", "data": {"Plate": placeholder}})
            assert res is None, placeholder


class TestPlateDataEdges:
    """The remaining answers a device can give."""

    def test_data_that_is_neither_text_nor_a_mapping_is_not_a_plate(self):
        for value in (42, [], None, 3.5):
            assert extract_plate_data({"Code": "X", "data": value}) is None, value

    def test_a_plate_of_only_punctuation_is_not_a_plate(self):
        """normalize_plate strips to alphanumerics, so a field holding separators comes
        back empty and must not become a reading."""
        res = extract_plate_data(
            {"Code": "X", "data": {"Object": {"ObjectType": "Plate", "Text": "-/-"}}})

        assert res is None

    def test_the_vehicle_type_falls_back_to_the_vehicle_text(self):
        """Some firmware puts the body style in Vehicle.Text rather than in a named
        field."""
        event = {"Code": "X", "data": {
            "Plate": "ABC1234",
            "Vehicle": {"Text": "Estate"},
        }}
        res = extract_plate_data(event)

        assert res["vehicle_type"] == "Estate"


class TestAuthorizedPlatesEdges:
    def test_something_that_is_neither_a_list_nor_a_string_is_no_list(self):
        """The option comes from a config entry, so a hand-edited .storage can put
        anything here. Returning an empty list means nothing is authorized, which is the
        safe direction for a gate."""
        assert parse_authorized_plates(42) == []
        assert parse_authorized_plates({"a": 1}) == []
