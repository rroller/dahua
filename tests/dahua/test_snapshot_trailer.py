"""A snapshot must end where the JPEG ends.

Some Dahua devices append a short proprietary block after the end-of-image
marker. Lenient decoders skip it, which is why it goes unnoticed; strict ones
reject the file outright, and they are entitled to -- bytes after EOI make the
JPEG malformed.

Measured on a VTO doorbell: every snapshot ended eight bytes past the marker,
`dhav` plus four varying bytes, stable across repeated fetches. NVR channels on
the same network ended exactly at the marker, so this is a per-device trait.

Everything here is deliberately narrow. Truncating an image on a guess would be
worse than passing a trailer through, so anything that does not match precisely
is returned untouched.
"""

from custom_components.dahua.client import strip_dahua_snapshot_trailer

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
BODY = b"\xff\xe0\x00\x10JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00" + b"imagedata"


def _jpeg(trailer=b""):
    return SOI + BODY + EOI + trailer


# --- the device this exists for -----------------------------------------------

def test_the_dhav_trailer_is_removed():
    assert strip_dahua_snapshot_trailer(_jpeg(b"dhav\x59\x19\x01\x00")) == _jpeg()


def test_the_result_ends_at_the_end_of_image_marker():
    """What a strict decoder actually checks."""
    assert strip_dahua_snapshot_trailer(_jpeg(b"dhav\x04\x1a\x01\x00")).endswith(EOI)


def test_a_trailer_of_another_length_is_still_removed():
    """The four bytes after the signature varied between fetches."""
    assert strip_dahua_snapshot_trailer(_jpeg(b"dhav" + b"\x00" * 32)) == _jpeg()


# --- and everything that must be left alone -----------------------------------

def test_a_clean_jpeg_is_untouched():
    clean = _jpeg()
    assert strip_dahua_snapshot_trailer(clean) is clean


def test_an_unrecognised_trailer_is_left_alone():
    """Not our signature, not our business -- passing it on beats truncating."""
    odd = _jpeg(b"\x00\x01\x02\x03")
    assert strip_dahua_snapshot_trailer(odd) == odd


def test_data_that_is_not_a_jpeg_is_left_alone():
    """An error page or an empty body must not be sliced up."""
    for junk in (b"", b"<html>not an image</html>", b"dhav"):
        assert strip_dahua_snapshot_trailer(junk) == junk


def test_a_jpeg_with_no_end_marker_is_left_alone():
    """Truncated download: there is no marker to trust, so change nothing."""
    truncated = SOI + BODY
    assert strip_dahua_snapshot_trailer(truncated) == truncated


def test_an_eoi_inside_the_data_does_not_cause_a_truncation():
    """EOI bytes can occur inside a stream; only a signed trailer may truncate."""
    inner = SOI + BODY + EOI + b"morecompresseddata"
    assert strip_dahua_snapshot_trailer(inner) == inner
