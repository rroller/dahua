"""The errors a user sees are translatable, and actually reach their translation.

`exception-translations` on Home Assistant's quality scale. Five
`HomeAssistantError` raises used to put an English string in front of the user, and
three of them put whatever `str(some_exception)` happened to be.

The trap this file mostly exists for is in `HomeAssistantError.__init__`:

    if not args and translation_key and translation_domain:
        self.generate_message = True

and `__str__` returns the positional message whenever there is one. So a raise that
passes **both** a string and a translation key looks complete, validates in
hassfest, ships, and never reads the translation. Nothing anywhere fails. The
string simply stays English in every language, which is the exact thing this rule
is about.

Read out of Home Assistant 2026.9.3 rather than assumed.
"""

import ast
import io
import json
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"


def _raises():
    """Every HomeAssistantError raise in the integration, as (module, ast.Call)."""
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and getattr(node.exc.func, "id", None) == "HomeAssistantError"):
                yield path.name, node.exc


def _keyword(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _messages_in_file():
    data = json.load(io.open(PACKAGE / "translations" / "en.json", encoding="utf-8"))
    return {key: entry["message"]
            for key, entry in data.get("exceptions", {}).items()}


def _placeholders(text):
    return set(re.findall(r"\{(\w+)\}", text))


# --- the trap ----------------------------------------------------------------

def test_no_raise_passes_a_message_as_well_as_a_key():
    """`HomeAssistantError.__init__` only sets `generate_message` when it gets no
    positional argument, and `__str__` prefers the positional message. A raise with
    both is silently untranslated: no error, no warning, hassfest is happy, and the
    English string shows in every language."""
    both = ["%s:%d" % (module, call.lineno)
            for module, call in _raises()
            if call.args and _keyword(call, "translation_key") is not None]

    assert not both, (
        "passes a positional message alongside a translation key, so the "
        "translation is never read: %s" % both)


def test_every_raise_is_translated():
    """A raise with neither a key nor an obvious reason to be exempt is an English
    string in front of a user who may not read English."""
    untranslated = ["%s:%d" % (module, call.lineno)
                    for module, call in _raises()
                    if _keyword(call, "translation_key") is None]

    assert not untranslated, "raises an untranslated message: %s" % untranslated


def test_every_raise_names_this_integration_as_the_domain():
    """Without `translation_domain` the key is looked up against nothing.
    `generate_message` is only set when both are given, so the message would fall
    back to the key itself, and the user would see `cancel_call_refused`."""
    wrong = []
    for module, call in _raises():
        domain = _keyword(call, "translation_domain")
        if domain is None or ast.unparse(domain) != "DOMAIN":
            wrong.append("%s:%d" % (module, call.lineno))

    assert not wrong, "does not pass translation_domain=DOMAIN: %s" % wrong


# --- the two sides agree ------------------------------------------------------

def test_every_key_raised_has_a_message():
    keys = {ast.literal_eval(_keyword(call, "translation_key"))
            for _module, call in _raises()
            if _keyword(call, "translation_key") is not None}
    missing = sorted(keys - set(_messages_in_file()))

    assert not missing, "raised with no message in en.json: %s" % missing


def test_every_message_is_raised_by_something():
    """A message nothing raises is one a translator spends time on for nothing."""
    keys = {ast.literal_eval(_keyword(call, "translation_key"))
            for _module, call in _raises()
            if _keyword(call, "translation_key") is not None}
    unused = sorted(set(_messages_in_file()) - keys)

    assert not unused, "in en.json with nothing raising it: %s" % unused


def test_every_placeholder_a_message_uses_is_supplied():
    """An unsupplied placeholder renders literally, so the user reads
    "{device} has no doorbell connection". Same failure as the repair cards in
    #822, in a different place."""
    messages = _messages_in_file()
    supplied = {}
    for _module, call in _raises():
        key_node = _keyword(call, "translation_key")
        if key_node is None:
            continue
        key = ast.literal_eval(key_node)
        placeholders = _keyword(call, "translation_placeholders")
        names = set()
        if placeholders is not None and isinstance(placeholders, ast.Dict):
            names = {ast.literal_eval(k) for k in placeholders.keys}
        supplied.setdefault(key, set()).update(names)

    for key, message in sorted(messages.items()):
        wanted = _placeholders(message)
        assert wanted <= supplied.get(key, set()), (
            "%s uses %s and the raise supplies %s"
            % (key, sorted(wanted), sorted(supplied.get(key, set()))))


def test_a_message_does_not_name_a_placeholder_nobody_reads():
    """The other direction: a raise that supplies a placeholder the message never
    uses is dead weight, and usually means the message was reworded."""
    messages = _messages_in_file()
    for _module, call in _raises():
        key_node = _keyword(call, "translation_key")
        placeholders = _keyword(call, "translation_placeholders")
        if key_node is None or not isinstance(placeholders, ast.Dict):
            continue
        key = ast.literal_eval(key_node)
        if key not in messages:
            # A key with no message at all is test_every_key_raised_has_a_message's
            # to report. Indexing it here would turn that into a KeyError from
            # inside this test, which says nothing about what is wrong.
            continue
        supplied = {ast.literal_eval(k) for k in placeholders.keys}
        assert supplied <= _placeholders(messages[key]), (
            "%s supplies %s and the message uses %s"
            % (key, sorted(supplied), sorted(_placeholders(messages[key]))))


# --- and the reasons that are not ours to translate --------------------------

def test_the_reasons_from_elsewhere_are_carried_not_replaced():
    """Two messages wrap something this integration did not write: what the
    doorbell said when it refused, and the text client.py builds when it cannot
    resolve an IVS rule. Those keep a `{reason}` rather than being replaced by a
    guess, so the sentence is translated and the detail survives."""
    messages = _messages_in_file()

    for key in ("cancel_call_refused", "ivs_rule_write_failed"):
        # Presence asserted rather than indexed, so a renamed key fails here with
        # a sentence instead of a KeyError from inside the assertion.
        assert key in messages, "%s has no message at all" % key
        assert "{reason}" in messages[key], key
