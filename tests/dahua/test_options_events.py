"""The event subscription must be changeable after setup, not only at setup."""

from types import SimpleNamespace

from custom_components.dahua import get_configured_events
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua.config_flow import DahuaOptionsFlowHandler
from custom_components.dahua.const import DEFAULT_EVENTS, DOMAIN

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


# --- an entry that has never carried an event list at all --------------------
#
# Reachable by an entry old enough to predate the setting. This used to return
# None, and None is not a list: binary_sensor.async_setup_entry iterates it with
# no guard, so the whole platform raised TypeError and the device got no binary
# sensors at all -- while async_start_event_listener skipped the stream quietly,
# because that one does guard. No events, no sensors, and nothing in the log
# connecting the two.

def test_an_entry_with_no_event_list_anywhere_gets_the_defaults():
    assert get_configured_events(_entry()) == DEFAULT_EVENTS


def test_it_never_returns_none():
    """The contract the binary_sensor platform relies on."""
    for entry in (_entry(), _entry(data_events=SETUP_EVENTS),
                  _entry(option_events=[]), _entry(data_events=[]),
                  _entry(data_events=SETUP_EVENTS, option_events=CHOSEN_EVENTS)):
        assert get_configured_events(entry) is not None


def test_the_defaults_include_the_smart_motion_codes():
    """Two of the nine are what #728's reporters lose. A default that omitted
    them would look like this bug fixed while leaving it in place."""
    assert "SmartMotionHuman" in DEFAULT_EVENTS
    assert "SmartMotionVehicle" in DEFAULT_EVENTS


def test_an_explicitly_empty_setup_value_is_still_honoured():
    """Absent and empty are different. Empty was chosen; absent never was."""
    assert get_configured_events(_entry(data_events=[])) == []


def test_the_caller_cannot_mutate_the_stored_list():
    """A list returned straight out of entry.data is the entry's own object, and
    an accidental append would edit the stored config."""
    stored = list(SETUP_EVENTS)
    entry = _entry(data_events=stored)

    got = get_configured_events(entry)
    got.append("VideoBlind")

    assert stored == SETUP_EVENTS


def _schema_defaults(result):
    """Maps field name -> resolved default from a shown form."""
    out = {}
    schema = result["data_schema"].schema
    for marker in schema:
        default = getattr(marker, "default", None)
        out[str(marker.schema)] = default() if callable(default) else default
        # A collapsed section is presentation, not a different set of fields. These
        # tests ask whether a field is offered, so descend into it rather than
        # reporting the section itself as the answer.
        nested = getattr(schema[marker], "schema", None)
        if nested is not None and hasattr(nested, "schema"):
            for inner in nested.schema:
                inner_default = getattr(inner, "default", None)
                out[str(inner.schema)] = (inner_default() if callable(inner_default)
                                          else inner_default)
    return out


async def _shown_options_form(hass, entry):
    registered = MockConfigEntry(
        domain=DOMAIN, data=dict(entry.data), options=dict(entry.options))
    registered.add_to_hass(hass)

    handler = DahuaOptionsFlowHandler()
    handler.hass = hass
    # Home Assistant resolves config_entry by looking the id up in hass, so the
    # entry has to be registered there and the flow's handler has to carry that
    # id. Assigning the entry onto the handler, by any attribute name, stopped
    # working once the id became the only link.
    handler.handler = registered.entry_id
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
