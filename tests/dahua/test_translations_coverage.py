"""The translated error messages must not drift, and must not blame the address.

Before this, each of the eight non-English files defined **exactly one** error string,
`config.error.auth`, and in every language it was the pre-#690 sentence naming the
address. English has twelve. Home Assistant falls back per key, so a non-English user
got eleven accurate English messages and **the one message in their own language was
the wrong one**: it sent them to check three things when the device had returned a 401
and only the credentials were in question.

#34 is somebody reading that message in English: *"Says 'Username, Password, or
Address is wrong'. Which it isn't."*

`test_no_reason_still_blames_the_password_by_accident` has forbidden this in English
for a while. It only ever looked at `en.json`, and eight files broke the same rule in
their own language with nothing checking them.
"""

import json
from pathlib import Path

import pytest

TRANSLATIONS = (
    Path(__file__).resolve().parents[2] / "custom_components" / "dahua" / "translations"
)

# The word for "address" in each shipped language, so the rule English has always had
# can be applied to all of them. Only `auth` is checked: cannot_connect names the
# address in every language and should.
ADDRESS_WORDS = {
    "en": "address",
    "bg": "адрес",
    "ca": "adreça",
    "es": "dirección",
    "fr": "adresse",
    "it": "indirizzo",
    "nl": "adres",
    "pt": "endereço",
    "pt-BR": "endereço",
}


def _languages():
    return sorted(p.stem for p in TRANSLATIONS.glob("*.json"))


def _errors(language):
    path = TRANSLATIONS / ("%s.json" % language)
    return json.loads(path.read_text(encoding="utf-8"))["config"].get("error", {})


# --- the map above has to keep up with the files -----------------------------


def test_every_shipped_language_has_an_address_word():
    """Derived rather than trusted. A language added without its word here would
    silently stop being checked, which is how the hand written issue placeholder map
    quietly stopped covering a new issue."""
    assert set(ADDRESS_WORDS) == set(
        _languages()
    ), "shipped but unmapped: %s; mapped but not shipped: %s" % (
        sorted(set(_languages()) - set(ADDRESS_WORDS)),
        sorted(set(ADDRESS_WORDS) - set(_languages())),
    )


# --- the rule English already had, now for everyone --------------------------


@pytest.mark.parametrize("language", sorted(ADDRESS_WORDS))
def test_no_language_blames_the_address_for_a_refused_login(language):
    """A 401 says the credentials were refused and nothing about the address."""
    auth = _errors(language).get("auth")
    if not auth:
        pytest.skip("%s does not translate auth" % language)

    assert ADDRESS_WORDS[language].casefold() not in auth.casefold(), (
        "%s tells the user to check their address over a refused login" % language
    )


# --- and the set must not drift --------------------------------------------


def test_every_language_covers_the_same_errors_as_english():
    """Not for completeness's sake: a half-translated error set is how the one
    misleading string came to be the only one anybody could read."""
    expected = set(_errors("en"))
    assert expected, "English defines no error strings"

    for language in _languages():
        if language == "en":
            continue
        missing = expected - set(_errors(language))
        assert not missing, "%s is missing %s" % (language, sorted(missing))


def test_no_language_invents_an_error_english_does_not_have():
    """A key English has dropped is dead weight, and a key English never had renders
    nowhere at all."""
    expected = set(_errors("en"))

    for language in _languages():
        extra = set(_errors(language)) - expected
        assert not extra, "%s has %s, which English does not" % (
            language,
            sorted(extra),
        )


@pytest.mark.parametrize("language", sorted(ADDRESS_WORDS))
def test_no_translated_message_is_empty(language):
    for key, text in _errors(language).items():
        assert text.strip(), "%s.%s is blank" % (language, key)


def test_the_machine_assisted_translations_say_so_somewhere():
    """These were not written by native speakers, and a reader deciding whether to
    trust them should not have to guess. The note also tells them where to help."""
    note = TRANSLATIONS / "README.md"

    assert note.exists(), "no note explaining where the translations came from"
    text = note.read_text(encoding="utf-8").casefold()
    assert "machine" in text
    assert "native" in text
