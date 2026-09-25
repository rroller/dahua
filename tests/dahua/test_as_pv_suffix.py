"""One character decided whether a camera had a siren.

#676. The checks asked for `-AS-PV`, with a leading hyphen. Dahua's own naming
puts a lens or sensor letter immediately before that suffix on some models, so
`DH-IPC-HDBW3549R1-ZAS-PV` contains `AS-PV` and does not contain `-AS-PV`.
@isisava's camera is one of those: it plainly has both a siren and a deterrence
light, and got neither entity.

This only widens the match to the family that already matched. It cannot reach
a camera that was not going to match anyway, so nothing gains an entity it was
not already meant to have.

The broader complaint in #676, OEM rebrands with no usable name at all, is not
solved by this and cannot be solved by matching names. That is what the RPC2
capability probe in #717 is for.
"""
from custom_components.dahua import DahuaDataUpdateCoordinator


def _camera(model):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.model = model
    return c


def test_the_reported_camera_gets_its_siren_and_light():
    """DH-IPC-HDBW3549R1-ZAS-PV, from #676, the case this is for."""
    c = _camera("DH-IPC-HDBW3549R1-ZAS-PV")

    assert c.supports_siren() is True
    assert c.supports_security_light() is True


def test_the_plain_suffix_still_matches():
    c = _camera("IPC-HDW3849HP-AS-PV")

    assert c.supports_siren() is True
    assert c.supports_security_light() is True


def test_a_camera_without_the_suffix_gains_nothing():
    c = _camera("IPC-HFW1431S-S4")

    assert c.supports_siren() is False
    assert c.supports_security_light() is False


def test_a_colour_camera_without_deterrence_gains_nothing():
    """The suffix is what marks deterrence, not the rest of the name."""
    c = _camera("IPC-Color4K-T-S2")

    assert c.supports_siren() is False
    assert c.supports_security_light() is False
