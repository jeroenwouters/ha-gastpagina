"""Active sessions and wrong codes in the past hour."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
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
    """Add the counters."""
    async_add_entities(
        [
            ActiveSessions(entry, "actieve_sessies", Platform.SENSOR),
            FailedLogins(entry, "foute_pogingen", Platform.SENSOR),
            OwnerSessions(entry, "eigenaarssessies", Platform.SENSOR),
        ]
    )


class ActiveSessions(GastEntity, SensorEntity):
    """Number of guest sessions (owners are counted apart)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        """Current count."""
        return self.runtime.sessions.count


class FailedLogins(GastEntity, SensorEntity):
    """Wrong codes in the past hour, all addresses together."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        """Rolling one-hour count."""
        return self.runtime.limiter.failures_last_hour()


class OwnerSessions(GastEntity, SensorEntity):
    """Number of owner sessions."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        """Current count."""
        return self.runtime.sessions.owner_count
