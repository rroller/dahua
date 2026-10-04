"""`dahua.get_overlay_text` raised NameError, because nothing ever called it.

`async_get_overlay_text` uses `dahua_utils.parse_overlay_lines` and camera.py never
imported `dahua_utils`. The service is registered, documented in services.yaml and
declared `SupportsResponse.ONLY`, so calling it from a script or a template got:

    NameError: name 'dahua_utils' is not defined

It shipped because the method had no test at all. `test_overlay_text.py` covers
`_overlay_text`, which is the *writing* side in client.py, and the reading side had
nothing. A one line import is the fix; the reason it is worth a file of its own is
that the next thing to add here should have to go through these tests.

Found by a sweep for global names a module loads but never defines or imports, run
over every module in the integration. camera.py was the only one.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.dahua.camera import DahuaCamera

WIDGET = {
    "table.VideoWidget[0].CustomTitle[0].Text": "Front Door|Side Gate",
    "table.VideoWidget[0].UserDefinedTitle[0].Text": "Line one",
    "table.VideoWidget[0].CustomTitle[1].Text": "Group one",
    "table.VideoWidget[2].CustomTitle[0].Text": "Another channel",
}


def _camera(table=None, channel=0):
    """The entity without Home Assistant's plumbing, as the other camera tests do."""
    camera = object.__new__(DahuaCamera)
    camera._logical_channel = channel
    camera._coordinator = SimpleNamespace(
        client=SimpleNamespace(
            async_get_video_widget=AsyncMock(
                return_value=WIDGET if table is None else table
            )
        )
    )
    return camera


async def test_reading_the_overlay_does_not_raise():
    """The regression itself. This failed with NameError before the import, which
    is not something any amount of reading the diff would have shown."""
    result = await _camera().async_get_overlay_text(0)

    assert isinstance(result, dict)


async def test_the_pipe_separated_lines_come_back_as_lines():
    """Dahua joins the lines of a title with a pipe, which is why the writer
    escapes the parts and leaves the separator alone. Reading is that backwards."""
    result = await _camera().async_get_overlay_text(0)

    assert result["text_overlay"] == ["Front Door", "Side Gate"]
    assert result["custom_overlay"] == ["Line one"]


async def test_the_two_names_mirror_the_services_that_write_them():
    """set_text_overlay writes CustomTitle and set_custom_overlay writes
    UserDefinedTitle. Returning them under the writer's name rather than the
    table's is the only thing that makes them guessable, so it is worth pinning."""
    result = await _camera().async_get_overlay_text(0)

    assert set(result) == {"text_overlay", "custom_overlay"}


async def test_the_group_selects_the_row():
    result = await _camera().async_get_overlay_text(1)

    assert result["text_overlay"] == ["Group one"]
    assert result["custom_overlay"] == [], "group 1 has no UserDefinedTitle set"


async def test_a_channel_reads_its_own_row():
    """On a recorder every channel is a row in one table, so reading the wrong one
    would report another camera's overlay as this camera's."""
    result = await _camera(channel=2).async_get_overlay_text(0)

    assert result["text_overlay"] == ["Another channel"]


async def test_nothing_set_reads_back_as_nothing_set():
    """Not one empty line. A template doing `| count` should get 0."""
    result = await _camera(table={}).async_get_overlay_text(0)

    assert result == {"text_overlay": [], "custom_overlay": []}


@pytest.mark.parametrize("value", ["", None])
async def test_an_empty_value_is_no_lines(value):
    result = await _camera(
        table={
            "table.VideoWidget[0].CustomTitle[0].Text": value,
        }
    ).async_get_overlay_text(0)

    assert result["text_overlay"] == []
