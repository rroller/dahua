"""A device that has refused the LightingScheme read is not asked again.

#654 added a check at light-command time: Smart Dual Light cameras decide
separately which emitter they will use, and while that says AIMode or
InfraredMode the white light stays off however correct the write was.

Measured afterwards on two recorders -- a DHI-NVR5464-16P-EI and the one on
#647 -- `getConfig&name=LightingScheme` returns `400 Bad Request`. So on every
NVR channel that check is a round trip that cannot succeed, plus a traceback in
the debug log for a warning that can never fire. One reporter has already gone
chasing that traceback.

Asking once per device keeps the check for everything that answers and costs a
refusing device exactly one request for the life of the entity.
"""

import logging

import pytest

from custom_components.dahua.light import DahuaIlluminator


class _Client:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def async_get_lighting_scheme(self):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Coordinator:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def get_device_name():
        return "Front Street"


def _illuminator(client):
    """The real entity, skipping only Home Assistant's own constructor."""
    entity = object.__new__(DahuaIlluminator)
    entity._coordinator = _Coordinator(client)
    entity._name = "Illuminator"
    entity._scheme_unreadable = False
    return entity


# --- a device that refuses ---------------------------------------------------

async def test_a_refusing_device_is_asked_exactly_once():
    client = _Client(Exception("400, message='Bad Request'"))
    entity = _illuminator(client)

    for _ in range(5):
        await entity._warn_if_the_scheme_blocks_it(3, "0")

    assert client.calls == 1, "a recorder cannot answer this, so asking again is waste"


async def test_the_command_still_succeeds_when_the_read_is_refused():
    """Not being able to check must never fail what the user asked for."""
    entity = _illuminator(_Client(Exception("400")))

    await entity._warn_if_the_scheme_blocks_it(3, "0")
    await entity._warn_if_the_scheme_blocks_it(3, "0")


# --- a device that answers is unaffected -------------------------------------

async def test_a_device_that_answers_is_asked_every_time():
    """The scheme is something the user can change on the camera at any moment,
    so a readable one must be re-read per command."""
    client = _Client({"table.LightingScheme[3][0].LightingMode": "WhiteMode"})
    entity = _illuminator(client)

    for _ in range(4):
        await entity._warn_if_the_scheme_blocks_it(3, "0")

    assert client.calls == 4


async def test_one_entity_refusing_does_not_silence_another():
    """The flag is per entity, so one channel cannot switch the check off for
    a different device that answers perfectly well."""
    refusing = _Client(Exception("400"))
    answering = _Client({"table.LightingScheme[0][0].LightingMode": "WhiteMode"})

    await _illuminator(refusing)._warn_if_the_scheme_blocks_it(3, "0")
    good = _illuminator(answering)
    await good._warn_if_the_scheme_blocks_it(0, "0")
    await good._warn_if_the_scheme_blocks_it(0, "0")

    assert answering.calls == 2


# --- and saying so, which is what none of the above did ----------------------
#
# Everything above is about not asking a refusing device twice. It is silent on
# whether the user ever learns anything, and until #959 they did not: the
# warning for a blocking scheme was written here and nothing in production
# called this method, while an unreadable scheme logged at debug and switched
# the check off for the life of the entity.
#
# Measured on a DHI-NVR5464-16P-EI: LightingScheme answers 400 on every channel,
# the Lighting_V2 white row accepts Mode=Manual and reads it back, and the
# emitter stays dark. Six paths were tried and none drives or observes that lamp
# through the recorder, so the honest outcome is a sentence in the log rather
# than a fix.


def _warnings(caplog):
    """Warnings from this integration only, so another logger cannot fail these.

    Matched by prefix, not equality. light.py takes its logger from `__name__`, so
    records arrive as `custom_components.dahua.light`, while digest.py uses
    `__package__` and arrives as `custom_components.dahua`. An equality check here
    found nothing while the warning was being emitted perfectly well.
    """
    return [r for r in caplog.records
            if r.levelno >= logging.WARNING
            and r.name.startswith("custom_components.dahua")]


async def test_a_device_that_cannot_answer_says_so_once(caplog):
    """The case that cost an afternoon. Accepted write, dark lamp, nothing logged
    above debug."""
    entity = _illuminator(_Client(Exception("400, message='Bad Request'")))

    for _ in range(4):
        await entity._warn_if_the_scheme_blocks_it(3, "0")

    warnings = _warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "LightingScheme" in warnings[0].getMessage()
    assert "Front Street" in warnings[0].getMessage()


async def test_the_message_does_not_claim_the_light_is_broken(caplog):
    """An absent scheme is benign on a camera that predates the table, and those
    two cases cannot be told apart from here. So the wording says what was not
    established, not that the light will fail."""
    await _illuminator(_Client(Exception("400")))._warn_if_the_scheme_blocks_it(3, "0")

    message = _warnings(caplog)[0].getMessage()
    assert "could not be checked" in message
    assert "web interface" in message, "it has to say what the owner can do"


async def test_a_readable_scheme_on_white_mode_says_nothing(caplog):
    """The common case stays quiet, or the warning stops being read."""
    client = _Client({"table.LightingScheme[3][0].LightingMode": "WhiteMode"})

    for _ in range(3):
        await _illuminator(client)._warn_if_the_scheme_blocks_it(3, "0")

    assert not _warnings(caplog)


async def test_a_blocking_scheme_still_warns(caplog):
    """#654's own case, which could never fire while nothing called this."""
    client = _Client({"table.LightingScheme[3][0].LightingMode": "AIMode"})

    await _illuminator(client)._warn_if_the_scheme_blocks_it(3, "0")

    messages = [r.getMessage() for r in _warnings(caplog)]
    assert any("AIMode" in m for m in messages), messages


def test_turning_the_light_on_actually_performs_the_check():
    """The regression guard this needs.

    The check was wired into the command path by #654 and the call was lost in a
    later move. Nothing went red, because the tests above call the method
    directly, so the only thing that had ever exercised the wiring was a person
    pressing the button. A source level assertion is the right shape here: the
    behavioural alternative is driving the whole of async_turn_on through a double
    of the camera, which is a second implementation of the thing under test.
    """
    import ast
    import inspect

    from custom_components.dahua import light as light_module

    source = inspect.getsource(light_module)
    tree = ast.parse(source)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "DahuaIlluminator")
    turn_on = next(n for n in cls.body
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_turn_on")

    called = {ast.unparse(node.func) for node in ast.walk(turn_on)
              if isinstance(node, ast.Call)}

    assert "self._warn_if_the_scheme_blocks_it" in called, sorted(called)
