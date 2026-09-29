"""The plate sensor must let go of its coordinator subscription when removed.

`add_plate_listener` returns the callback that undoes the subscription, and the
authorized vehicle sensor hands it to `async_on_remove` (#842). This sensor was
the fourth caller of `add_plate_listener` and did not, so a removed one kept
being called on every recognised plate -- and on the Home Assistant version this
repo targets, the removed entity's state is written back into the state machine.
"""

import pytest

from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.sensor import DahuaLicensePlateSensor


class _Coordinator:
    # The real method, not a stand-in: a fake returning None would have been
    # accepted by async_on_remove without a word.
    add_plate_listener = DahuaDataUpdateCoordinator.add_plate_listener

    def __init__(self):
        self._plate_listeners = []

    def get_serial_number(self):
        return "SERIAL1"

    def get_last_plate(self):
        return "unknown"

    def get_last_plate_data(self):
        return {}


@pytest.fixture
async def plate_sensor(hass, monkeypatch):
    """A real plate sensor, attached to a real hass.

    Only `schedule_update_ha_state` is stubbed -- the entity is not registered
    with a platform, so the real one has nothing to write to.
    """
    monkeypatch.setattr(
        DahuaLicensePlateSensor,
        "schedule_update_ha_state",
        lambda self, force_refresh=False: None,
        raising=False,
    )

    def build(coordinator):
        entity = DahuaLicensePlateSensor(coordinator, object())
        entity.hass = hass
        return entity

    return build


async def test_removal_stops_the_plate_subscription(plate_sensor):
    """Without the removal the callback outlives the entity."""
    coordinator = _Coordinator()
    sensor = plate_sensor(coordinator)

    await sensor.async_added_to_hass()
    assert coordinator._plate_listeners, "never subscribed, so the rest proves nothing"

    assert sensor._on_remove, "registered nothing to undo the subscription"
    for undo in list(sensor._on_remove):
        undo()

    assert coordinator._plate_listeners == [], "the callback outlived the entity"
