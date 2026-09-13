"""Model gating for active-deterrence light entities."""

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator(model: str) -> DahuaDataUpdateCoordinator:
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.model = model
    return coordinator


def test_ipc_color4m_tz_exposes_the_security_light():
    """The IPC-Color4M-TZ's red/blue LEDs use the existing coaxial CGI."""
    assert _coordinator("IPC-Color4M-TZ").supports_security_light()


def test_ipc_color4m_tz_suffix_still_exposes_the_security_light():
    """OEM firmware sometimes appends lens or region text to the model."""
    assert _coordinator("IPC-Color4M-TZ-2.8mm").supports_security_light()


def test_an_unrelated_color_camera_does_not_gain_a_security_light():
    assert not _coordinator("IPC-Color4K-T-S2").supports_security_light()
