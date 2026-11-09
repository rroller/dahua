"""A refusal says what a model will not do. It was being thrown away.

Every capability probe ended the same way:

    except PROBE_FAILED:
        self._supports_x = False

which records that the feature is off and discards which of two very different
things happened. An HTTP status is the device answering, and a 400 for a config
table is it saying it does not serve that table: a fact about the model, and
one that generalises. A timeout is the device not answering: a fact about that
moment, which generalises to nothing.

Not having the first, per model, is what the model-name matching in #570, #676
and #690 exists to work around, and it cannot be collected while it is
discarded at the point it is produced.
"""

from aiohttp import ClientConnectionError, ClientResponseError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.diagnostics import _capabilities_block
from custom_components.dahua.rpc2 import Rpc2MethodRefused, Rpc2TableAbsent


def _coordinator():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._probe_refusals = {}
    return c


def _status(code):
    return ClientResponseError(request_info=None, history=(), status=code)


# --- the device answered ----------------------------------------------------


def test_a_status_is_recorded_as_an_answer():
    c = _coordinator()

    c._note_probe_refusal("lighting_scheme", _status(400))

    assert c._probe_refusals["lighting_scheme"] == {
        "answered": True,
        "status": 400,
        "error": "ClientResponseError",
    }


def test_a_404_is_an_answer_too():
    """No such endpoint is still the device telling us something."""
    c = _coordinator()

    c._note_probe_refusal("ptz_position", _status(404))

    assert c._probe_refusals["ptz_position"]["answered"] is True
    assert c._probe_refusals["ptz_position"]["status"] == 404


def test_an_rpc2_refusal_is_an_answer_too():
    """An RPC2 refusal carries a code rather than an HTTP status, and it is the
    device answering: this firmware does not serve that method or table."""
    c = _coordinator()

    c._note_probe_refusal(
        "cloud_upgrade",
        Rpc2MethodRefused(
            "Dahua RPC2 method configManager.getConfig returned result=false",
            code=268959743,
            message="Unknown error",
        ),
    )

    refusal = c._probe_refusals["cloud_upgrade"]
    assert refusal["answered"] is True
    assert refusal["status"] == 268959743


def test_a_reply_with_no_table_is_an_answer_too():
    """⭐⭐ THE THIRD SHAPE, and it has neither a status nor a code: the call
    SUCCEEDED and the table simply was not in the reply.

    @kmich measured what judging that by the absence of a status cost (#1114).
    `get_cloud_upgrade_info` raised a bare `ValueError` when a device replied
    without `_DHCloudUpgrade_`, this filed it as silence, and
    `probes_that_did_not_answer` then reported `cloud_upgrade` for ever. On
    all seventeen of his entries, VTH, IPC and NVR alike, that kept the
    leftover-rows card from ever appearing.
    """
    c = _coordinator()

    c._note_probe_refusal(
        "cloud_upgrade",
        Rpc2TableAbsent("Dahua RPC2 response is missing the _DHCloudUpgrade_ table"),
    )

    refusal = c._probe_refusals["cloud_upgrade"]
    assert refusal["answered"] is True
    # ⚠ And no invented status. The device answered; it did not answer WITH
    # a code, and reporting one would be a reading nobody took.
    assert refusal["status"] is None
    assert refusal["error"] == "Rpc2TableAbsent"


def test_a_table_absent_probe_is_not_reported_as_unanswered():
    """The consequence, asserted where the card actually reads it."""
    c = _coordinator()

    c._note_probe_refusal("cloud_upgrade", Rpc2TableAbsent("no table"))

    assert "cloud_upgrade" not in c.probes_that_did_not_answer()


def test_a_bare_value_error_is_still_silence():
    """⚠ The fix is a class saying `answered`, not "ValueError now counts".
    A `ValueError` from anywhere else still says nothing about the device, and
    treating every one of them as an answer would lose the distinction this
    file exists for."""
    c = _coordinator()

    c._note_probe_refusal("cloud_upgrade", ValueError("something else entirely"))

    assert c._probe_refusals["cloud_upgrade"]["answered"] is False


# --- the device did not answer ----------------------------------------------


def test_a_timeout_is_not_an_answer():
    c = _coordinator()

    c._note_probe_refusal("smart_motion_detect", TimeoutError())

    refusal = c._probe_refusals["smart_motion_detect"]
    assert refusal["answered"] is False
    assert refusal["status"] is None
    assert refusal["error"] == "TimeoutError"


def test_a_dropped_connection_is_not_an_answer():
    c = _coordinator()

    c._note_probe_refusal("lighting_v2", ClientConnectionError("no route"))

    assert c._probe_refusals["lighting_v2"]["answered"] is False


def test_the_two_are_distinguishable():
    """The whole point. Both used to end at supports_x = False."""
    c = _coordinator()

    c._note_probe_refusal("refused", _status(400))
    c._note_probe_refusal("silent", TimeoutError())

    assert (
        c._probe_refusals["refused"]["answered"]
        != c._probe_refusals["silent"]["answered"]
    )


# --- more than one ----------------------------------------------------------


def test_each_probe_is_recorded_separately():
    c = _coordinator()

    c._note_probe_refusal("day_night_color", _status(400))
    c._note_probe_refusal("coaxial_control", _status(501))

    assert sorted(c._probe_refusals) == ["coaxial_control", "day_night_color"]
    assert c._probe_refusals["coaxial_control"]["status"] == 501


def test_a_later_result_replaces_the_earlier_one():
    c = _coordinator()

    c._note_probe_refusal("lighting", TimeoutError())
    c._note_probe_refusal("lighting", _status(400))

    assert c._probe_refusals["lighting"]["status"] == 400


# --- it reaches diagnostics -------------------------------------------------


def test_diagnostics_reports_them():
    c = _coordinator()
    c._supports_lighting_v2 = False
    c._note_probe_refusal("lighting_v2", _status(400))

    block = _capabilities_block(c)

    assert block["refusals"]["lighting_v2"]["status"] == 400
    assert (
        block["probed"]["lighting_v2"] is False
    ), "the capability and the reason should both be there"


def test_a_device_that_refused_nothing_reports_nothing():
    c = _coordinator()

    assert _capabilities_block(c)["refusals"] == {}


def test_it_works_on_a_coordinator_whose_init_never_ran():
    """A dozen tests build coordinators with object.__new__.

    A capability probe is the wrong place to start depending on __init__
    having run, so the store initialises itself rather than assuming.
    """
    bare = object.__new__(DahuaDataUpdateCoordinator)

    bare._note_probe_refusal("coaxial_control", TimeoutError())

    assert bare._probe_refusals["coaxial_control"]["answered"] is False
