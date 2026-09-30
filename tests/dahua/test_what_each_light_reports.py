"""Which light a device gets, and what each of them tells Home Assistant it can do.

`light.py` builds five different entities and none of their property surfaces was
executed by the suite. They are one-line properties, which is exactly why nobody wrote
tests for them, and also why a mistake in one would be invisible: nothing raises, the
entity just offers the wrong controls for ever.

The one that matters is `color_mode`. Two of these lights are dimmable and report
`BRIGHTNESS`; three are on-off and report `ONOFF`. Home Assistant decides whether to draw
a brightness slider from that, so the wrong answer either hides a control that works or
offers one the camera will ignore. `supported_color_modes` is `{self.color_mode}` on all
five, so it follows whatever `color_mode` says and the two have to agree by construction
rather than by coincidence.

`async_setup_entry`'s five conditions decide which of them a given device gets at all,
and each is a different question asked of the coordinator.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.components.light import ColorMode, LightEntityFeature

from custom_components.dahua.light import (
    AmcrestRingLight,
    DahuaIlluminator,
    DahuaInfraredLight,
    DahuaSecurityLight,
    FloodLight,
    async_setup_entry,
)

from . import adds_entities

# Which lights dim and which only switch. The split is the point of the test: it is
# what Home Assistant draws a brightness slider from.
DIMMABLE = (DahuaInfraredLight, DahuaIlluminator)
ON_OFF = (FloodLight, DahuaSecurityLight, AmcrestRingLight)
EVERY_LIGHT = DIMMABLE + ON_OFF


class _Coordinator:
    """Answers what the lights read, with every capability off by default."""

    subentry_id = None
    # CoordinatorEntity.__init__ reads these off whatever it is handed.
    config_entry = None
    last_update_success = True
    data = {}

    def __init__(self, **capabilities):
        self._capabilities = capabilities
        self.infrared_on = False
        self.illuminator_on = False
        self.flood_on = False
        self.security_on = False
        self.ring_on = False

    def __getattr__(self, name):
        # Capability questions are all `supports_x()` / `is_x()` / `creates_x()`,
        # and a light only ever asks yes or no. Anything a test did not name is no.
        if name.startswith(("supports_", "is_", "creates_", "uses_")):
            return lambda *a, **k: self._capabilities.get(name, False)
        raise AttributeError(name)

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Front Gate"

    def is_infrared_light_on(self):
        return self.infrared_on

    def is_illuminator_on(self):
        return self.illuminator_on

    def is_flood_light_on(self):
        return self.flood_on

    def is_security_light_on(self):
        return self.security_on

    def is_ring_light_on(self):
        return self.ring_on


def _light(cls, coordinator=None):
    """Build the entity without Home Assistant's entity plumbing, the way
    `test_light.py` does. None of the properties here touches it."""
    entity = object.__new__(cls)
    entity._coordinator = coordinator or _Coordinator()
    entity.coordinator = entity._coordinator
    entity.async_write_ha_state = Mock()
    entity._manual_on = False
    return entity


# --- what each light says it can do -----------------------------------------

@pytest.mark.parametrize("cls", DIMMABLE)
def test_a_dimmable_light_reports_brightness(cls):
    """Home Assistant draws the brightness slider from this. Reporting ONOFF here
    would hide a control that works."""
    assert _light(cls).color_mode == ColorMode.BRIGHTNESS


@pytest.mark.parametrize("cls", ON_OFF)
def test_a_light_that_only_switches_reports_onoff(cls):
    """And the other way round: offering a slider the camera ignores looks like
    the integration is broken rather than like the light having one setting."""
    assert _light(cls).color_mode == ColorMode.ONOFF


@pytest.mark.parametrize("cls", EVERY_LIGHT)
def test_the_supported_modes_are_exactly_the_one_it_reports(cls):
    """`supported_color_modes` is `{self.color_mode}` on all five. Asserted against
    `color_mode` rather than against a literal, so the two cannot drift apart: a
    light whose set named a mode it does not report would be rejected by Home
    Assistant at registration."""
    light = _light(cls)

    assert light.supported_color_modes == {light.color_mode}


@pytest.mark.parametrize("cls", EVERY_LIGHT)
def test_no_light_is_polled(cls):
    """They are all driven by the coordinator's own poll. A light that said it
    wanted polling would be asked on Home Assistant's schedule as well."""
    assert _light(cls).should_poll is False


@pytest.mark.parametrize("cls", (DahuaInfraredLight, FloodLight))
def test_the_lights_with_effects_say_so(cls):
    assert _light(cls).supported_features == LightEntityFeature.EFFECT


# --- and whether it is on ---------------------------------------------------

@pytest.mark.parametrize("cls, attribute", [
    (DahuaInfraredLight, "infrared_on"),
    (FloodLight, "flood_on"),
    (DahuaSecurityLight, "security_on"),
    (AmcrestRingLight, "ring_on"),
])
@pytest.mark.parametrize("state", [True, False])
def test_each_light_reads_its_own_state(cls, attribute, state):
    """Four lights, four different questions to the coordinator. They are one line
    each and one of them reading another's answer would be silent."""
    coordinator = _Coordinator()
    setattr(coordinator, attribute, state)

    assert _light(cls, coordinator).is_on is state


# --- which lights a device gets ---------------------------------------------

async def _built(hass, **capabilities):
    added = []
    entry = SimpleNamespace(entry_id="e1",
                            runtime_data={0: _Coordinator(**capabilities)})
    await async_setup_entry(hass, entry, adds_entities(added))
    return [type(entity) for entity in added]


async def test_a_camera_with_nothing_gets_no_lights(hass):
    """The common case. Most cameras have neither an illuminator nor a siren, and
    a platform that added entities anyway would give every one of them five
    controls that do nothing."""
    assert await _built(hass) == []


@pytest.mark.parametrize("capability, cls", [
    ("supports_infrared_light", DahuaInfraredLight),
    ("supports_illuminator", DahuaIlluminator),
    ("is_flood_light", FloodLight),
    ("creates_security_light_entity", DahuaSecurityLight),
    ("is_amcrest_doorbell", AmcrestRingLight),
])
async def test_each_capability_adds_its_own_light(hass, capability, cls):
    """Five conditions, five different questions. Each is tested alone so that one
    of them answering for another cannot pass unnoticed."""
    assert await _built(hass, **{capability: True}) == [cls]


async def test_a_device_can_have_several_at_once(hass):
    """An NVR channel with a deterrence camera on it really does have both, and
    the entities are independent."""
    built = await _built(hass, supports_illuminator=True,
                         creates_security_light_entity=True)

    assert set(built) == {DahuaIlluminator, DahuaSecurityLight}


async def test_a_recorder_calls_its_deterrence_light_a_warning_light(hass):
    """Same entity, different name. A recorder's deterrence output is a warning
    light in its own UI, and calling it a security light there sends people looking
    for a setting under the wrong name."""
    added = []
    entry = SimpleNamespace(entry_id="e1", runtime_data={0: _Coordinator(
        creates_security_light_entity=True, uses_recorder_deterrence=True)})

    await async_setup_entry(hass, entry, adds_entities(added))

    assert added[0]._attr_translation_key == "warning_light"


async def test_a_camera_calls_it_a_security_light(hass):
    """The negative control for the line above."""
    added = []
    entry = SimpleNamespace(entry_id="e1", runtime_data={0: _Coordinator(
        creates_security_light_entity=True)})

    await async_setup_entry(hass, entry, adds_entities(added))

    assert added[0]._attr_translation_key == "security_light"
