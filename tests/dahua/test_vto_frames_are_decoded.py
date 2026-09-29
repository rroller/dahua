"""A doorbell event with an accent in it used to be dropped in silence.

`data_received` slices `self.buffer`, which is bytes, and hands the packet to
`parse_response`. That did:

    data = str(response)

For bytes that is the *repr*, so a UTF-8 byte becomes the four characters backslash,
x and two hex digits. `\\x` is not a valid JSON escape, so `raw_decode` refuses the
object, `extract_json_objects` steps past it, and the event is gone. No exception, no
log line, nothing on the bus.

ASCII survived by accident, the repr of ASCII bytes being the same characters, which
is why it held up for so long. What it cost was every event carrying a name that is
not ASCII: a card holder, a channel title, a file path.

vto.py was the lowest-covered module in the integration at 31%, and none of these
static methods had a test. They are the whole framing layer -- the parser on the way
in, the header on the way out -- so that is what this covers: a real DHIP header, one
event and two, a truncated tail, bytes that are not UTF-8 at all, and the outgoing
length field that has to agree with the bytes actually written.
"""

import json
import struct

from custom_components.dahua.vto import DahuaVTOClient

# The binary header in front of every frame, taken from the worked examples in
# parse_response's own comments. 0x8c is not a valid UTF-8 start byte, which is why
# the decode has to be errors="replace" rather than strict.
HEADER = b"\x00\x00\x00DHIP\x8c-\x96{\x08\x00\x00\x00{\x01\x00\x00\x00\x00\x00\x00"

# convert_message writes seven fields: two big-endian longs, a double, four
# little-endian longs.
OUTGOING_HEADER_SIZE = 4 + 4 + 8 + 4 + 4 + 4 + 4


def _event(card_name="Front Door", event_id=8):
    return {
        "id": event_id,
        "method": "client.notifyEventStream",
        "params": {"SID": 513, "eventList": [
            {"Action": "Pulse", "Code": "AccessControl",
             "Data": {"CardNo": "1234ABCD", "CardName": card_name}}]},
        "session": 1722306858,
    }


def _frame(*payloads):
    """One packet as the device sends it: header, JSON, newline, per event."""
    out = b""
    for payload in payloads:
        out += HEADER + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    return out


# --- the regression ----------------------------------------------------------

def test_an_ascii_event_parses():
    """The baseline that always worked, so the next test is about the accent and not
    about the frame."""
    messages = DahuaVTOClient.parse_response(_frame(_event()))

    assert [m["id"] for m in messages] == [8]


def test_an_event_with_an_accent_in_it_parses():
    """This is the bug. One u-umlaut in a card holder's name and the whole event was
    dropped: the doorbell press never reached the bus, and nothing said so."""
    messages = DahuaVTOClient.parse_response(
        _frame(_event(card_name="Vorderer Eingang ü")))

    assert [m["id"] for m in messages] == [8], "the event was dropped"
    card = messages[0]["params"]["eventList"][0]["Data"]["CardName"]
    assert card == "Vorderer Eingang ü", "the name survived but changed"


def test_a_name_outside_latin_1_parses_too():
    """Two and three byte sequences, since the failure was per byte."""
    messages = DahuaVTOClient.parse_response(_frame(_event(card_name="大門")))

    assert len(messages) == 1, "the event was dropped"
    assert messages[0]["params"]["eventList"][0]["Data"]["CardName"] == "大門"


# --- the frames a doorbell actually sends ------------------------------------

def test_two_events_in_one_read_both_parse():
    """parse_response's own comment documents this: usually one event per line,
    sometimes two."""
    messages = DahuaVTOClient.parse_response(
        _frame(_event(event_id=8), _event(event_id=9)))

    assert [m["id"] for m in messages] == [8, 9]


def test_the_binary_header_does_not_stop_the_parse():
    """0x8c in the header is not a valid UTF-8 start byte, so a strict decode would
    raise on every single packet -- and the broad except in parse_response would turn
    that into an empty list rather than a traceback. That is why the decode replaces
    rather than strict, and this is the test that stops someone tightening it."""
    packet = _frame(_event())

    assert b"\x8c" in packet
    assert DahuaVTOClient.parse_response(packet)


def test_a_truncated_frame_surrenders_an_inner_object_with_no_id():
    """A frame cut short mid-object, which is what a split TCP read looks like when
    the newline arrives before the rest of the payload.

    The parser does not come back empty here, which is worth writing down. raw_decode
    refuses the truncated outer object, `pos` advances by one character, and the next
    `{` it finds is the complete inner "params" object -- so the caller is handed a
    fragment that was never a message. What keeps that harmless is one line in
    data_received: it reads `message.get("id")`, the fragment has none, and
    `data_handlers.get(None, self.handle_default)` sends it to handle_default instead
    of dispatching it as a reply. Handlers are registered under integer request ids,
    so nothing is ever registered under None.
    """
    whole = _frame(_event())

    messages = DahuaVTOClient.parse_response(whole[:len(whole) - 10])

    # Not a loop over a possibly-empty list: this asserts the fragment is really
    # there, so the "no id" check below cannot pass vacuously.
    assert len(messages) == 1, "expected the inner object, got %s" % (messages,)
    assert "eventList" in messages[0], (
        "expected the params fragment: %s" % (messages[0],))
    assert messages[0].get("id") is None, (
        "a fragment carrying an id would be dispatched as if it were a real reply")


def test_a_frame_truncated_harder_yields_nothing():
    """Cut enough away and no complete object is left, which must come back empty
    rather than raise: this runs inside data_received, the event transport for the
    whole doorbell."""
    whole = _frame(_event())

    assert DahuaVTOClient.parse_response(whole[:len(whole) - 60]) == []


def test_rubbish_yields_nothing_rather_than_raising():
    assert DahuaVTOClient.parse_response(b"\x00\x01\x02 not json at all\n") == []


def test_bytes_that_are_not_utf_8_at_all_do_not_raise():
    """A stream resynchronising after a partial read can hand over anything.
    errors="replace" is what keeps that from taking the connection down."""
    assert DahuaVTOClient.parse_response(b"\xff\xfe\xfd{\"id\":1}\n") == [{"id": 1}]


def test_a_string_still_works():
    """parse_response is a static method and nothing stops a caller passing text.
    The bytes branch is an addition, not a replacement."""
    assert DahuaVTOClient.parse_response('{"id": 4}') == [{"id": 4}]


# --- the object finder underneath -------------------------------------------

def test_nested_objects_are_returned_once_at_the_top_level():
    """raw_decode consumes the whole outer object and the position advances past it,
    so the inner ones are not yielded again as messages of their own."""
    text = '{"id": 1, "params": {"inner": {"deeper": true}}}'

    assert list(DahuaVTOClient.extract_json_objects(text)) == [
        {"id": 1, "params": {"inner": {"deeper": True}}}]


def test_text_around_the_objects_is_skipped():
    text = 'header junk {"id": 1} middle junk {"id": 2} trailing'

    assert list(DahuaVTOClient.extract_json_objects(text)) == [{"id": 1}, {"id": 2}]


# --- the outgoing side, which is the same mistake mirrored -------------------

def test_the_declared_length_matches_the_bytes_actually_written():
    """The incoming bug was characters confused with bytes. convert_message is where
    the same confusion would bite in the other direction: it packs `len(message_data)`
    characters into three header fields and then writes `message_data.encode("utf-8")`
    bytes. Understate that and the device mis-frames the login.

    It is correct today, and for a reason that is easy to undo: `json.dumps` defaults
    to ensure_ascii=True, so a non-ASCII password is written as a \\uXXXX escape and
    the string is pure ASCII, where one character is one byte. Passing
    ensure_ascii=False -- which looks like a tidy-up -- would break the frame for
    exactly the users this change is about. This test is here to fail if someone does
    that.
    """
    for label, password in (("ascii", "secret"),
                            ("non-ascii", "gehört-nicht-mir")):
        message = DahuaVTOClient.convert_message(
            {"method": "global.login", "params": {"password": password}})

        payload = message[OUTGOING_HEADER_SIZE:]
        declared = struct.unpack("<L", message[16:20])[0]

        assert declared == len(payload), (
            "%s: the header declares %d bytes and %d were written"
            % (label, declared, len(payload)))
        # The third length field carries the same value.
        assert struct.unpack("<L", message[24:28])[0] == len(payload), label
        # Round-trips, which is the point of getting the length right.
        assert json.loads(payload.decode("utf-8"))["params"]["password"] == password


def test_the_outgoing_header_is_the_dhip_magic():
    """Byte-reversing this magic once made a framing bug of mine look like patchy
    firmware support, so it is pinned rather than trusted."""
    message = DahuaVTOClient.convert_message({"method": "global.login"})

    assert struct.unpack(">L", message[0:4])[0] == 0x20000000
    assert message[4:8] == b"DHIP"


# --- the login hash ---------------------------------------------------------

def test_the_login_hash_is_two_rounds_of_md5():
    """A golden vector for the challenge response, with credentials that are
    obviously fake. Dahua's scheme is two MD5 rounds in uppercase hex: first
    user:realm:password, then user:random:thatdigest. Nothing public documents it, so
    if a refactor changes it there is no reference to check against. This is that
    reference."""
    digest = DahuaVTOClient._get_hashed_password(
        "1234567890", "Login to 00408C123456", "admin", "not-a-real-password")

    assert digest == "3CAECEC8AF5D91ECECEDC596386944B8"


def test_the_login_hash_depends_on_the_random_and_the_password():
    """Both halves of the challenge feed in, so a stale random or a wrong password
    cannot produce the same answer."""
    args = ("1234567890", "Login to 00408C123456", "admin", "not-a-real-password")
    baseline = DahuaVTOClient._get_hashed_password(*args)

    other_password = DahuaVTOClient._get_hashed_password(*args[:3], "other")
    other_random = DahuaVTOClient._get_hashed_password("9999999999", *args[1:])

    assert other_password != baseline
    assert other_random != baseline


def test_a_non_ascii_password_hashes_over_its_utf_8_bytes():
    """The hash encodes explicitly, so this one was never at risk. Pinned because it
    is the same axis the parser got wrong."""
    digest = DahuaVTOClient._get_hashed_password(
        "1234567890", "Login to 00408C123456", "admin", "gehör")

    assert digest == "BFFCBC496B6A80698D9999F3CA6FB751"
