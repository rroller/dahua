"""An encoder format's audio controls carry the stream's name, not the table's.

They read "Main format 1 audio" and "Extra format 1 audio", which are Dahua's
names for the two halves of its Encode table. The camera entities on the same
device page call the same streams Main and Sub, so one stream had two names.

Read through the real classes and the real English file, so a key the file
does not hold, or a placeholder the string names and the entity does not fill,
fails here.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.dahua.select import (
    ENCODE_AUDIO_SOURCE_NAMES,
    DahuaEncodeAudioSourceSelect,
    encode_audio_source_key,
)
from custom_components.dahua.switch import (
    ENCODE_AUDIO_NAMES,
    DahuaEncodeAudioSwitch,
    encode_audio_key,
)

TRANSLATIONS = (
    Path(__file__).resolve().parents[2] / "custom_components" / "dahua" / "translations"
)


def _strings(language="en"):
    return json.loads((TRANSLATIONS / ("%s.json" % language)).read_text("utf-8"))[
        "entity"
    ]


class _Coordinator:
    data = {}

    def get_serial_number(self):
        return "SERIAL"

    def get_channel(self):
        return 0


def _name(cls, platform, format_type, format_index, language="en"):
    """What Home Assistant composes: the file's string with the entity's fill."""
    entity = cls(
        _Coordinator(),
        SimpleNamespace(entry_id="entry"),
        format_type,
        format_index,
        "table.Encode[0].%s[%d].AudioEnable" % (format_type, format_index),
    )
    template = _strings(language)[platform][entity._attr_translation_key]["name"]
    return template.format(**entity._attr_translation_placeholders)


@pytest.mark.parametrize(
    "format_type, format_index, name",
    [
        ("MainFormat", 0, "Main audio"),
        ("MainFormat", 1, "Main format 2 audio"),
        ("MainFormat", 3, "Main format 4 audio"),
        ("ExtraFormat", 0, "Sub audio"),
        ("ExtraFormat", 1, "Sub 2 audio"),
        ("ExtraFormat", 2, "Sub 3 audio"),
    ],
)
def test_the_switch_is_named_after_its_stream(format_type, format_index, name):
    assert _name(DahuaEncodeAudioSwitch, "switch", format_type, format_index) == name


@pytest.mark.parametrize(
    "format_type, format_index, name",
    [
        ("MainFormat", 0, "Main audio source"),
        ("MainFormat", 1, "Main format 2 audio source"),
        ("MainFormat", 3, "Main format 4 audio source"),
        ("ExtraFormat", 0, "Sub audio source"),
        ("ExtraFormat", 1, "Sub 2 audio source"),
        ("ExtraFormat", 2, "Sub 3 audio source"),
    ],
)
def test_the_source_select_is_named_after_its_stream(format_type, format_index, name):
    assert (
        _name(DahuaEncodeAudioSourceSelect, "select", format_type, format_index) == name
    )


def test_every_format_on_a_channel_has_a_name_of_its_own():
    """A recorder channel holds MainFormat[0..3] and ExtraFormat[0..2]. Two of
    them reading the same would be two switches nobody can tell apart."""
    formats = [("MainFormat", i) for i in range(4)] + [
        ("ExtraFormat", i) for i in range(3)
    ]
    for cls, platform in (
        (DahuaEncodeAudioSwitch, "switch"),
        (DahuaEncodeAudioSourceSelect, "select"),
    ):
        names = [_name(cls, platform, *format_) for format_ in formats]
        assert len(set(names)) == len(formats), names


@pytest.mark.parametrize("language", ["en", "es"])
def test_every_key_has_a_string_that_the_entity_can_fill(language):
    """str.format raises on a placeholder the entity does not supply, which in
    Home Assistant is an entity with no name at all."""
    for cls, platform, table in (
        (DahuaEncodeAudioSwitch, "switch", ENCODE_AUDIO_NAMES),
        (DahuaEncodeAudioSourceSelect, "select", ENCODE_AUDIO_SOURCE_NAMES),
    ):
        for _key, format_type, first in table:
            assert _name(cls, platform, format_type, 0 if first else 1, language)


def test_the_old_keys_are_gone_from_every_language():
    """Left behind, the old string is a name no entity asks for."""
    for path in TRANSLATIONS.glob("*.json"):
        entity = json.loads(path.read_text("utf-8")).get("entity", {})
        assert "encode_audio" not in entity.get("switch", {}), path.name
        assert "encode_audio_source" not in entity.get("select", {}), path.name


def test_the_id_an_installed_entity_was_registered_under_does_not_move():
    """Only the name changed. A different unique id would be a new entity and
    an orphaned old one on every installation."""
    switch = DahuaEncodeAudioSwitch(
        _Coordinator(), SimpleNamespace(entry_id="entry"), "ExtraFormat", 1, "key"
    )
    select = DahuaEncodeAudioSourceSelect(
        _Coordinator(), SimpleNamespace(entry_id="entry"), "MainFormat", 0, "key"
    )

    assert switch.unique_id == "SERIAL_audio_extraformat_1"
    assert select.unique_id == "SERIAL_audio_source_mainformat_0"


def test_a_half_the_table_does_not_have_is_refused():
    with pytest.raises(ValueError):
        encode_audio_key("SnapFormat", 0)
    with pytest.raises(ValueError):
        encode_audio_source_key("SnapFormat", 0)
