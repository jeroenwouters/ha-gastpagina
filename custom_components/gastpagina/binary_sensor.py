"""binary_sensor.gastpagina_boeking_actief: on while a code is valid."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import GastEntity
from .hub import label
from .runtime import GastConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the booking sensor."""
    async_add_entities(
        [
            BookingActive(entry, "boeking_actief", Platform.BINARY_SENSOR),
            *(
                FeedOk(entry, f"feed_{name}_ok", Platform.BINARY_SENSOR, name)
                for name in entry.runtime_data.hub.settings.feeds
            ),
        ]
    )


class BookingActive(GastEntity, BinarySensorEntity):
    """Whether some booking's code works right now."""

    @property
    def is_on(self) -> bool:
        """At least one valid booking."""
        return bool(self.runtime.bookings.valid())


class FeedOk(GastEntity, BinarySensorEntity):
    """On while a channel feed reads fine."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: GastConfigEntry, key: str, platform: str, feed: str) -> None:
        """One per configured feed."""
        super().__init__(entry, key, platform)
        self._feed = feed
        self._attr_translation_key = "feed_ok"
        self._attr_translation_placeholders = {"feed": label(feed)}

    @property
    def is_on(self) -> bool | None:
        """None until the first read."""
        return self.runtime.hub.status[self._feed].ok
