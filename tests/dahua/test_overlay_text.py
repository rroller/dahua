"""A camera name with a space in it should survive being written back.

The text of a channel title or overlay went into the URL raw. yarl then encodes
the query on the way out, and a space becomes "+", which the firmware stores
literally -- so every camera name with a space was written back wrong, and the
device answered OK either way.

Measured on a DHI-NVR5464-16P-EI, writing and reading back the same channel:

    Name=Channel+1     -> stored as "Channel+1"
    Name=Channel%201   -> stored as "Channel 1"
    Name=Gate%232      -> stored as "Gate#2"
    Name=Gate&Drive    -> "Error Bad Request!", and so is Gate%26Drive

The last one is the device refusing an ampersand outright, which quoting cannot
fix and which at least fails loudly. The first three are what this is for.
"""

from custom_components.dahua.client import _overlay_text


def test_a_space_is_escaped_not_turned_into_a_plus():
    """The whole point: "+" is stored literally by the device."""
    assert _overlay_text("Front Door") == "Front%20Door"


def test_every_space_is_escaped():
    assert _overlay_text("A B C") == "A%20B%20C"


def test_a_hash_cannot_truncate_the_value():
    """Unescaped, yarl reads the rest as a URL fragment and never sends it."""
    assert _overlay_text("Gate#2") == "Gate%232"


def test_an_ampersand_cannot_split_the_query():
    assert _overlay_text("Gate&Drive") == "Gate%26Drive"


def test_the_line_separator_stays_a_pipe():
    """Dahua splits lines on it, so it must arrive as a pipe, not %7C."""
    assert _overlay_text("Front Door", "Side Gate") == "Front%20Door|Side%20Gate"


def test_empty_lines_are_dropped_as_before():
    assert _overlay_text("Only", "", None) == "Only"
    assert _overlay_text("a", "b", "c", "d") == "a|b|c|d"


def test_plain_text_is_unchanged():
    """Nothing happens to a name that needed no escaping."""
    assert _overlay_text("BACKYARD") == "BACKYARD"
    assert _overlay_text("SIDE-GATE") == "SIDE-GATE"
