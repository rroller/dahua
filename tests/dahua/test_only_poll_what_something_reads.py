"""The coaxial status should be fetched only when an entity exists that reads it.

The poll asked for `coaxialControlIO.cgi?action=getStatus` whenever
`_supports_coaxial_control and self._wanted_by(LIGHT, SWITCH)`. That flag means "the
endpoint answers", not "this device has a siren or a light", so the poll asked a broader
question than the entities did.

Measured on a DHI-NVR5464 with eleven entries: it answered that endpoint on every poll
while having **no siren and no security light entity anywhere**, only two illuminators,
which read `Lighting_V2`. At a 120 second interval that is on the order of 7,900 requests
a day for a value nothing displays.

**Three entities read it, and the third is the one to miss.** A flood light reads
`WhiteLight` out of the same status when the camera reports floodlightmode, and reads
`Lighting_V2` when it does not, so narrowing this to the siren and the security light
alone would have left a flood light reading a value nobody fetched. That failure is
silent: the entity exists and simply stops changing.

So the rule now lives on the coordinator and both the platform and the poll ask it. Two
copies of it drifting apart is exactly how the wrong direction happens.

The RPC2 branch keeps its own condition on purpose. `uses_rpc2_deterrence` already returns
False unless a speaker or light is detected or manually enabled, so it cannot fetch for
nothing.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.const import LIGHT, SELECT, SWITCH


def _coordinator(**kwargs):
    """A coordinator stub carrying only what these predicates read."""
    state = {
        "recorder": False,
        "nvr_deterrence": False,
        "siren": False,
        "security_light": False,
        "amcrest_doorbell": False,
        "flood_light": False,
        "floodlightmode": False,
        "platforms": [LIGHT, SWITCH],
    }
    state.update(kwargs)

    c = SimpleNamespace(
        _supports_floodlightmode=state["floodlightmode"],
        uses_recorder_deterrence=lambda: state["recorder"],
        supports_nvr_active_deterrence=lambda: state["nvr_deterrence"],
        supports_siren=lambda: state["siren"],
        supports_security_light=lambda: state["security_light"],
        is_amcrest_doorbell=lambda: state["amcrest_doorbell"],
        is_flood_light=lambda: state["flood_light"],
        _wanted_by=lambda *wanted: any(w in state["platforms"] for w in wanted),
    )
    for name in ("creates_siren_entity", "creates_security_light_entity",
                 "reads_coaxial_status"):
        setattr(c, name, getattr(DahuaDataUpdateCoordinator, name).__get__(c))
    return c


# --- the measured case: a recorder that has neither --------------------------

def test_a_recorder_without_the_opt_in_is_not_asked():
    """The DHI-NVR5464 here. Eleven entries, no siren and no security light entity, and
    the endpoint asked on every poll regardless."""
    assert _coordinator(recorder=True, nvr_deterrence=False).reads_coaxial_status() is False


def test_a_recorder_with_the_opt_in_is_asked():
    """Ticking NVR active deterrence creates the Alarm and Warning Light, and those read
    this status, so it has to be fetched."""
    assert _coordinator(recorder=True, nvr_deterrence=True).reads_coaxial_status() is True


# --- a direct camera ---------------------------------------------------------

def test_a_camera_with_a_siren_is_asked():
    assert _coordinator(siren=True).reads_coaxial_status() is True


def test_a_camera_with_a_security_light_is_asked():
    assert _coordinator(security_light=True).reads_coaxial_status() is True


def test_a_camera_with_neither_is_not_asked():
    assert _coordinator().reads_coaxial_status() is False


# --- the flood light, which is the easy one to miss -------------------------

def test_a_flood_light_that_reads_this_status_is_asked():
    """`is_flood_light_on` reads WhiteLight out of this status when the camera reports
    floodlightmode. Narrowing the poll to the siren and security light alone would have
    left this entity reading a value nobody fetched, and it would have gone on existing
    and simply stopped changing."""
    coordinator = _coordinator(flood_light=True, floodlightmode=True)

    assert coordinator.reads_coaxial_status() is True


def test_a_flood_light_on_firmware_without_floodlightmode_is_not_asked():
    """The same entity reads Lighting_V2 instead on that firmware, so this would be
    fetched for nothing."""
    coordinator = _coordinator(flood_light=True, floodlightmode=False)

    assert coordinator.reads_coaxial_status() is False


def test_a_flood_light_still_needs_the_light_platform():
    coordinator = _coordinator(flood_light=True, floodlightmode=True, platforms=[SWITCH])

    assert coordinator.reads_coaxial_status() is False


# --- a platform switched off means nothing reads it --------------------------

def test_a_siren_with_the_switch_platform_off_is_not_asked():
    """Turning a platform off is documented as reducing what the device is asked, not
    just what you see."""
    coordinator = _coordinator(siren=True, platforms=[LIGHT])

    assert coordinator.reads_coaxial_status() is False


def test_a_security_light_with_the_light_platform_off_is_not_asked():
    coordinator = _coordinator(security_light=True, platforms=[SWITCH])

    assert coordinator.reads_coaxial_status() is False


def test_a_siren_is_asked_even_when_only_the_switch_platform_is_on():
    coordinator = _coordinator(siren=True, platforms=[SWITCH])

    assert coordinator.reads_coaxial_status() is True


def test_no_platforms_at_all_asks_for_nothing():
    coordinator = _coordinator(siren=True, security_light=True, flood_light=True,
                               floodlightmode=True, platforms=[])

    assert coordinator.reads_coaxial_status() is False


# --- the Amcrest doorbell's security light is a select, not a light ----------

def test_an_amcrest_doorbell_creates_no_security_light():
    """select.py builds it instead, and that reads Lighting_V2."""
    coordinator = _coordinator(security_light=True, amcrest_doorbell=True)

    assert coordinator.creates_security_light_entity() is False


def test_an_amcrest_doorbell_is_not_asked_for_this_status():
    coordinator = _coordinator(security_light=True, amcrest_doorbell=True,
                               platforms=[LIGHT, SELECT])

    assert coordinator.reads_coaxial_status() is False


def test_an_amcrest_doorbell_with_a_siren_is_still_asked():
    """The exclusion is the security light's, not the whole device's."""
    coordinator = _coordinator(siren=True, amcrest_doorbell=True)

    assert coordinator.reads_coaxial_status() is True


# --- the predicates are the platforms' own rule ------------------------------

def test_the_recorder_opt_in_decides_the_siren_on_a_recorder():
    assert _coordinator(recorder=True, nvr_deterrence=True,
                        siren=False).creates_siren_entity() is True
    assert _coordinator(recorder=True, nvr_deterrence=False,
                        siren=True).creates_siren_entity() is False


def test_the_model_decides_the_siren_on_a_direct_camera():
    assert _coordinator(recorder=False, siren=True).creates_siren_entity() is True
    assert _coordinator(recorder=False, siren=False).creates_siren_entity() is False


# --- and the platforms have to use them, or the rule is not shared ----------

@pytest.mark.parametrize("module,predicate", [
    ("switch.py", "creates_siren_entity"),
    ("light.py", "creates_security_light_entity"),
])
def test_the_platform_asks_the_coordinator_rather_than_repeating_the_rule(module, predicate):
    """The whole point of moving it. If a platform goes back to spelling the rule out,
    the poll and the platform can disagree again, and the direction that breaks is a
    entity reading a value nobody fetched."""
    import io
    from pathlib import Path

    source = io.open(
        Path(__file__).resolve().parents[2] / "custom_components" / "dahua" / module,
        encoding="utf-8").read()

    assert "coordinator.%s()" % predicate in source, (
        "%s does not use coordinator.%s" % (module, predicate))
    assert "supports_nvr_active_deterrence()" not in source, (
        "%s still spells out the recorder rule itself" % module)


def test_the_poll_gates_the_fetch_on_the_shared_rule():
    """Driving the real poll needs the whole of Home Assistant, so this reads the source:
    the coaxial branch must be gated on `reads_coaxial_status`, not on `_wanted_by`."""
    import ast
    import io
    from pathlib import Path

    source = io.open(
        Path(__file__).resolve().parents[2]
        / "custom_components" / "dahua" / "__init__.py", encoding="utf-8").read()
    update = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_update_data")

    # An if/elif chain is nested `If` nodes, so the outer one contains the inner branch
    # too. The gate wanted is the branch whose own *body* makes the call.
    gates = [
        ast.unparse(node.test) for node in ast.walk(update)
        if isinstance(node, ast.If)
        and any("_async_coaxial_status" in ast.unparse(stmt) for stmt in node.body)]

    assert len(gates) == 1, "expected one coaxial branch, found %s" % gates
    assert "self.reads_coaxial_status()" in gates[0], (
        "the coaxial fetch is not gated on the shared rule: %s" % gates[0])
