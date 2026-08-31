"""The event subscription must be changeable after setup, not only at setup."""

from types import SimpleNamespace

from custom_components.dahua import get_configured_events
from custom_components.dahua.config_flow import DahuaOptionsFlowHandler

SETUP_EVENTS = ["VideoMotion", "CrossLineDetection", "AudioMutation"]
CHOSEN_EVENTS = ["VideoMotion"]


def _entry(data_events=None, option_events=None):
    data = {"events": data_events} if data_events is not None else {}
    options = {"events": option_events} if option_events is not None else {}
    return SimpleNamespace(data=data, options=options)


def test_options_win_over_the_setup_value():
    entry = _entry(data_events=SETUP_EVENTS, option_events=CHOSEN_EVENTS)
    assert get_configured_events(entry) == CHOSEN_EVENTS


def test_setup_value_is_used_when_the_option_was_never_set():
    """Entries created before this option existed must keep working."""
    entry = _entry(data_events=SETUP_EVENTS)
    assert get_configured_events(entry) == SETUP_EVENTS


def test_an_empty_selection_is_honoured_not_treated_as_unset():
    """Deselecting every event means "none", not "fall back to setup"."""
    entry = _entry(data_events=SETUP_EVENTS, option_events=[])
    assert get_configured_events(entry) == []


def _schema_defaults(result):
    """Maps field name -> resolved default from a shown form."""
    out = {}
    for marker in result["data_schema"].schema:
        default = getattr(marker, "default", None)
        out[str(marker.schema)] = default() if callable(default) else default
    return out


async def _shown_options_form(hass, entry):
    handler = DahuaOptionsFlowHandler()
    handler.hass = hass
    # Assigning config_entry is deprecated and raises under the test harness;
    # set the attribute Home Assistant's own flow manager populates.
    handler._config_entry = entry
    handler.options = dict(entry.options)
    return await handler.async_step_user()


async def test_options_form_offers_events_defaulted_to_the_setup_value(hass):
    entry = _entry(data_events=SETUP_EVENTS)

    defaults = _schema_defaults(await _shown_options_form(hass, entry))

    assert "events" in defaults, "the options screen does not expose events"
    assert defaults["events"] == SETUP_EVENTS


async def test_options_form_defaults_to_the_previously_chosen_events(hass):
    entry = _entry(data_events=SETUP_EVENTS, option_events=CHOSEN_EVENTS)

    defaults = _schema_defaults(await _shown_options_form(hass, entry))

    assert defaults["events"] == CHOSEN_EVENTS


async def test_platform_toggles_are_still_offered(hass):
    """Adding events must not displace what the screen already had."""
    defaults = _schema_defaults(await _shown_options_form(hass, _entry(SETUP_EVENTS)))

    for field in ("camera", "light", "switch", "binary_sensor", "select"):
        assert field in defaults, f"lost the {field} toggle"
    assert "auto_detect_channel" in defaults
