"""A recorder's configured-channel count as a diagnostic sensor (Hikvision parity).

Read once from the recorder's RemoteDevice table at setup, like the disk
sensors, so it adds nothing to the poll. It counts configured (enabled) slots:
the table says a camera is set up on a channel, not that it is reachable now.
"""

from types import SimpleNamespace

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.sensor import DahuaConfiguredChannelsSensor


def _coordinator(remote):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._remote_devices = remote
    return c


def test_counts_only_the_enabled_slots():
    c = _coordinator(
        {
            0: {"enabled": True, "protocol": "private"},
            1: {"enabled": False, "protocol": ""},
            2: {"enabled": True, "protocol": "onvif"},
        }
    )
    assert c.get_configured_channel_count() == 2


def test_no_table_is_none_not_zero():
    """None, so the sensor is not created on a non-recorder or after a failed
    read; zero would wrongly claim a recorder that has no cameras."""
    assert _coordinator({}).get_configured_channel_count() is None


def _sensor(count):
    s = object.__new__(DahuaConfiguredChannelsSensor)
    s._coordinator = SimpleNamespace(
        get_configured_channel_count=lambda: count,
        get_serial_number=lambda: "SER1",
    )
    return s


def test_the_sensor_reports_the_count():
    assert _sensor(15).native_value == 15


def test_the_sensor_id_is_the_device_serial():
    assert _sensor(15).unique_id == "SER1_configured_channels"


def test_the_sensor_is_off_by_default():
    """A recorder is one entry per channel and all answer is_recorder_host, so
    an enabled-by-default sensor would appear once per channel. Off by default,
    like the disk sensors, so the host-wide count is opt-in rather than repeated."""
    assert _sensor(15).entity_registry_enabled_default is False
