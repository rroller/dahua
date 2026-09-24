"""go2rtc holds the RTSP talk channel while HA streams unless told not to (#595).

On a doorbell that leaves it showing a call in progress (the AD410's ring goes
navy instead of turquoise) and the vendor app cannot talk through it. The
option adds go2rtc's #backchannel=0 to the stream source.
"""

from types import SimpleNamespace

from custom_components.dahua.camera import DahuaCamera, rtsp_stream_source
from custom_components.dahua.config_flow import DahuaOptionsFlowHandler

URL = "rtsp://host:554/cam/realmonitor?channel=1&subtype=0"


def test_the_url_is_unchanged_by_default():
    assert rtsp_stream_source(URL, False) == URL


def test_disabling_the_backchannel_adds_the_go2rtc_fragment():
    assert rtsp_stream_source(URL, True) == URL + "#backchannel=0"


class _Client:
    @staticmethod
    def to_stream_name(subtype):
        return "Main" if subtype == 0 else "Sub"

    @staticmethod
    def get_rtsp_stream_url(channel, subtype):
        return "rtsp://host/cam?channel=%s&subtype=%s" % (channel, subtype)


class _Coordinator:
    client = _Client()

    def get_channel(self):
        return 0

    def get_channel_number(self):
        return 1

    def get_serial_number(self):
        return "SERIAL"


def _entry(**options):
    return SimpleNamespace(title="Front Door", data={}, options=dict(options))


async def _source(**options):
    return await DahuaCamera(_Coordinator(), 0, _entry(**options)).stream_source()


async def test_an_entry_without_the_option_keeps_the_backchannel():
    """Existing installs must not lose talking from Home Assistant."""
    assert await _source() == "rtsp://host/cam?channel=1&subtype=0"


async def test_the_option_reaches_the_camera_stream_source():
    assert await _source(disable_backchannel=True) == (
        "rtsp://host/cam?channel=1&subtype=0#backchannel=0"
    )


def _schema_defaults(result):
    out = {}
    for marker in result["data_schema"].schema:
        default = getattr(marker, "default", None)
        out[str(marker.schema)] = default() if callable(default) else default
    return out


async def _shown_options_form(hass, entry):
    handler = DahuaOptionsFlowHandler()
    handler.hass = hass
    handler._config_entry = entry
    handler.options = dict(entry.options)
    return await handler.async_step_user()


async def test_the_options_form_offers_it_disabled_by_default(hass):
    defaults = _schema_defaults(await _shown_options_form(hass, _entry()))

    assert defaults["disable_backchannel"] is False


async def test_the_options_form_preserves_the_configured_value(hass):
    defaults = _schema_defaults(
        await _shown_options_form(hass, _entry(disable_backchannel=True))
    )

    assert defaults["disable_backchannel"] is True
