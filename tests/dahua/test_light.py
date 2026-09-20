"""light.py had no tests at all, and day/night handling has regressed before."""

from unittest.mock import Mock

import pytest

from custom_components.dahua import dahua_utils
from custom_components.dahua.light import DahuaIlluminator, DahuaInfraredLight

ATTR_BRIGHTNESS = "brightness"


class _Client:
    def __init__(self):
        self.v1 = []
        self.v2 = []
        self.v2_raw = []
        self.scheme_calls = []
        self.scheme_reads = 0
        self.scheme_writes = []
        self.operations = []

        # Default camera state used by the existing illuminator tests.
        self.scheme = "AIMode"
        self.light_mode = "Manual"
        self.light_brightness = 64
        self.light_field = "NearLight"

    async def async_set_lighting_v1(self, channel, enabled, brightness, profile_mode="0"):
        self.v1.append((channel, enabled, brightness, profile_mode))

    async def async_get_lighting_scheme_mode(self, channel, profile_mode):
        return self.scheme

    async def async_get_lighting_scheme(self):
        self.scheme_reads += 1
        return {}

    async def async_set_lighting_scheme_illuminator(
        self, channel, enabled, brightness, profile_mode, light_index
    ):
        self.scheme_calls.append(
            (channel, enabled, brightness, profile_mode, light_index)
        )

    async def async_set_lighting_scheme(self, channel, profile_mode, mode):
        previous = self.scheme
        self.scheme = mode
        self.scheme_writes.append(
            (channel, profile_mode, mode)
        )
        self.operations.append(
            ("scheme", channel, profile_mode, mode)
        )
        return previous

    async def async_get_lighting_v2_live_state(
        self,
        channel,
        profile_mode,
        light_index=0,
    ):
        return (
            self.light_mode,
            self.light_field,
            self.light_brightness,
        )

    async def async_set_lighting_v2(
        self,
        channel,
        enabled,
        brightness,
        profile_mode,
        light_index=0,
        bank="MiddleLight",
    ):
        self.v2.append(
            (
                channel,
                enabled,
                brightness,
                profile_mode,
                light_index,
                bank,
            )
        )
        self.operations.append(
            (
                "v2",
                channel,
                enabled,
                brightness,
                profile_mode,
                light_index,
                bank,
            )
        )

        if enabled:
            self.light_mode = "Manual"
            self.light_brightness = brightness
            self.light_field = bank
        else:
            self.light_mode = "Off"

    async def async_set_lighting_v2_raw(
        self,
        channel,
        profile_mode,
        light_index,
        mode,
        bank="MiddleLight",
        brightness=None,
    ):
        self.v2_raw.append(
            (
                channel,
                profile_mode,
                light_index,
                mode,
                bank,
                brightness,
            )
        )
        self.operations.append(
            (
                "v2_raw",
                channel,
                profile_mode,
                light_index,
                mode,
                bank,
                brightness,
            )
        )

        self.light_mode = mode
        self.light_brightness = brightness
        self.light_field = bank


class _Store:
    """Minimal in-memory replacement for Home Assistant Store."""

    def __init__(self):
        self.data = None
        self.saved = []
        self.removed = 0

    async def async_save(self, data):
        self.data = dict(data)
        self.saved.append(dict(data))

    async def async_load(self):
        if self.data is None:
            return None
        return dict(self.data)

    async def async_remove(self):
        self.data = None
        self.removed += 1


class _Coordinator:
    def __init__(self, channel=3, profile_mode="1", uses_scheme=False):
        self.client = _Client()
        self._channel = channel
        self._profile_mode = profile_mode
        self.refreshed = 0
        self.infrared_on = True
        self.infrared_brightness = 128
        self.illuminator_on = False
        self.illuminator_brightness = 64
        # Which light this device calls the white one; 0 on most models.
        self.illuminator_index = 0
        # Which brightness bank the white light uses; MiddleLight on most models.
        self.illuminator_bank = "MiddleLight"
        self.camera_reboot_generation = 0
        self.uses_scheme = uses_scheme

    def get_channel(self):
        return self._channel

    def get_profile_mode(self):
        return self._profile_mode

    def get_infrared_profile(self):
        """The profile the infrared light really uses; the live one here."""
        return self._profile_mode

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Front Door"

    def is_infrared_light_on(self):
        return self.infrared_on

    def get_infrared_brightness(self):
        return self.infrared_brightness

    def is_illuminator_on(self):
        return self.illuminator_on

    def get_illuminator_brightness(self):
        return self.illuminator_brightness

    def get_illuminator_index(self):
        return self.illuminator_index

    def get_illuminator_bank(self):
        return self.illuminator_bank

    def get_camera_reboot_generation(self):
        return self.camera_reboot_generation

    def uses_lighting_scheme_illuminator(self):
        return self.uses_scheme

    async def async_refresh(self):
        self.refreshed += 1


def _light(cls, coordinator, name="Infrared"):
    """Build the entity without Home Assistant's entity plumbing."""
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    entity._name = name
    # These unit tests bypass entity registration; observe state writes without
    # asking Home Assistant to publish an unregistered entity.
    entity.async_write_ha_state = Mock()

    if cls is DahuaIlluminator:
        entity._manual_on = False
        entity._scheme_restore = None
        entity._light_restore = None
        entity._last_brightness = 255
        entity._restore_store = _Store()
        entity._seen_reboot_generation = (
            coordinator.get_camera_reboot_generation()
        )
        entity._reboot_recovery_task = None

    return entity


# --- brightness conversion -------------------------------------------------

@pytest.mark.parametrize("hass_value,expected", [
    (0, 0),
    (255, 100),
    (128, 50),
    (None, 100),   # no brightness given means full, not off
])
def test_hass_brightness_maps_to_dahua_scale(hass_value, expected):
    assert dahua_utils.hass_brightness_to_dahua_brightness(hass_value) == expected


@pytest.mark.parametrize("dahua_value,expected", [
    ("0", 0),
    ("100", 255),
    ("50", 127),
    ("", 255),     # blank means full
    (None, 255),
])
def test_dahua_brightness_maps_back_to_hass_scale(dahua_value, expected):
    assert dahua_utils.dahua_brightness_to_hass_brightness(dahua_value) == expected


def test_the_two_conversions_agree_on_what_no_value_means():
    """Both default to "full", but they work on different scales.

    Full is 100 on the Dahua scale and 255 on the HASS scale. Using 100 for
    both made an unspecified brightness mean 39%, so a plain toggle-on lit
    the light dimly.
    """
    no_value_hass = dahua_utils.dahua_brightness_to_hass_brightness(None)
    no_value_dahua = dahua_utils.hass_brightness_to_dahua_brightness(None)

    assert no_value_hass == 255, "full on the HASS scale"
    assert no_value_dahua == 100, "full on the Dahua scale"
    assert dahua_utils.dahua_brightness_to_hass_brightness(str(no_value_dahua)) == no_value_hass


def test_full_and_off_survive_a_round_trip():
    for hass_value in (0, 255):
        dahua = dahua_utils.hass_brightness_to_dahua_brightness(hass_value)
        assert dahua_utils.dahua_brightness_to_hass_brightness(str(dahua)) == hass_value


# --- infrared light (v1 API) ----------------------------------------------

async def test_infrared_turn_on_sends_the_channel_and_brightness():
    c = _Coordinator(channel=3)
    await _light(DahuaInfraredLight, c).async_turn_on(**{ATTR_BRIGHTNESS: 255})

    assert c.client.v1 == [(3, True, 100, "1")], (
        "the write must name the profile the camera is using, not 0")
    assert c.client.v2 == [], "the infrared light must not use the v2 API"
    assert c.refreshed == 1


async def test_infrared_turn_off_sends_enabled_false():
    c = _Coordinator(channel=3)
    await _light(DahuaInfraredLight, c).async_turn_off()

    assert len(c.client.v1) == 1
    channel, enabled, _, profile = c.client.v1[0]
    assert (channel, enabled) == (3, False)
    assert profile == "1", "turning off must reach the same profile as turning on"


async def test_infrared_turn_on_without_brightness_uses_full():
    c = _Coordinator()
    await _light(DahuaInfraredLight, c).async_turn_on()

    assert c.client.v1[0][2] == 100


def test_infrared_reads_state_from_the_coordinator():
    c = _Coordinator()
    light = _light(DahuaInfraredLight, c)

    assert light.is_on is True
    assert light.brightness == 128
    c.infrared_on = False
    assert light.is_on is False


# --- illuminator (v2 API, carries the profile mode) ------------------------

async def test_illuminator_passes_the_profile_mode_through():
    """Day/night handling rides on this argument and has regressed before."""
    c = _Coordinator(channel=2, profile_mode="1")

    await _light(DahuaIlluminator, c, "Illuminator").async_turn_on(**{ATTR_BRIGHTNESS: 255})

    assert c.client.v2 == [
        (2, True, 100, "1", 0, "NearLight")
    ]
    assert c.client.v1 == [], "the illuminator must not use the v1 API"


async def test_illuminator_turn_off_keeps_the_profile_mode():
    c = _Coordinator(channel=2, profile_mode="0")
    light = _light(DahuaIlluminator, c, "Illuminator")
    await light.async_turn_on()
    c._profile_mode = "1"
    await light.async_turn_off()

    assert c.client.v2_raw == [
        (2, "0", 0, "Off", "NearLight", None),
        (2, "0", 0, "Manual", "NearLight", 64),
    ]
    assert c.client.scheme == "AIMode"
    assert light._restore_store.data is None
    light.async_write_ha_state.assert_called()


async def test_illuminator_writes_to_the_light_the_device_calls_white():
    """On a camera that reports index 0 as infrared, writing to 0 changes a
    light nobody can see. The resolved index has to reach the client."""
    c = _Coordinator(channel=2, profile_mode="0")
    c.illuminator_index = 1

    await _light(DahuaIlluminator, c, "Illuminator").async_turn_on()

    assert c.client.v2[0][4] == 1, "the illuminator wrote to the wrong light"


async def test_illuminator_uses_whatever_profile_mode_is_current():
    c = _Coordinator(profile_mode="2")
    await _light(DahuaIlluminator, c, "Illuminator").async_turn_on()
    assert c.client.v2[0][3] == "2"


async def test_scheme_illuminator_uses_the_two_table_client_path():
    c = _Coordinator(channel=0, profile_mode="1", uses_scheme=True)
    c.illuminator_index = 1

    light = _light(DahuaIlluminator, c, "Illuminator")
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})
    await light.async_turn_off(**{ATTR_BRIGHTNESS: 255})

    assert c.client.scheme_calls == [
        (0, True, 100, "1", 1),
        (0, False, 100, "1", 1),
    ]
    assert c.client.v2 == []


# --- identity --------------------------------------------------------------

def test_the_two_lights_do_not_share_a_unique_id():
    """A collision would merge two different lights into one entity."""
    c = _Coordinator()
    infrared = _light(DahuaInfraredLight, c).unique_id
    illuminator = _light(DahuaIlluminator, c, "Illuminator").unique_id

    assert infrared == "SERIAL1_infrared"
    assert illuminator == "SERIAL1_illuminator"
    assert infrared != illuminator


def test_name_is_prefixed_with_the_device_name():
    c = _Coordinator()
    assert _light(DahuaInfraredLight, c, "Infrared").name == "Front Door Infrared"


# --- Smart Dual Light restore regressions -----------------------------------

async def test_duplicate_off_preserves_restored_camera_configuration():
    c = _Coordinator()
    light = _light(DahuaIlluminator, c, "Illuminator")
    await light.async_turn_off()
    assert c.client.operations == []

    await light.async_turn_on()
    await light.async_turn_off()
    operations = list(c.client.operations)
    await light.async_turn_off()

    assert c.client.operations == operations
    assert (c.client.scheme, c.client.light_mode, c.client.light_brightness) == (
        "AIMode", "Manual", 64
    )


async def test_brightness_update_keeps_original_profile_and_restore_snapshot():
    c = _Coordinator(channel=2, profile_mode="0")
    light = _light(DahuaIlluminator, c, "Illuminator")
    await light.async_turn_on()
    c._profile_mode = "1"
    c.illuminator_index = 1
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 128})

    assert c.client.v2[-1] == (2, True, 50, "0", 0, "NearLight")
    assert light._restore_store.data["old_brightness"] == 64
    assert light._restore_store.data["previous_scheme"] == "AIMode"
    await light.async_turn_off()
    assert c.client.v2_raw[-1] == (2, "0", 0, "Manual", "NearLight", 64)


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("mode,brightness", [("Auto", 100), ("Off", 100), ("Manual", 42)])
async def test_external_change_is_preserved_on_off_and_restart(restart, mode, brightness):
    c = _Coordinator()
    light = _light(DahuaIlluminator, c, "Illuminator")
    await light.async_turn_on()
    c.client.light_mode = mode
    c.client.light_brightness = brightness
    c.client.operations.clear()

    if restart:
        recovered_light = _light(DahuaIlluminator, c, "Illuminator")
        recovered_light._restore_store = light._restore_store
        assert await recovered_light._recover_persisted_override() is True
    else:
        await light.async_turn_off()

    assert c.client.operations == []
    assert light._restore_store.data is None
    assert (c.client.light_mode, c.client.light_brightness) == (mode, brightness)


async def test_interrupted_restore_resumes_after_restart(monkeypatch):
    c = _Coordinator()
    light = _light(DahuaIlluminator, c, "Illuminator")
    await light.async_turn_on()
    original_set = c.client.async_set_lighting_v2_raw

    async def fail_restoring_row(channel, profile, index, mode, bank, brightness=None):
        if mode == "Manual":
            raise RuntimeError("Camera disconnected during restore")
        return await original_set(channel, profile, index, mode, bank, brightness)

    monkeypatch.setattr(c.client, "async_set_lighting_v2_raw", fail_restoring_row)
    with pytest.raises(RuntimeError, match="disconnected"):
        await light.async_turn_off()
    assert light._restore_store.data["phase"] == "restoring"
    assert c.client.scheme == "InfraredMode"

    monkeypatch.setattr(c.client, "async_set_lighting_v2_raw", original_set)
    recovered_light = _light(DahuaIlluminator, c, "Illuminator")
    recovered_light._restore_store = light._restore_store
    assert await recovered_light._recover_persisted_override() is True
    assert light._restore_store.data is None
    assert (c.client.scheme, c.client.light_mode, c.client.light_brightness) == (
        "AIMode", "Manual", 64
    )


async def test_illuminator_safe_restore_order():
    """WhiteLight must stay physically off while its original state is restored."""
    c = _Coordinator(channel=2, profile_mode="0")
    light = _light(DahuaIlluminator, c, "Illuminator")

    await light._restore_camera_lighting(
        (2, "0", "AIMode"),
        (2, "0", 1, "NearLight", "Manual", 88),
    )

    assert c.client.operations == [
        ("v2_raw", 2, "0", 1, "Off", "NearLight", None),
        ("scheme", 2, "0", "InfraredMode"),
        ("v2_raw", 2, "0", 1, "Manual", "NearLight", 88),
        ("scheme", 2, "0", "AIMode"),
    ]


async def test_persisted_recovery_restores_whitemode_baseline():
    """WhiteMode cannot be treated as stale only because the scheme matches."""
    c = _Coordinator(channel=2, profile_mode="0")
    c.client.scheme = "WhiteMode"
    c.client.light_brightness = dahua_utils.hass_brightness_to_dahua_brightness(180)

    light = _light(DahuaIlluminator, c, "Illuminator")
    light._restore_store.data = {
        "active": True,
        "scheme_channel": 2,
        "scheme_profile": "0",
        "previous_scheme": "WhiteMode",
        "channel": 2,
        "profile_mode": "0",
        "index": 1,
        "field": "NearLight",
        "old_mode": "Manual",
        "old_brightness": 71,
        "ha_brightness": 180,
    }

    recovered = await light._recover_persisted_override()

    assert recovered is True
    assert light._restore_store.data is None
    assert light._last_brightness == 180

    assert c.client.operations == [
        ("v2_raw", 2, "0", 1, "Off", "NearLight", None),
        ("scheme", 2, "0", "InfraredMode"),
        ("v2_raw", 2, "0", 1, "Manual", "NearLight", 71),
        ("scheme", 2, "0", "WhiteMode"),
    ]


async def test_persisted_recovery_refuses_unknown_scheme():
    """Do not overwrite a camera state that may have been changed externally."""
    c = _Coordinator(channel=2, profile_mode="0")
    c.client.scheme = "SomethingElse"
    c.client.light_brightness = dahua_utils.hass_brightness_to_dahua_brightness(200)

    light = _light(DahuaIlluminator, c, "Illuminator")
    snapshot = {
        "active": True,
        "scheme_channel": 2,
        "scheme_profile": "0",
        "previous_scheme": "AIMode",
        "channel": 2,
        "profile_mode": "0",
        "index": 1,
        "field": "NearLight",
        "old_mode": "Manual",
        "old_brightness": 88,
        "ha_brightness": 200,
    }
    light._restore_store.data = dict(snapshot)

    recovered = await light._recover_persisted_override()

    assert recovered is False
    assert light._restore_store.data == snapshot
    assert c.client.operations == []


async def test_persisted_recovery_clears_stale_snapshot():
    """Clear a snapshot only when scheme and WhiteLight are both restored."""
    c = _Coordinator(channel=2, profile_mode="0")
    c.client.scheme = "AIMode"
    c.client.light_mode = "Manual"
    c.client.light_brightness = 88
    c.client.light_field = "NearLight"

    light = _light(DahuaIlluminator, c, "Illuminator")
    light._restore_store.data = {
        "active": True,
        "scheme_channel": 2,
        "scheme_profile": "0",
        "previous_scheme": "AIMode",
        "channel": 2,
        "profile_mode": "0",
        "index": 1,
        "field": "NearLight",
        "old_mode": "Manual",
        "old_brightness": 88,
        "ha_brightness": 160,
    }

    recovered = await light._recover_persisted_override()

    assert recovered is True
    assert light._restore_store.data is None
    assert c.client.operations == []


async def test_restore_off_mode_preserves_saved_brightness():
    """Mode=Off must still restore the saved brightness value exactly."""
    c = _Coordinator(channel=2, profile_mode="0")
    light = _light(DahuaIlluminator, c, "Illuminator")

    await light._restore_camera_lighting(
        (2, "0", "WhiteMode"),
        (2, "0", 1, "NearLight", "Off", 30),
    )

    assert c.client.operations == [
        ("v2_raw", 2, "0", 1, "Off", "NearLight", None),
        ("v2_raw", 2, "0", 1, "Off", "NearLight", 30),
        ("scheme", 2, "0", "WhiteMode"),
    ]

    assert c.client.light_mode == "Off"
    assert c.client.light_brightness == 30


async def test_persisted_recovery_repairs_partially_restored_state():
    """Matching scheme alone must not discard a still-needed restore."""
    c = _Coordinator(channel=2, profile_mode="0")

    # The scheme has already returned to the original AIMode, but the
    # WhiteLight brightness is still the HA override rather than the saved 30.
    c.client.scheme = "AIMode"
    c.client.light_mode = "Manual"
    c.client.light_brightness = 100
    c.client.light_field = "NearLight"

    light = _light(DahuaIlluminator, c, "Illuminator")
    light._restore_store.data = {
        "active": True,
        "scheme_channel": 2,
        "scheme_profile": "0",
        "previous_scheme": "AIMode",
        "channel": 2,
        "profile_mode": "0",
        "index": 1,
        "field": "NearLight",
        "old_mode": "Manual",
        "old_brightness": 30,
        "ha_brightness": 255,
    }

    recovered = await light._recover_persisted_override()

    assert recovered is True
    assert light._restore_store.data is None

    assert c.client.operations == [
        ("v2_raw", 2, "0", 1, "Off", "NearLight", None),
        ("scheme", 2, "0", "InfraredMode"),
        ("v2_raw", 2, "0", 1, "Manual", "NearLight", 30),
        ("scheme", 2, "0", "AIMode"),
    ]

    assert c.client.scheme == "AIMode"
    assert c.client.light_mode == "Manual"
    assert c.client.light_brightness == 30


async def test_reboot_recovery_failure_leaves_generation_for_retry(monkeypatch):
    """Failed reboot recovery must not consume the reboot generation."""
    c = _Coordinator(channel=2, profile_mode="0")
    light = _light(DahuaIlluminator, c, "Illuminator")

    light._manual_on = True
    light._seen_reboot_generation = 0

    results = iter((False, True))

    async def recover():
        return next(results)

    monkeypatch.setattr(
        light,
        "_recover_persisted_override",
        recover,
    )
    monkeypatch.setattr(
        DahuaIlluminator,
        "async_write_ha_state",
        lambda self: None,
    )

    # First recovery attempt fails. Generation 1 must remain unconsumed.
    await light._async_handle_camera_reboot(1)

    assert light._seen_reboot_generation == 0
    assert light._manual_on is True

    # A later retry succeeds and only then consumes generation 1.
    await light._async_handle_camera_reboot(1)

    assert light._seen_reboot_generation == 1
    assert light._manual_on is False
    assert light._scheme_restore is None
    assert light._light_restore is None
