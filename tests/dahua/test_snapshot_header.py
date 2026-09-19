"""A snapshot's header segments must end where they say they do.

#575 reported Telegram refusing snapshots from a DH-IPC-HFW2449TL-S-PRO while
browsers and viewers accepted them. The first guess was the JFIF version field
and the second was the post-EOI `dhav` trailer that #658 removes; the reporter
then supplied an unmodified snapshot which has neither problem -- it starts at
SOI, ends exactly at EOI, and carries no trailer.

Measured on that file:

    009e  COM  declares length 4094, actually occupies 4702 -- 608 short
          the declared end, 109e, holds 0x00 where a marker must be
          the next real marker is at 12fe

A decoder that trusts the length desynchronises there and loses the rest of the
file. Lenient ones resynchronise, which is why this went unnoticed.

The reporter's own experiments isolate the cause precisely: removing that
segment makes the file acceptable, while zeroing its payload and leaving the
length alone does not. The defect is the declared length, not the contents.

Only COM and APPn segments are dropped -- both are ignorable by definition.
Everything here is deliberately narrow: a header that cannot be repaired into a
consistent one is returned exactly as it arrived.
"""

from custom_components.dahua.client import (
    repair_dahua_snapshot_header,
    strip_dahua_snapshot_trailer,
)

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

# The five non-zero bytes the measured 4700-byte payload actually contained.
PADDING = bytearray(4700)
for offset, value in ((1, 0x7C), (4, 0x05), (5, 0xFA), (6, 0xAB), (7, 0x6A)):
    PADDING[offset] = value
PADDING = bytes(PADDING)

DECLARED_TOO_SHORT = 4094          # what the camera writes
TRUE_LENGTH = len(PADDING) + 2     # what it actually emits


def _segment(marker: int, payload: bytes, declared: int = None) -> bytes:
    """One header segment. `declared` overrides the length actually written."""
    if declared is None:
        declared = len(payload) + 2
    return bytes((0xFF, marker)) + declared.to_bytes(2, "big") + payload


APP0 = _segment(0xE0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00")
DQT = _segment(0xDB, bytes(65))
DHAV_COMMENT = _segment(0xFE, b"DHAV" + bytes(250))
SOF0 = _segment(0xC0, b"\x08\x05\xa0\x0a\x00\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
DHT = _segment(0xC4, bytes(3))
SOS = _segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00")
SCAN = b"entropycodeddata" + EOI

BAD_COMMENT = _segment(0xFE, PADDING, declared=DECLARED_TOO_SHORT)
GOOD_COMMENT = _segment(0xFE, PADDING)

BROKEN = SOI + APP0 + DQT + BAD_COMMENT + DHAV_COMMENT + SOF0 + DHT + SOS + SCAN
REPAIRED = SOI + APP0 + DQT + DHAV_COMMENT + SOF0 + DHT + SOS + SCAN


# --- the file this exists for -------------------------------------------------

def test_a_segment_that_lies_about_its_length_is_dropped():
    assert repair_dahua_snapshot_header(BROKEN) == REPAIRED


def test_exactly_the_bad_segment_is_removed_and_nothing_else():
    removed = len(BROKEN) - len(repair_dahua_snapshot_header(BROKEN))

    assert removed == TRUE_LENGTH + 2, "expected the marker and its real extent"


def test_the_image_itself_is_untouched():
    """Everything from the frame header onward must survive byte for byte."""
    result = repair_dahua_snapshot_header(BROKEN)

    assert len(result) < len(BROKEN), "nothing was removed at all"
    assert result[result.find(b"\xff\xc0"):] == BROKEN[BROKEN.find(b"\xff\xc0"):]


def test_a_correctly_declared_comment_is_kept():
    """The second COM on the real file declares its length correctly."""
    result = repair_dahua_snapshot_header(BROKEN)

    assert DHAV_COMMENT in result
    assert BAD_COMMENT not in result, "the mis-declared one should be gone"


def test_the_result_still_starts_and_ends_where_a_jpeg_should():
    result = repair_dahua_snapshot_header(BROKEN)

    assert result.startswith(SOI) and result.endswith(EOI)


# --- anything else is left alone ----------------------------------------------

def test_a_well_formed_header_is_returned_unchanged():
    assert repair_dahua_snapshot_header(REPAIRED) == REPAIRED


def test_a_file_that_is_not_a_jpeg_is_returned_unchanged():
    assert repair_dahua_snapshot_header(b"not an image at all") == b"not an image at all"
    assert repair_dahua_snapshot_header(b"") == b""


def test_a_quantisation_table_with_a_bad_length_is_not_touched():
    """A DQT is the image. Dropping one to tidy the header would be worse."""
    broken_dqt = (SOI + APP0 + _segment(0xDB, bytes(65), declared=40)
                  + SOF0 + DHT + SOS + SCAN)

    assert repair_dahua_snapshot_header(broken_dqt) == broken_dqt


def test_a_frame_header_with_a_bad_length_is_not_touched():
    broken_sof = (SOI + APP0 + _segment(0xC0, bytes(15), declared=9)
                  + DHT + SOS + SCAN)

    assert repair_dahua_snapshot_header(broken_sof) == broken_sof


def test_a_repair_that_would_not_help_changes_nothing():
    """No marker follows the bad segment, so there is nothing to resume at."""
    truncated = SOI + APP0 + _segment(0xFE, bytes(200), declared=4094)

    assert repair_dahua_snapshot_header(truncated) == truncated


def test_a_header_with_no_scan_at_all_is_returned_unchanged():
    assert repair_dahua_snapshot_header(SOI + APP0 + DQT) == SOI + APP0 + DQT


# --- the two cleanups are independent -----------------------------------------

def test_the_trailer_stripper_is_unaffected_by_a_bad_header():
    """#658's fix and this one address different devices and must not interact."""
    with_trailer = BROKEN + b"dhav\x59\x19\x01\x00"

    assert strip_dahua_snapshot_trailer(with_trailer) == BROKEN


def test_a_file_with_both_faults_comes_out_clean():
    both = BROKEN + b"dhav\x59\x19\x01\x00"

    assert repair_dahua_snapshot_header(strip_dahua_snapshot_trailer(both)) == REPAIRED
