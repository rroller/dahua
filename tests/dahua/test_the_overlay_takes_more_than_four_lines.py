"""The text overlay service is not limited to four lines (#312).

The cap was never the camera's. `set_text_overlay` was written with four
numbered fields and never grown, while `_overlay_text` has always been
variadic and `parse_overlay_lines` has always split on every pipe it finds.
So the only thing stopping a fifth line was the service signature.

What the devices in #312 actually do, from the reporter's own screenshots:

    the NVR's configuration page       5 lines
    the camera's own configuration    14 lines (keep pressing +)

and @sattel69's IPC-HDBW1320E-W is one of the four-line models, which is why
four looked like the protocol's answer rather than one model's.

Nothing here measures a device. The limit stays the device's to enforce: a
write naming a line it does not have is answered with `Error`, and the
existing `"OK" not in value` check turns that into a raise, so a camera that
will not take ten lines says so.

Mutation table, each caught by the test named after it:

* the client slicing back to four lines
* `lines` not reaching the client at all
* `lines` silently winning over text1-text4, rather than raising
* the numbered path changing the URL it has always written
* the pipe or the escaping changing under a long list
"""

import ast

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua.client import DahuaClient


class _RecordingClient(DahuaClient):
    """A client that records the URL instead of talking to a camera."""

    def __init__(self):  # pylint: disable=super-init-not-called
        self.urls = []

    async def get(self, url, *args, **kwargs):
        self.urls.append(url)
        return "OK"


class _Camera:
    """The entity method under test, with the coordinator it reads."""

    def __init__(self, client):
        self._logical_channel = 0
        self._coordinator = type("C", (), {"client": client})()

    async def call(self, **kwargs):
        from custom_components.dahua.camera import DahuaCamera

        return await DahuaCamera.async_set_service_set_text_overlay(
            self, kwargs.pop("group", 0), **kwargs
        )


def _client_and_camera():
    client = _RecordingClient()
    return client, _Camera(client)


# --- the client takes as many lines as it is given ------------------------


async def test_the_client_writes_every_line_it_is_given():
    """Ten lines, because the camera in #312 takes fourteen."""
    client = _RecordingClient()
    lines = ["L%d" % n for n in range(1, 11)]

    await client.async_set_service_set_text_overlay(0, 1, *lines)

    assert client.urls == [
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&VideoWidget[0].CustomTitle[1].Text=" + "|".join(lines)
    ]


async def test_the_client_does_not_slice_to_four():
    client = _RecordingClient()

    await client.async_set_service_set_text_overlay(0, 0, "a", "b", "c", "d", "e")

    assert client.urls[0].endswith("Text=a|b|c|d|e")


async def test_a_long_list_is_still_escaped_and_pipe_separated():
    """The escaping rule does not change just because the list is longer."""
    client = _RecordingClient()

    await client.async_set_service_set_text_overlay(
        0, 0, "Front Door", "Gate#2", "plain", "A B", "last"
    )

    assert client.urls[0].endswith("Text=Front%20Door|Gate%232|plain|A%20B|last")


async def test_the_four_line_url_is_exactly_what_it_has_always_been():
    """The old call shape must put the identical URL on the wire."""
    client = _RecordingClient()

    await client.async_set_service_set_text_overlay(2, 3, "one", "two", "", "")

    assert client.urls == [
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&VideoWidget[2].CustomTitle[3].Text=one|two"
    ]


async def test_a_refused_write_still_raises():
    """A device given more lines than it has answers Error, not OK."""

    class _Refusing(_RecordingClient):
        async def get(self, url, *args, **kwargs):
            self.urls.append(url)
            return "Error"

    client = _Refusing()

    with pytest.raises(Exception, match="Could not set text"):
        await client.async_set_service_set_text_overlay(0, 0, "a", "b", "c", "d", "e")


# --- the service hands lines through --------------------------------------


async def test_lines_reaches_the_camera():
    client, camera = _client_and_camera()

    await camera.call(lines=["one", "two", "three", "four", "five", "six"])

    assert client.urls[0].endswith("Text=one|two|three|four|five|six")


async def test_the_numbered_fields_still_work():
    """Every automation written against this service keeps working."""
    client, camera = _client_and_camera()

    await camera.call(text1="a", text2="b", text3="c", text4="d")

    assert client.urls[0].endswith("Text=a|b|c|d")


async def test_the_numbered_fields_alone_write_the_same_url_as_the_client():
    client_direct = _RecordingClient()
    await client_direct.async_set_service_set_text_overlay(0, 0, "a", "b", "", "")

    client, camera = _client_and_camera()
    await camera.call(text1="a", text2="b")

    assert client.urls == client_direct.urls


async def test_giving_both_is_refused_rather_than_resolved():
    """Either resolution throws away text the user typed."""
    client, camera = _client_and_camera()

    with pytest.raises(HomeAssistantError) as raised:
        await camera.call(text1="kept", lines=["also", "kept"])

    # Translated rather than an English string, which the quality-scale guard
    # in test_exceptions_are_translated.py requires of every raise here.
    assert raised.value.translation_key == "overlay_lines_and_numbered_text"
    assert client.urls == [], "nothing should have been written"


async def test_empty_numbered_fields_do_not_count_as_a_conflict():
    """The schema defaults them to "", so an unset field must not collide."""
    client, camera = _client_and_camera()

    await camera.call(text1="", text2="", lines=["only", "these"])

    assert client.urls[0].endswith("Text=only|these")


async def test_an_empty_list_clears_the_overlay():
    """Writing no lines is how the overlay is emptied, as four blanks were."""
    client, camera = _client_and_camera()

    await camera.call(lines=[])

    assert client.urls[0].endswith("Text=")


# --- the schema -----------------------------------------------------------


def _text_overlay_schema():
    """The voluptuous schema the platform registers for set_text_overlay."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "custom_components"
        / "dahua"
        / "camera.py"
    ).read_text(encoding="utf-8")

    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "async_register_entity_service"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Constant)
            and node.args[2].value == "async_set_service_set_text_overlay"
        ):
            return node.args[1]
    raise AssertionError("set_text_overlay is not registered")


def test_the_schema_accepts_a_lines_field():
    """Without this the service call is rejected before the method is reached."""
    schema = _text_overlay_schema()
    keys = [
        key.args[0].value
        for key in schema.keys
        if isinstance(key, ast.Call) and key.args
    ]
    assert "lines" in keys


def test_the_schema_still_accepts_the_four_numbered_fields():
    schema = _text_overlay_schema()
    keys = [
        key.args[0].value
        for key in schema.keys
        if isinstance(key, ast.Call) and key.args
    ]
    for field in ("text1", "text2", "text3", "text4"):
        assert field in keys


def test_services_yaml_describes_lines():
    """A field with no entry is invisible in Developer tools -> Actions."""
    import pathlib

    import yaml

    described = yaml.safe_load(
        (
            pathlib.Path(__file__).resolve().parents[2]
            / "custom_components"
            / "dahua"
            / "services.yaml"
        ).read_text(encoding="utf-8")
    )
    fields = described["set_text_overlay"]["fields"]

    assert "lines" in fields
    assert fields["lines"]["selector"]["text"]["multiple"] is True
    for field in ("text1", "text2", "text3", "text4"):
        assert field in fields, "the numbered fields must stay documented"
