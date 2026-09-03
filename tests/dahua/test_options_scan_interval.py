"""How often an entry polls its device must be configurable after setup."""

from datetime import timedelta
from types import SimpleNamespace

from custom_components.dahua import get_configured_scan_interval
from custom_components.dahua.config_flow import DahuaOptionsFlowHandler
from custom_components.dahua.const import DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL


def _entry(**options):
    return SimpleNamespace(data={}, options=dict(options))


def test_defaults_to_the_previous_hardcoded_interval():
    """Entries that never set the option keep the behaviour they had."""
    assert get_configured_scan_interval(_entry()) == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


def test_uses_the_configured_interval():
    assert get_configured_scan_interval(_entry(scan_interval=300)) == timedelta(seconds=300)


def test_values_below_the_minimum_are_raised_to_it():
    """A hand-edited entry must not be able to hammer the device."""
    assert get_configured_scan_interval(_entry(scan_interval=1)) == timedelta(
        seconds=MIN_SCAN_INTERVAL
    )


def test_a_nonsense_value_falls_back_to_the_default():
    for bad in ("", None, "abc", []):
        assert get_configured_scan_interval(_entry(scan_interval=bad)) == timedelta(
            seconds=DEFAULT_SCAN_INTERVAL
        ), f"{bad!r} did not fall back"


def test_a_numeric_string_is_accepted():
    """Home Assistant can hand back a string from a number field."""
    assert get_configured_scan_interval(_entry(scan_interval="120")) == timedelta(seconds=120)


def _schema_defaults(result):
    out = {}
    for marker in result["data_schema"].schema:
        default = getattr(marker, "default", None)
        out[str(marker.schema)] = default() if callable(default) else default
    return out


def _validators(result):
    return {str(m.schema): v for m, v in result["data_schema"].schema.items()}


async def _shown_options_form(hass, entry):
    handler = DahuaOptionsFlowHandler()
    handler.hass = hass
    # Assigning config_entry is deprecated and raises under the test harness;
    # set the attribute Home Assistant's own flow manager populates.
    handler._config_entry = entry
    handler.options = dict(entry.options)
    return await handler.async_step_user()


async def test_options_form_offers_the_interval(hass):
    defaults = _schema_defaults(await _shown_options_form(hass, _entry()))

    assert "scan_interval" in defaults, "the options screen does not expose the interval"
    assert defaults["scan_interval"] == DEFAULT_SCAN_INTERVAL


async def test_options_form_defaults_to_the_chosen_interval(hass):
    defaults = _schema_defaults(await _shown_options_form(hass, _entry(scan_interval=600)))

    assert defaults["scan_interval"] == 600


async def test_options_form_rejects_an_interval_below_the_minimum(hass):
    """The screen should refuse it rather than relying on the read-side clamp."""
    import voluptuous as vol

    validate = _validators(await _shown_options_form(hass, _entry()))["scan_interval"]

    assert validate(MIN_SCAN_INTERVAL) == MIN_SCAN_INTERVAL
    try:
        validate(MIN_SCAN_INTERVAL - 1)
    except vol.Invalid:
        pass
    else:
        raise AssertionError("accepted an interval below the minimum")


async def test_platform_toggles_are_still_offered(hass):
    defaults = _schema_defaults(await _shown_options_form(hass, _entry()))

    for field in ("camera", "light", "switch", "binary_sensor", "select"):
        assert field in defaults, f"lost the {field} toggle"
    assert "auto_detect_channel" in defaults
