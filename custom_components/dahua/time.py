"""Time entities for Dahua Profile Schedule."""

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from . import DahuaDataUpdateCoordinator, entry_coordinators
from .const import DOMAIN
from .entity import DahuaBaseEntity
from .late import async_add_and_keep_watching

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass,
    entry,
    async_add_devices,
):
    """Set up Profile Schedule times for compatible channels."""

    def build(coordinator):
        if not coordinator.supports_profile_schedule():
            return []

        return [
            DahuaProfileDayStartTime(coordinator, entry),
            DahuaProfileNightStartTime(coordinator, entry),
        ]

    await async_add_and_keep_watching(
        hass,
        entry,
        build,
        async_add_devices,
        entry_coordinators,
    )


class DahuaProfileScheduleTime(DahuaBaseEntity, TimeEntity):
    """Base class for one camera-owned Profile Schedule endpoint."""

    _attr_entity_category = EntityCategory.CONFIG
    _endpoint: str

    def __init__(
        self,
        coordinator: DahuaDataUpdateCoordinator,
        config_entry,
    ):
        super().__init__(coordinator, config_entry)

        self._attr_unique_id = (
            f"{coordinator.get_serial_number()}" f"_profile_{self._endpoint}"
        )

    @property
    def native_value(self) -> time | None:
        """Return the camera-reported endpoint."""
        schedule_times = self._coordinator.get_profile_schedule_times()

        return (
            None if schedule_times is None else getattr(schedule_times, self._endpoint)
        )

    async def async_set_value(self, value: time) -> None:
        """Change one endpoint while preserving its camera-reported peer."""
        if self._coordinator.get_profile_management_mode() != "schedule":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=("profile_schedule_requires_schedule_mode"),
            )

        schedule_times = self._coordinator.get_profile_schedule_times()

        if schedule_times is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="profile_schedule_unavailable",
            )

        day_start = value if self._endpoint == "day_start" else schedule_times.day_start

        night_start = (
            value if self._endpoint == "night_start" else schedule_times.night_start
        )

        try:
            await self._coordinator.async_set_profile_schedule_times(
                day_start,
                night_start,
            )
        except (
            ConnectionError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="profile_schedule_write_failed",
                translation_placeholders={
                    "reason": str(err),
                },
            ) from err

        await self._coordinator.async_refresh()


class DahuaProfileDayStartTime(DahuaProfileScheduleTime):
    """Control the start of the camera's Day profile period."""

    _attr_translation_key = "profile_day_start"
    _endpoint = "day_start"


class DahuaProfileNightStartTime(DahuaProfileScheduleTime):
    """Control the start of the camera's Night profile period."""

    _attr_translation_key = "profile_night_start"
    _endpoint = "night_start"
