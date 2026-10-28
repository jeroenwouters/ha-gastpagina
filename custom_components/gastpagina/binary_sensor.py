"""binary_sensor.gastpagina_boeking_actief: on while a code is valid."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import GastEntity
from .runtime import GastConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the booking sensor."""
    async_add_entities([BookingActive(entry, "boeking_actief", Platform.BINARY_SENSOR)])


class BookingActive(GastEntity, BinarySensorEntity):
    """Whether some booking's code works right now."""

    @property
    def is_on(self) -> bool:
        """At least one valid booking."""
        return bool(self.runtime.bookings.valid())
