"""
Illuminator for for Dahua cameras that have white light illuminators.

See https://developers.home-assistant.io/docs/core/entity/light
"""

import asyncio
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    LightEntity, LightEntityFeature, ColorMode,
)

from . import DahuaDataUpdateCoordinator, dahua_utils, scheme_blocking_white_light
from .const import DOMAIN, SECURITY_LIGHT_ICON, INFRARED_ICON
from .entity import DahuaBaseEntity
from .client import SECURITY_LIGHT_TYPE


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Setup light platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if coordinator.supports_infrared_light():
        entities.append(DahuaInfraredLight(coordinator, entry, "Infrared"))

    if coordinator.supports_illuminator():
        entities.append(DahuaIlluminator(coordinator, entry, "Illuminator"))

    if coordinator.is_flood_light():
        entities.append(FloodLight(coordinator, entry, "Flood Light"))

    has_security_light = (
        coordinator.supports_nvr_active_deterrence()
        if coordinator.is_nvr_channel()
        else coordinator.supports_security_light()
    )
    if has_security_light and not coordinator.is_amcrest_doorbell():
        #  The Amcrest doorbell works a little different and is added in select.py
        security_light_name = (
            "Warning Light" if coordinator.is_nvr_channel() else "Security Light"
        )
        entities.append(DahuaSecurityLight(coordinator, entry, security_light_name))

    if coordinator.is_amcrest_doorbell():
        entities.append(AmcrestRingLight(coordinator, entry, "Ring Light"))

    async_add_entities(entities)


class DahuaInfraredLight(DahuaBaseEntity, LightEntity):
    """Representation of a Dahua infrared light (for cameras that have them)"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_infrared"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_infrared_light_on()

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255 inclusive"""
        return self._coordinator.get_infrared_brightness()

    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}

    @property
    def supported_features(self):
        """Flag supported features."""
        return LightEntityFeature.EFFECT

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on with the current brightness"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_lighting_v1(channel, True, dahua_brightness)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_lighting_v1(channel, False, dahua_brightness)
        await self.coordinator.async_refresh()

    @property
    def icon(self):
        """Return the icon of this switch."""
        return INFRARED_ICON


class DahuaIlluminator(DahuaBaseEntity, LightEntity):
    """Physical Dahua white-light illuminator."""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator
        self._scheme_unreadable = False
        self._entry = entry
        self._restore_store = None

        # (channel, profile, previous LightingScheme)
        self._scheme_restore = None

        # (channel, profile, index, field, old_mode, old_brightness)
        self._light_restore = None

        # Lighting_V2.Mode alone cannot tell us whether the physical
        # emitter is active while Smart Dual Light/AIMode is in use.
        self._manual_on = False

        # Last host reboot generation this entity has already seen.
        self._seen_reboot_generation = 0
        self._reboot_recovery_task = None

        # HA manual illuminator brightness is independent from the
        # camera's Smart Dual Light / Self-adaptive brightness.
        #
        # Default HA manual illuminator brightness. The most recently
        # requested brightness is preserved while the entity remains loaded
        # and may also be recovered from persistent state.
        self._last_brightness = 255

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_illuminator"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._manual_on

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255 inclusive"""
        return self._last_brightness

    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_added_to_hass(self):
        """Restore a CGI lighting override left behind by an HA restart."""
        await super().async_added_to_hass()

        store_key = (
            f"{DOMAIN}.illuminator_restore."
            f"{self._entry.entry_id}.{self.unique_id}"
        )

        self._restore_store = Store(
            self.hass,
            1,
            store_key,
        )

        try:
            await self._recover_persisted_override()
        except Exception:
            # Never destroy the saved snapshot when recovery fails.
            # It may still be required on the next restart.
            _LOGGER.warning(
                "Dahua Illuminator: startup restore recovery failed",
                exc_info=True,
            )

        # Whatever generation exists at entity startup is the baseline.
        # Only a later coordinator poll with a newer generation represents
        # a reboot that happened while this entity was alive.
        self._seen_reboot_generation = (
            self._coordinator.get_camera_reboot_generation()
        )

    @callback
    def _handle_coordinator_update(self):
        """React to camera reboot and external illuminator changes."""

        generation = self._coordinator.get_camera_reboot_generation()
        reboot_pending = False

        if generation > self._seen_reboot_generation:
            previous_generation = self._seen_reboot_generation

            _LOGGER.debug(
                "Dahua Illuminator: coordinator reported host reboot "
                "generation %s -> %s",
                previous_generation,
                generation,
            )

            if not self._manual_on:
                # No HA override is active, so there is nothing to recover.
                self._seen_reboot_generation = generation
            else:
                # A reboot can temporarily make the camera state differ from
                # our expected override. Do not mistake that for an external
                # configuration change; reboot recovery owns this update.
                reboot_pending = True

                if (
                    self._reboot_recovery_task is None
                    or self._reboot_recovery_task.done()
                ):
                    self._reboot_recovery_task = (
                        self.hass.async_create_background_task(
                            self._async_handle_camera_reboot(generation),
                            f"dahua_illuminator_reboot_recovery_{self.unique_id}",
                        )
                    )

        if self._manual_on and not reboot_pending:
            matches = self._current_override_matches_coordinator_data()

            if matches is False:
                task = getattr(self, "_external_change_task", None)

                if task is None or task.done():
                    self._external_change_task = (
                        self.hass.async_create_background_task(
                            self._async_release_externally_changed_override(),
                            f"dahua_illuminator_external_change_{self.unique_id}",
                        )
                    )

        super()._handle_coordinator_update()

    async def _async_handle_camera_reboot(self, generation: int):
        """Restore an active persistent CGI override after camera reboot."""

        try:
            _LOGGER.info(
                "Dahua Illuminator: camera reboot detected by coordinator; "
                "recovering persistent override"
            )

            recovered = await self._recover_persisted_override()

            if not recovered:
                _LOGGER.warning(
                    "Dahua Illuminator: camera reboot detected, but "
                    "persistent override could not be safely recovered; "
                    "preserving HA restore state"
                )
                return

            # Consume this reboot generation only after recovery succeeded.
            # If recovery fails, the next coordinator update will retry it.
            self._seen_reboot_generation = max(
                self._seen_reboot_generation,
                generation,
            )

            # Reboot ends the HA manual override only after recovery has
            # completed or the saved snapshot was confirmed stale.
            self._reset_override_state()

            self.async_write_ha_state()

        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning(
                "Dahua Illuminator: failed to recover override "
                "after camera reboot",
                exc_info=True,
            )
        finally:
            self._reboot_recovery_task = None

    async def _persist_current_restore_snapshot(self):
        """Persist original camera state before applying a CGI override."""

        if self._restore_store is None:
            raise RuntimeError(
                "Illuminator restore Store is not initialized"
            )

        if (
            self._scheme_restore is None
            or self._light_restore is None
        ):
            raise RuntimeError(
                "Cannot apply illuminator override without complete restore state"
            )

        (
            scheme_channel,
            scheme_profile,
            previous_scheme,
        ) = self._scheme_restore

        (
            channel,
            profile_mode,
            index,
            field,
            old_mode,
            old_brightness,
        ) = self._light_restore

        data = {
            "active": True,
            "phase": "override",
            "scheme_channel": scheme_channel,
            "scheme_profile": scheme_profile,
            "previous_scheme": previous_scheme,
            "channel": channel,
            "profile_mode": profile_mode,
            "index": index,
            "field": field,
            "old_mode": old_mode,
            "old_brightness": old_brightness,
            "ha_brightness": self._last_brightness,
            "override_brightness": (
                dahua_utils.hass_brightness_to_dahua_brightness(
                    self._last_brightness
                )
            ),
        }

        # IMPORTANT:
        # Save BEFORE writing WhiteMode to the camera.
        await self._restore_store.async_save(data)

        _LOGGER.debug(
            "Dahua Illuminator: persisted restore snapshot "
            "(scheme=%s mode=%s brightness=%s)",
            previous_scheme,
            old_mode,
            old_brightness,
        )

    async def _clear_persisted_restore_snapshot(self):
        """Remove the saved override snapshot after a successful restore."""

        if self._restore_store is not None:
            await self._restore_store.async_remove()

        self._reset_override_state()

    def _reset_override_state(self):
        """Release in-memory ownership only after persistent cleanup succeeds."""
        self._manual_on = False
        self._scheme_restore = None
        self._light_restore = None

    async def _mark_persisted_restore_phase(self, data=None):
        """Mark that HA has started restoring its saved camera state."""

        if self._restore_store is None:
            raise RuntimeError(
                "Illuminator restore Store is not initialized"
            )

        if data is None:
            data = await self._restore_store.async_load()

        if not isinstance(data, dict) or not data.get("active"):
            raise RuntimeError(
                "Illuminator restore snapshot is missing"
            )

        data["phase"] = "restoring"
        await self._restore_store.async_save(data)

    def _override_matches(self, mode, brightness):
        """Compare a known WhiteLight mode with HA's last manual override."""
        if mode != "Manual":
            return False

        # Missing or unusable brightness is not evidence of external takeover.
        try:
            expected = dahua_utils.hass_brightness_to_dahua_brightness(
                self._last_brightness
            )
            return int(brightness) == expected
        except (TypeError, ValueError):
            return True

    def _current_override_matches_coordinator_data(self):
        """Return None when the coordinator has no usable WhiteLight mode."""
        if self._light_restore is None:
            return None

        channel, profile, index, field, *_ = self._light_restore
        base = f"table.Lighting_V2[{channel}][{profile}][{index}]"
        mode = self._coordinator.data.get(f"{base}.Mode")
        if mode is None:
            return None

        return self._override_matches(
            mode, self._coordinator.data.get(f"{base}.{field}[0].Light")
        )

    async def _live_override_matches_camera(self):
        """Check fresh camera state before OFF; never use a cached snapshot."""
        if self._light_restore is None:
            return None

        channel, profile, index, *_ = self._light_restore
        mode, _field, brightness = (
            await self._coordinator.client.async_get_lighting_v2_live_state(
                channel, profile, index
            )
        )
        return self._override_matches(mode, brightness)

    async def _async_release_externally_changed_override(self):
        """Release HA ownership after another control path changes the light."""

        try:
            if not self._manual_on:
                return

            # Re-check after the task gets CPU time. The camera may already
            # have changed again.
            if self._current_override_matches_coordinator_data() is not False:
                return

            _LOGGER.info(
                "Dahua Illuminator: camera WhiteLight configuration changed "
                "outside the HA light entity; releasing the saved override "
                "without restoring the old camera state"
            )

            # Respect the new external configuration exactly as it is.
            # In particular, do not restore the state saved before HA ON.
            await self._clear_persisted_restore_snapshot()

            self.async_write_ha_state()

        except asyncio.CancelledError:
            raise
        except Exception:
            # If Store cleanup fails, retain ownership state so a future
            # coordinator update can retry rather than losing recovery data.
            _LOGGER.warning(
                "Dahua Illuminator: failed to release externally changed "
                "override",
                exc_info=True,
            )
        finally:
            self._external_change_task = None

    async def _recover_persisted_override(self) -> bool:
        """Recover an unfinished CGI override.

        Return True when recovery is complete, when the original state was
        already restored, or when an external change has superseded HA's
        override. Return False when recovery cannot be performed safely.
        """

        if self._restore_store is None:
            return False

        data = await self._restore_store.async_load()

        if not isinstance(data, dict) or not data.get("active"):
            return False

        try:
            phase = str(data.get("phase", "override"))

            if phase not in ("override", "restoring"):
                raise ValueError(f"unknown restore phase: {phase}")

            scheme_channel = int(data["scheme_channel"])
            scheme_profile = str(data["scheme_profile"])
            previous_scheme = str(data["previous_scheme"])

            channel = int(data["channel"])
            profile_mode = str(data["profile_mode"])
            index = int(data["index"])
            field = str(data["field"])
            old_mode = data.get("old_mode")
            old_brightness = data.get("old_brightness")

            saved_ha_brightness = data.get("ha_brightness")
            if isinstance(saved_ha_brightness, int):
                self._last_brightness = saved_ha_brightness

            override_brightness = data.get("override_brightness")

            if override_brightness is None and isinstance(
                saved_ha_brightness, int
            ):
                override_brightness = (
                    dahua_utils.hass_brightness_to_dahua_brightness(
                        saved_ha_brightness
                    )
                )

            if override_brightness is not None:
                override_brightness = int(override_brightness)

        except (KeyError, TypeError, ValueError):
            _LOGGER.warning(
                "Dahua Illuminator: invalid persisted restore snapshot; "
                "leaving it untouched"
            )
            return False

        current_scheme = (
            await self._coordinator.client
            .async_get_lighting_scheme_mode(
                scheme_channel,
                scheme_profile,
            )
        )

        (
            current_mode,
            _current_field,
            current_brightness,
        ) = (
            await self._coordinator.client
            .async_get_lighting_v2_live_state(
                channel,
                profile_mode,
                index,
            )
        )

        light_restored = (
            current_mode == old_mode
            and (
                old_brightness is None
                or current_brightness == old_brightness
            )
        )

        # Both parts are already back to the saved original state.
        if current_scheme == previous_scheme and light_restored:
            await self._clear_persisted_restore_snapshot()

            _LOGGER.info(
                "Dahua Illuminator: persisted override already restored "
                "(LightingScheme=%s)",
                previous_scheme,
            )
            return True

        # While phase=override, HA only owns the camera state if the
        # WhiteLight row still looks like the Manual override HA wrote.
        #
        # If a service, Web UI, or another client changed it to Auto/Off or
        # changed Manual brightness, that newer configuration wins. Do not
        # restore the older snapshot over it.
        if phase == "override":
            brightness_still_owned = (
                override_brightness is None
                or current_brightness is None
                or current_brightness == override_brightness
            )

            override_still_owned = (
                current_mode == "Manual"
                and brightness_still_owned
            )

            if not override_still_owned:
                _LOGGER.info(
                    "Dahua Illuminator: persisted override was superseded "
                    "by an external camera change "
                    "(mode=%s brightness=%s); preserving the current camera "
                    "configuration",
                    current_mode,
                    current_brightness,
                )

                await self._clear_persisted_restore_snapshot()

                return True

        # WhiteMode is the state HA deliberately writes. Some tested cameras
        # may later report the saved scheme again while the Manual WhiteLight
        # override remains active, so that state is also safe to restore.
        # During restore, InfraredMode is also our own intermediate write.
        # Any other scheme may represent an unrelated external change.
        if (
            current_scheme != "WhiteMode"
            and current_scheme != previous_scheme
            and not (phase == "restoring" and current_scheme == "InfraredMode")
        ):
            _LOGGER.warning(
                "Dahua Illuminator: saved override exists, but current "
                "LightingScheme=%s is neither WhiteMode nor the saved "
                "LightingScheme=%s; not changing camera configuration "
                "automatically",
                current_scheme,
                previous_scheme,
            )
            return False

        _LOGGER.info(
            "Dahua Illuminator: recovering persisted CGI override; "
            "restoring LightingScheme=%s",
            previous_scheme,
        )

        # Once restore writes begin, a crash/restart must continue recovery
        # rather than mistaking our own intermediate Off/InfraredMode state
        # for an external camera change.
        await self._mark_persisted_restore_phase(data)

        await self._restore_camera_lighting(
            (
                scheme_channel,
                scheme_profile,
                previous_scheme,
            ),
            (
                channel,
                profile_mode,
                index,
                field,
                old_mode,
                old_brightness,
            ),
        )

        # Only clear persistent state after every camera write succeeded.
        await self._clear_persisted_restore_snapshot()

        _LOGGER.info(
            "Dahua Illuminator: persisted override recovery complete; "
            "original camera lighting configuration restored"
        )

        return True

    async def _restore_camera_lighting(
        self,
        scheme_restore,
        light_restore,
    ):
        """Restore the camera lighting state saved before the HA override."""

        if light_restore is not None:
            (
                channel,
                profile_mode,
                index,
                field,
                old_mode,
                old_brightness,
            ) = light_restore
        else:
            channel = self._coordinator.get_channel()
            profile_mode = self._coordinator.get_profile_mode()
            index = self._coordinator.get_illuminator_index()
            field = self._coordinator.get_illuminator_bank()
            old_mode = None
            old_brightness = None

        # Explicitly release the physical white emitter first.
        await self._coordinator.client.async_set_lighting_v2_raw(
            channel,
            profile_mode,
            index,
            "Off",
            field,
        )

        _LOGGER.debug(
            "Dahua Illuminator: forced WhiteLight Off "
            "(channel=%s profile=%s)",
            channel,
            profile_mode,
        )

        if scheme_restore is None:
            return

        (
            scheme_channel,
            scheme_profile,
            previous_scheme,
        ) = scheme_restore

        # Restore Manual/Auto WhiteLight state behind InfraredMode so the
        # physical white emitter does not immediately turn back on.
        if old_mode:
            if old_mode != "Off":
                await self._coordinator.client.async_set_lighting_scheme(
                    scheme_channel,
                    scheme_profile,
                    "InfraredMode",
                )

                _LOGGER.debug(
                    "Dahua Illuminator: temporary InfraredMode "
                    "for safe WhiteLight restore "
                    "(channel=%s profile=%s)",
                    scheme_channel,
                    scheme_profile,
                )

            # Restore the complete original row even when Mode was Off.
            # The brightness value is still part of the saved camera config.
            await self._coordinator.client.async_set_lighting_v2_raw(
                channel,
                profile_mode,
                index,
                old_mode,
                field,
                old_brightness,
            )

            _LOGGER.debug(
                "Dahua Illuminator: restored WhiteLight "
                "Mode=%s brightness=%s "
                "(channel=%s profile=%s field=%s)",
                old_mode,
                old_brightness,
                channel,
                profile_mode,
                field,
            )

        await self._coordinator.client.async_set_lighting_scheme(
            scheme_channel,
            scheme_profile,
            previous_scheme,
        )

        _LOGGER.debug(
            "Dahua Illuminator: restored LightingScheme %s "
            "(channel=%s profile=%s)",
            previous_scheme,
            scheme_channel,
            scheme_profile,
        )

    async def async_turn_on(self, **kwargs):
        """Force the physical white illuminator on."""

        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)

        if hass_brightness is None:
            hass_brightness = self._last_brightness

        if not hass_brightness or hass_brightness < 1:
            hass_brightness = 255

        self._last_brightness = hass_brightness

        dahua_brightness = (
            dahua_utils.hass_brightness_to_dahua_brightness(
                hass_brightness
            )
        )

        channel = self._coordinator.get_channel()
        profile_mode = self._coordinator.get_profile_mode()
        index = self._coordinator.get_illuminator_index()

        # IPC-Color4M-TZ uses the dedicated full-table LightingScheme API
        # introduced in 0.9.99. Its state is not a Lighting_V2 override.
        if self._coordinator.uses_lighting_scheme_illuminator():
            await self._coordinator.client.async_set_lighting_scheme_illuminator(
                channel, True, dahua_brightness, profile_mode, index
            )
            await self._coordinator.async_refresh()
            self.async_write_ha_state()
            return

        field = self._coordinator.get_illuminator_bank()

        # Capture the original WhiteLight row only on the first ON.
        #
        # IMPORTANT:
        # Read this directly from the camera. coordinator.data can contain
        # an older Lighting_V2 snapshot and must not be used for restore
        # state.
        if self._light_restore is None:
            (
                old_mode,
                live_field,
                old_brightness,
            ) = (
                await self._coordinator.client
                .async_get_lighting_v2_live_state(
                    channel,
                    profile_mode,
                    index,
                )
            )

            # Use the field actually reported by the live WhiteLight row.
            field = live_field

            self._light_restore = (
                channel,
                profile_mode,
                index,
                field,
                old_mode,
                old_brightness,
            )

            _LOGGER.debug(
                "Dahua Illuminator: LIVE saved WhiteLight "
                "Mode=%s brightness=%s "
                "(channel=%s profile=%s field=%s)",
                old_mode,
                old_brightness,
                channel,
                profile_mode,
                field,
            )

        # A brightness update may arrive after the coordinator has switched
        # profiles (for example at sunset). Keep writing the exact live row
        # captured by the first HA command so OFF can restore that same row.
        if self._light_restore is not None:
            (
                channel,
                profile_mode,
                index,
                field,
                _old_mode,
                _old_brightness,
            ) = self._light_restore

        if self._scheme_restore is None:
            current_scheme = (
                await self._coordinator.client
                .async_get_lighting_scheme_mode(channel, profile_mode)
            )
            self._scheme_restore = (channel, profile_mode, current_scheme)
        else:
            scheme_channel, scheme_profile, _previous_scheme = (
                self._scheme_restore
            )
            current_scheme = (
                await self._coordinator.client
                .async_get_lighting_scheme_mode(scheme_channel, scheme_profile)
            )

        # Persist the original state BEFORE the first CGI override.
        #
        # CGI setConfig survives an HA restart, so the restore snapshot
        # must already be on disk before WhiteMode is written.
        await self._persist_current_restore_snapshot()

        # First set WhiteLight parameters.
        await self._coordinator.client.async_set_lighting_v2(
            channel,
            True,
            dahua_brightness,
            profile_mode,
            index,
            field,
        )

        # Then force WhiteMode. Some cameras later report AIMode again,
        # but the physical white-light override remains active.
        if current_scheme != "WhiteMode":
            await (
                self._coordinator.client
                .async_set_lighting_scheme(
                    channel,
                    profile_mode,
                    "WhiteMode",
                )
            )

            _LOGGER.debug(
                "Dahua Illuminator: %s -> WhiteMode "
                "(channel=%s profile=%s field=%s brightness=%s)",
                current_scheme,
                channel,
                profile_mode,
                field,
                dahua_brightness,
            )

        self._manual_on = True

        await self._coordinator.async_refresh()
        self.async_write_ha_state()

    async def _warn_if_the_scheme_blocks_it(self, channel, profile_mode):
        """Warn once when LightingScheme cannot be read or blocks white light."""
        if self._scheme_unreadable:
            return
        try:
            data = await self._coordinator.client.async_get_lighting_scheme()
        except Exception:  # pylint: disable=broad-except
            self._scheme_unreadable = True
            _LOGGER.debug(
                "LightingScheme is not readable on this device; the white light "
                "scheme check is switched off for it", exc_info=True)
            return
        blocking = scheme_blocking_white_light(data, channel, profile_mode)
        if blocking is not None:
            _LOGGER.warning(
                "The white light on %s was set, but the camera's lighting scheme is "
                "%s, so the light will not physically come on. Switch that camera to "
                "white light in its own web interface to use this entity.",
                self._coordinator.get_device_name(), blocking,
            )

    async def async_turn_off(self, **kwargs):
        """Turn off white-light override and restore original Smart Dual Light config."""

        if self._coordinator.uses_lighting_scheme_illuminator():
            channel = self._coordinator.get_channel()
            profile_mode = self._coordinator.get_profile_mode()
            index = self._coordinator.get_illuminator_index()
            hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
            dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(
                hass_brightness
            )
            await self._coordinator.client.async_set_lighting_scheme_illuminator(
                channel, False, dahua_brightness, profile_mode, index
            )
            await self._coordinator.async_refresh()
            self.async_write_ha_state()
            return

        # OFF must be idempotent.
        #
        # Once HA has already released the manual override, another
        # light.turn_off call must NOT write WhiteLight.Mode=Off again,
        # otherwise it destroys the camera's restored Self-adaptive
        # WhiteLight configuration.
        if (
            not self._manual_on
            and self._light_restore is None
            and self._scheme_restore is None
        ):
            _LOGGER.debug(
                "Dahua Illuminator: already off; ignoring duplicate OFF"
            )
            return

        # Do not rely only on coordinator.data here. Another HA service,
        # camera Web UI, or external client may have changed the WhiteLight
        # row after the last coordinator refresh.
        #
        # Re-read the camera immediately before restoring the saved baseline.
        # If our Manual override has already been superseded, respect the
        # newer camera configuration and only release HA ownership.
        if self._manual_on and self._light_restore is not None:
            live_matches = await self._live_override_matches_camera()

            if live_matches is False:
                _LOGGER.info(
                    "Dahua Illuminator: OFF found that the HA override "
                    "was already superseded externally; preserving the "
                    "current camera configuration"
                )

                await self._clear_persisted_restore_snapshot()

                await self._coordinator.async_refresh()
                self.async_write_ha_state()
                return

        await self._mark_persisted_restore_phase()

        await self._restore_camera_lighting(
            self._scheme_restore,
            self._light_restore,
        )

        # Camera restore completed successfully. It is now safe to
        # discard the persistent recovery snapshot.
        await self._clear_persisted_restore_snapshot()

        await self._coordinator.async_refresh()
        self.async_write_ha_state()



class AmcrestRingLight(DahuaBaseEntity, LightEntity):
    """Representation of a Amcrest ring light"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue).
        Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_ring_light"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_ring_light_on()

    async def async_turn_on(self, **kwargs):
        """Turn the light on"""
        await self._coordinator.client.async_set_light_global_enabled(True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        await self._coordinator.client.async_set_light_global_enabled(False)
        await self._coordinator.async_refresh()

    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}


class FloodLight(DahuaBaseEntity, LightEntity):
    """
        Representation of a Amcrest, Dahua, and Lorex Flood Light (for cameras that have them)
        Unlike the 'Dahua Illuminator', Amcrest Flood Lights do not play nicely
        with adjusting the 'White Light' brightness.
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_flood_light"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_flood_light_on()

    @property
    def supported_features(self):
        """Flag supported features."""
        return LightEntityFeature.EFFECT
    
    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on"""
        if self._coordinator._supports_floodlightmode:
            channel = self._coordinator.get_channel()
            self._coordinator._floodlight_mode = await self._coordinator.client.async_get_floodlightmode()
            await self._coordinator.client.async_set_floodlightmode(2)
            if self._coordinator.is_nvr_channel():
                await self._coordinator.client.async_set_nvr_coaxial_control_state(
                    self._coordinator.get_channel_number(), SECURITY_LIGHT_TYPE, True
                )
            else:
                await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, True)
            await self._coordinator.async_refresh()
        else:
            channel = self._coordinator.get_channel()
            profile_mode = self._coordinator.get_profile_mode()
            await self._coordinator.client.async_set_lighting_v2_for_flood_lights(channel, True, profile_mode)
            await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        if self._coordinator._supports_floodlightmode:
            channel = self._coordinator.get_channel()
            if self._coordinator.is_nvr_channel():
                await self._coordinator.client.async_set_nvr_coaxial_control_state(
                    self._coordinator.get_channel_number(), SECURITY_LIGHT_TYPE, False
                )
            else:
                await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, False)
            await self._coordinator.client.async_set_floodlightmode(self._coordinator._floodlight_mode)
            await self._coordinator.async_refresh()
        else:
            channel = self._coordinator.get_channel()
            profile_mode = self._coordinator.get_profile_mode()
            await self._coordinator.client.async_set_lighting_v2_for_flood_lights(channel, False, profile_mode)
            await self._coordinator.async_refresh()


class DahuaSecurityLight(DahuaBaseEntity, LightEntity):
    """
    Representation of a Dahua light (for cameras that have them). This is the red/blue flashing lights.
    The camera will only keep this light on for a few seconds before it automatically turns off.
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_security"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_security_light_on()

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on"""
        channel = self._coordinator.get_channel()
        if self._coordinator.is_nvr_channel():
            await self._coordinator.client.async_set_nvr_coaxial_control_state(
                self._coordinator.get_channel_number(), SECURITY_LIGHT_TYPE, True
            )
        else:
            await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        channel = self._coordinator.get_channel()
        if self._coordinator.is_nvr_channel():
            await self._coordinator.client.async_set_nvr_coaxial_control_state(
                self._coordinator.get_channel_number(), SECURITY_LIGHT_TYPE, False
            )
        else:
            await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, False)
        await self._coordinator.async_refresh()

    @property
    def icon(self):
        """Return the icon of this switch."""
        return SECURITY_LIGHT_ICON

    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}
