"""The overlay text could be written and never read back.

#461. Five services set or enable an overlay and none of them read one, so the
text could be pushed to a camera and never asked about again. @TesLocker
pointed at the endpoint that answers it:

    configManager.cgi?action=getConfig&name=VideoWidget

and made the point that matters: the text can also be changed on the camera
itself, so remembering what Home Assistant last wrote is not the same thing as
knowing what is on the video.

The two names mirror the services that write them rather than the tables they
live in, which is the only thing that makes them guessable:

    set_text_overlay    writes VideoWidget[ch].CustomTitle[group].Text
    set_custom_overlay  writes VideoWidget[ch].UserDefinedTitle[group].Text
"""
import yaml

from custom_components.dahua import dahua_utils


# --- the parsing ------------------------------------------------------------

def test_one_line_reads_back_as_one_line():
    assert dahua_utils.parse_overlay_lines("Front Door") == ["Front Door"]


def test_the_pipe_is_the_line_separator():
    """The writer escapes the parts and leaves the separator alone."""
    assert dahua_utils.parse_overlay_lines("Front|Door") == ["Front", "Door"]


def test_four_lines_survive():
    assert dahua_utils.parse_overlay_lines("a|b|c|d") == ["a", "b", "c", "d"]


def test_nothing_set_reads_back_as_nothing():
    """Not one empty line, which would show as a blank row in a template."""
    assert dahua_utils.parse_overlay_lines("") == []


def test_a_missing_key_reads_back_as_nothing():
    """data.get() on a camera that has no such overlay returns None."""
    assert dahua_utils.parse_overlay_lines(None) == []


def test_a_space_is_a_space():
    """The writer percent-encodes each part because yarl turns a space into +.

    If that ever regresses, the text comes back with a plus in it and this says
    so rather than the user noticing on the video.
    """
    assert dahua_utils.parse_overlay_lines("Front Door|Side Gate") == [
        "Front Door", "Side Gate"]


def test_an_empty_line_between_two_others_is_kept():
    """Dahua stores a blank middle line as an empty segment, not as a missing one."""
    assert dahua_utils.parse_overlay_lines("a||c") == ["a", "", "c"]


# --- the service contract ---------------------------------------------------

def test_the_service_is_declared():
    with open("custom_components/dahua/services.yaml", encoding="utf-8") as f:
        services = yaml.safe_load(f)

    assert "get_overlay_text" in services
    assert "group" in services["get_overlay_text"]["fields"]


def test_it_is_declared_beside_the_services_that_write():
    """A reader nobody can find next to the writers is not much of a reader."""
    with open("custom_components/dahua/services.yaml", encoding="utf-8") as f:
        services = yaml.safe_load(f)

    for writer in ("set_text_overlay", "set_custom_overlay"):
        assert writer in services, "%s went missing" % writer
