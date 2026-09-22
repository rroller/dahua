"""The event starts where Code= is, not on the fourth line.

#587, reported by @jaaneo on a DHI-TPC-BF1241. That camera is visual and
thermal in one body, and its thermal channel does not send the same header
shape as its visual one. parse_event skipped exactly three lines and required
the fourth to begin the event:

    s = event_block.split("\\n", 3)
    if len(s) < 4:
        continue
    event_block = s[3].strip()
    if not event_block.startswith("Code="):
        continue

A block with one header line fewer fell into the `len(s) < 4` guard and was
dropped, so those events never arrived and nothing logged a reason.

Every existing fixture uses the four-line shape, which is why the suite was
green throughout. These use the shapes that were not covered.
"""
from custom_components.dahua import dahua_utils


def _block(header_lines, code="VideoMotion", index=0):
    """One multipart block with a chosen number of header lines."""
    headers = "".join("%s\r\n" % h for h in header_lines)
    return ("--myboundary\r\n{0}\r\n"
            "Code={1};action=Start;index={2}\r\n").format(headers, code, index)


FOUR_LINE = ["Content-Type: text/plain", "Content-Length: 40"]
THREE_LINE = ["Content-Type: text/plain"]
FIVE_LINE = ["Content-Type: text/plain", "Content-Length: 40", "X-Vendor: 1"]


def test_the_usual_four_line_header_still_works():
    events = dahua_utils.parse_event(_block(FOUR_LINE))

    assert [e["Code"] for e in events] == ["VideoMotion"]


def test_one_header_line_fewer_is_not_dropped():
    """The #587 case. This is what the thermal channel sends."""
    events = dahua_utils.parse_event(
        _block(THREE_LINE, code="CrossLineDetection", index=1))

    assert [e["Code"] for e in events] == ["CrossLineDetection"], \
        "a block with three header lines was dropped"


def test_one_header_line_more_is_not_dropped_either():
    events = dahua_utils.parse_event(
        _block(FIVE_LINE, code="CrossRegionDetection", index=1))

    assert [e["Code"] for e in events] == ["CrossRegionDetection"]


def test_the_rest_of_the_event_still_comes_with_it():
    """Finding the start must not cost the fields after it."""
    events = dahua_utils.parse_event(
        _block(THREE_LINE, code="CrossLineDetection", index=3))

    assert events[0]["action"] == "Start"
    assert events[0]["index"] == "3"


def test_mixed_shapes_in_one_payload_all_arrive():
    """A hybrid camera sends both on the one stream."""
    payload = (_block(FOUR_LINE, code="VideoMotion", index=0)
               + _block(THREE_LINE, code="CrossLineDetection", index=1))

    events = dahua_utils.parse_event(payload)

    assert [e["Code"] for e in events] == ["VideoMotion", "CrossLineDetection"]


# --- what must still be skipped ---------------------------------------------

def test_a_block_that_stops_inside_its_headers_is_skipped():
    """A chunk can end anywhere, so this is ordinary rather than exceptional.

    Raising here took the whole stream down for every camera on the host (#475).
    """
    assert dahua_utils.parse_event("--myboundary\r\nContent-Ty") == []


def test_headers_with_no_event_are_skipped():
    assert dahua_utils.parse_event(
        "--myboundary\r\nContent-Type: text/plain\r\n\r\n") == []


def test_an_empty_payload_is_skipped():
    assert dahua_utils.parse_event("") == []
