"""v1 unless the device has refused it. Not "v2 wherever a v2 row exists".

The first version of this preferred Lighting_V2 whenever a channel reported an
`InfraredLight` row there. On the recorder it was measured against that is right --
v1 answers `403 Authority:check failure` on every channel and every profile, and a
v2 write moved `ZoomPrio` to `Manual` and read back `Manual`.

But #647 carries a **directly connected** IPC-T5442TM-AS-6mm reporting three v2
`InfraredLight` rows, with no `WhiteLight` row at all:

    table.Lighting_V2[0][0][0].LightType=InfraredLight  Mode=Auto    MiddleLight 50
    table.Lighting_V2[0][1][0].LightType=InfraredLight  Mode=Manual  MiddleLight 100
    table.Lighting_V2[0][2][0].LightType=InfraredLight  Mode=Auto    MiddleLight 50

and that issue's complaint is "the CGI values change and the physical LEDs do not".
Moving every camera that merely *reports* a v2 row onto a table nobody has verified
drives its emitter is that complaint, shipped. A v2 write was proved to move the
*mode*; it was never proved to move the *light*.

So the rule decides from what a device **accepts**, not from what it reports. That
is also the shape this codebase has reached independently three times --
`_HOST_CGI_CONFIG_ABSENT`, `_RPC2_TABLE_UNAVAILABLE` and `refusals` all learn from a
refusal -- while the fifteen capabilities still gated on a model string are its
recurring bug class (#570, #676, #690).
"""

import pytest

from custom_components.dahua.coordinator import infrared_transport

A_ROW = ("0", 0, "NearLight")


# --- the rule ------------------------------------------------------------------


def test_v1_is_used_while_it_has_not_been_refused():
    """Every camera that works today keeps the table it works on, including ones
    that happen to report a v2 row."""
    assert infrared_transport(A_ROW, v1_refused=False) == "v1"


def test_v2_is_used_once_v1_has_been_refused():
    """The recorder's case: v1 answers 403 on every channel and v2 works."""
    assert infrared_transport(A_ROW, v1_refused=True) == "v2"


def test_a_refused_v1_with_no_v2_row_stays_on_v1():
    """Thirteen of that recorder's fifteen channels. There is nowhere to fall back
    to, and reporting v2 would send a write to a row the device does not serve."""
    assert infrared_transport(None, v1_refused=True) == "v1"


def test_no_v2_row_and_no_refusal_is_the_ordinary_case():
    assert infrared_transport(None, v1_refused=False) == "v1"


@pytest.mark.parametrize(
    "row",
    [
        ("0", 0, "MiddleLight"),
        ("2", 1, "NearLight"),
        ("0", 3, "FarLight"),
    ],
)
def test_the_row_contents_do_not_change_the_decision(row):
    """Only its presence matters here. Which profile, index and bank to use is
    `infrared_v2_row`'s job, and keeping the two separate is what lets this one be
    a pure two-input decision."""
    assert infrared_transport(row, v1_refused=False) == "v1"
    assert infrared_transport(row, v1_refused=True) == "v2"


# --- and the shape of the thing -------------------------------------------------


def test_it_answers_with_one_of_exactly_two_names():
    """The callers branch on this string, so a third value would be a silent
    fall-through to the v1 branch."""
    answers = {
        infrared_transport(row, refused)
        for row in (A_ROW, None)
        for refused in (True, False)
    }

    assert answers == {"v1", "v2"}


def test_a_missing_row_is_judged_by_being_none_not_by_being_falsy():
    """An empty tuple is not "no row" -- it would mean the detector returned
    something malformed, and treating that as absent would hide the bug. There is
    exactly one way to say "this channel has no v2 row" and it is None."""
    assert infrared_transport((), v1_refused=True) == "v2"
