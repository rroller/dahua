"""Pulling one event out of the multipart stream, as soon as it is complete.

The event stream is a multipart body that never ends. Parts arrive split across TCP
chunks, so the client buffers and pulls off whole parts. Dahua includes Content-Length on
each one, and once that many payload bytes are buffered the part is complete and can be
delivered immediately. Waiting for the next boundary instead costs up to one heartbeat
interval of latency on every event, which on a doorbell is the difference between a
notification arriving with the ring and after it.

Devices that omit Content-Length keep the older next-boundary behaviour, so both paths
have to work and both are here.

The subtle one is what happens when the two disagree. If the next boundary turns up
*before* the declared payload would end, the length was wrong or the payload was
truncated, and the framing is the stronger evidence: believing the length would consume
the start of the next part and lose it. That is the case worth having a test for, because
nothing about it fails loudly. The stream carries on and one event quietly never arrives.

Pure bytes, no Home Assistant, so these ran locally against the real function.
"""

import pytest

from custom_components.dahua.client import _pop_complete_multipart_part

BOUNDARY = b"--myboundary"


def _part(payload, declared=None, separator=b"\r\n\r\n", headers=True):
    """One multipart part. `declared` lies about Content-Length when given."""
    head = BOUNDARY + b"\r\nContent-Type: text/plain"
    if headers:
        length = len(payload) if declared is None else declared
        head += b"\r\nContent-Length: " + str(length).encode()
    return head + separator + payload


# --- not enough to deliver yet ----------------------------------------------


def test_a_buffer_with_no_boundary_yields_nothing():
    """The first chunk of a stream can arrive before the first boundary does."""
    assert _pop_complete_multipart_part(b"garbage", BOUNDARY) == (None, b"garbage")


def test_anything_before_the_first_boundary_is_dropped():
    """A stream that begins mid-part, or a preamble. Keeping it would put junk at
    the front of the first event."""
    payload = b"Code=VideoMotion;action=Start;index=0"
    buffer = b"preamble junk" + _part(payload) + BOUNDARY

    part, rest = _pop_complete_multipart_part(buffer, BOUNDARY)

    assert part is not None
    assert part.startswith(BOUNDARY)
    assert b"preamble junk" not in part


def test_headers_that_have_not_finished_arriving_yield_nothing():
    """No blank line yet, so there is no telling where the payload starts."""
    buffer = BOUNDARY + b"\r\nContent-Len"

    assert _pop_complete_multipart_part(buffer, BOUNDARY) == (None, buffer)


def test_a_payload_still_arriving_yields_nothing():
    """Content-Length says forty and twelve have turned up. Delivering now would
    hand the parser half an event."""
    buffer = _part(b"twelve bytes", declared=40)

    assert _pop_complete_multipart_part(buffer, BOUNDARY) == (None, buffer)


# --- delivering on Content-Length -------------------------------------------


def test_a_complete_part_is_delivered_without_waiting_for_the_next_boundary():
    """The whole point. The next part may be a heartbeat interval away, and this
    event is ready now."""
    payload = b"Code=VideoMotion;action=Start;index=0"
    buffer = _part(payload)

    part, rest = _pop_complete_multipart_part(buffer, BOUNDARY)

    assert part is not None
    assert payload in part
    assert rest == b""


def test_what_is_left_is_the_start_of_the_next_part():
    payload = b"Code=VideoMotion;action=Start;index=0"
    buffer = _part(payload) + b"\r\n" + BOUNDARY + b"\r\nContent-Type: text"

    part, rest = _pop_complete_multipart_part(buffer, BOUNDARY)

    assert payload in part
    assert rest.startswith(BOUNDARY)


@pytest.mark.parametrize("framing", [b"\r\n", b"\n"])
def test_the_framing_before_the_next_boundary_is_not_left_behind(framing):
    """Content-Length excludes the newline that separates the part from the next
    boundary. Left in the remainder it would sit at the front of the next part,
    which is the same as the preamble problem one part later."""
    buffer = _part(b"abc") + framing + BOUNDARY

    _part_out, rest = _pop_complete_multipart_part(buffer, BOUNDARY)

    assert rest == BOUNDARY


@pytest.mark.parametrize("separator", [b"\r\n\r\n", b"\n\n"])
def test_both_header_separators_are_understood(separator):
    """Firmware disagrees about CRLF. Understanding only one leaves the other's
    events buffered for ever, because the headers never look finished."""
    payload = b"Code=VideoMotion;action=Start;index=0"
    buffer = _part(payload, separator=separator)

    part, _rest = _pop_complete_multipart_part(buffer, BOUNDARY)

    assert part is not None
    assert payload in part


# --- when the length and the framing disagree -------------------------------


def test_a_length_that_would_eat_the_next_part_is_not_believed():
    """The subtle one. The declared length runs past where the next boundary
    actually is, so trusting it would swallow the start of the next event and lose
    it. The framing is the stronger evidence.
    """
    first = _part(b"short", declared=500)
    buffer = first + BOUNDARY + b"\r\nContent-Length: 3\r\n\r\nabc"

    part, rest = _pop_complete_multipart_part(buffer, BOUNDARY)

    assert part is not None
    assert b"short" in part
    assert rest.startswith(BOUNDARY), "the next part was consumed"
    assert b"abc" in rest


@pytest.mark.parametrize("declared", [b"not a number", b"-5"])
def test_an_unusable_content_length_falls_back_to_the_boundary(declared):
    """A negative or unparseable length is no length at all. Falling back keeps
    the device working at the old latency rather than not working."""
    head = (
        BOUNDARY
        + b"\r\nContent-Type: text/plain\r\nContent-Length: "
        + declared
        + b"\r\n\r\n"
    )
    buffer = head + b"payload" + b"\r\n" + BOUNDARY

    part, rest = _pop_complete_multipart_part(buffer, BOUNDARY)

    assert part is not None
    assert b"payload" in part
    assert rest.startswith(BOUNDARY)


# --- devices that send no length at all -------------------------------------


def test_without_a_length_it_waits_for_the_next_boundary():
    payload = b"Code=VideoMotion;action=Start;index=0"
    buffer = _part(payload, headers=False) + b"\r\n" + BOUNDARY

    part, rest = _pop_complete_multipart_part(buffer, BOUNDARY)

    assert part is not None
    assert payload in part
    assert rest.startswith(BOUNDARY)


def test_without_a_length_an_unterminated_part_yields_nothing():
    """No length and no next boundary, so there is nothing to say the part has
    finished. Delivering it would be guessing."""
    buffer = _part(b"still arriving", headers=False)

    assert _pop_complete_multipart_part(buffer, BOUNDARY) == (None, buffer)
