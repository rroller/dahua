"""Repairing a snapshot whose header lies about its own length.

Measured on a DH-IPC-HFW2449TL-S-PRO snapshot supplied on #575. The file starts and ends
correctly and carries no trailer, but its first COM segment declares 4094 bytes and
actually occupies 4702. A decoder that trusts the length lands mid-padding on a 0x00
where a marker should be and loses the rest of the file.

The repair drops that segment. What makes it safe is what it refuses to drop: only COM
and APPn, which are a comment and application metadata and ignorable by definition.
A quantisation table or a frame header **is** the image, so a segment that lies about its
length and is not ignorable is left exactly as it is and the caller gets the original
bytes back.

It is also conservative about its own success. The repaired bytes are re-walked, and
unless the result is a header whose every segment lands on the next marker, the original
is returned. A guess that does not pay off changes nothing.

Every fixture here that asserts "unchanged" is built so that the guard under test is the
only reason it is unchanged. A first pass at this file had three tests that a mutation
sweep walked straight through, because on a file needing no repair, a guard that fires
and a guard that does not both hand back the same bytes.

Pure byte handling with no Home Assistant in it, so these ran locally against the real
function, and every guard was checked by breaking it.
"""

import pytest

from custom_components.dahua.client import (
    JPEG_APP0,
    JPEG_COM,
    JPEG_SOI,
    repair_dahua_snapshot_header,
)

SOS = bytes((0xFF, 0xDA))
EOI = bytes((0xFF, 0xD9))
QUANTISATION_TABLE = 0xDB      # length-bearing, and not ignorable
PADDING = bytes((0xFF,))       # legal fill between markers


def _segment(marker, payload, declared=None):
    """One JPEG segment. `declared` lies about the length when given."""
    length = len(payload) + 2 if declared is None else declared
    return bytes((0xFF, marker)) + length.to_bytes(2, "big") + payload


def _liar():
    """A COM segment that occupies 42 bytes and says 20, which is #575's shape."""
    return _segment(JPEG_COM, bytes(40), declared=20)


def _good():
    return _segment(JPEG_APP0, bytes(8))


def _jpeg(*segments, scan=bytes((0x01, 0x02, 0x03))):
    """A file whose header is the segments given, then the start of scan data.

    The consistency walk stops at SOS, so the scan data only has to exist; it is
    not marker-structured and is never examined.
    """
    return JPEG_SOI + b"".join(segments) + SOS + scan + EOI


# --- files that need nothing doing to them ----------------------------------

def test_a_consistent_file_is_returned_unchanged():
    """The common case, and it must be exact: the same object back, not a rebuilt
    copy that happens to compare equal."""
    data = _jpeg(_segment(JPEG_APP0, b"JFIF" + bytes(12)))

    assert repair_dahua_snapshot_header(data) is data


def test_something_that_is_not_a_jpeg_is_left_alone():
    """The camera answers with an error page often enough. Walking it as though it
    were markers would be reading arbitrary bytes as lengths."""
    data = b"<html>404 not found</html>"

    assert repair_dahua_snapshot_header(data) is data


def test_an_empty_answer_is_left_alone():
    assert repair_dahua_snapshot_header(b"") == b""


# --- the reported fault -----------------------------------------------------

def test_a_comment_that_lies_about_its_length_is_dropped():
    """#575 in miniature. The COM segment declares fewer bytes than it occupies,
    so the byte at its declared end is padding rather than the next marker."""
    data = JPEG_SOI + _liar() + _good() + SOS + bytes((0x01, 0x02)) + EOI

    repaired = repair_dahua_snapshot_header(data)

    assert repaired != data
    assert repaired.startswith(JPEG_SOI)
    assert bytes((0xFF, JPEG_COM)) not in repaired
    assert _good() in repaired, "the segment that was fine was dropped too"


def test_an_application_segment_that_lies_is_dropped_too():
    """APPn is metadata and equally ignorable. The reporter's file had it in COM,
    but nothing says the next camera will."""
    liar = _segment(JPEG_APP0, bytes(40), declared=20)
    good = _segment(JPEG_APP0 + 1, bytes(8))
    data = JPEG_SOI + liar + good + SOS + bytes((0x01, 0x02)) + EOI

    repaired = repair_dahua_snapshot_header(data)

    assert repaired != data
    assert good in repaired


def test_the_repaired_file_is_actually_consistent():
    """Not merely different. The whole point is that a decoder can now walk it."""
    from custom_components.dahua.client import _jpeg_header_is_consistent

    data = JPEG_SOI + _liar() + _good() + SOS + bytes((0x01,)) + EOI

    assert not _jpeg_header_is_consistent(data)
    assert _jpeg_header_is_consistent(repair_dahua_snapshot_header(data))


def test_fill_bytes_are_skipped_rather_than_read_as_a_marker():
    """0xFF repeated is legal padding between markers. Reading the second one as a
    marker stops the walk dead, so a file that needs repairing and happens to carry
    padding would be handed back untouched.

    Written as a file that **does** need repairing, because on one that does not,
    skipping the padding and stopping at it both return the same bytes.
    """
    data = JPEG_SOI + PADDING + _liar() + _good() + SOS + bytes((0x01,)) + EOI

    repaired = repair_dahua_snapshot_header(data)

    assert repaired != data, "the padding stopped the walk"
    assert _good() in repaired


# --- what it refuses to touch -----------------------------------------------

def test_a_segment_that_is_not_ignorable_is_never_dropped():
    """A quantisation table is the image. Dropping one to make the header walk
    would turn an unreadable snapshot into a readable wrong one, which is worse:
    the caller can at least see that the first failed."""
    liar = _segment(QUANTISATION_TABLE, bytes(40), declared=20)
    data = JPEG_SOI + liar + _good() + SOS + bytes((0x01,)) + EOI

    assert repair_dahua_snapshot_header(data) is data


def test_a_repair_that_does_not_help_changes_nothing():
    """The result is re-walked and thrown away unless it actually helped.

    There **is** a marker to resume at here, so the repair is attempted and does
    produce something. What it produces still never reaches SOS, so it is not a
    walkable header and the original is handed back instead. A fixture with nothing
    to resume at would return earlier and never reach this line at all.
    """
    # A quantisation table declaring 5, and then the file simply stops, so a walk
    # of the repaired bytes runs off the end without finding SOS.
    truncated = bytes((0xFF, QUANTISATION_TABLE, 0x00, 0x05)) + bytes(3)
    data = JPEG_SOI + _liar() + truncated

    assert repair_dahua_snapshot_header(data) is data


def test_bytes_that_are_not_a_jpeg_are_not_walked_as_one():
    """The SOI check earns its place only against input that would otherwise
    walk. An error page of prose stops at the first byte that is not 0xFF, so it
    cannot tell the guard apart; this is a payload whose bytes do look like
    segments, and without the check it would be rewritten as though it were an
    image.
    """
    liar = _segment(JPEG_COM, bytes(40), declared=20)
    # No SOI. Everything after the first two bytes is valid segment structure.
    data = bytes((0x00, 0x00)) + liar + _good() + SOS + bytes((0x01,)) + EOI

    assert repair_dahua_snapshot_header(data) is data


def test_a_header_that_is_not_markers_at_all_is_left_alone():
    """A truncated or mangled file. The walk stops at the first byte that is not
    0xFF where a marker should be rather than hunting for one."""
    data = JPEG_SOI + bytes(range(8))

    assert repair_dahua_snapshot_header(data) is data


def test_a_segment_declaring_less_than_its_own_length_field_is_left_alone():
    """A length below 2 does not cover the two bytes it is written in, so there is
    no sane end to compute and the file is left alone.

    The rest of this file is a perfectly good header, so without that guard the
    segment would be dropped and a different, consistent file handed back. That is
    what makes this assertion mean something.
    """
    nonsense = bytes((0xFF, JPEG_COM, 0x00, 0x00))
    data = JPEG_SOI + nonsense + _good() + SOS + bytes((0x01,)) + EOI

    assert repair_dahua_snapshot_header(data) is data
