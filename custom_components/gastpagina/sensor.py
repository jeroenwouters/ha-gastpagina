"""Active sessions and wrong codes in the past hour."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import GastEntity
from .hub import label
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
            NextBooking(entry, "volgende_boeking", Platform.SENSOR),
            OpenRequests(entry, "openstaande_aanvragen", Platform.SENSOR),
            *(
                FeedLastSync(entry, f"feed_{name}_laatste_sync", Platform.SENSOR, name)
                for name in entry.runtime_data.hub.settings.feeds
            ),
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


class NextBooking(GastEntity, SensorEntity):
    """Arrival of the next (or current) confirmed stay; no guest names."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def _next(self) -> Any:
        now = dt_util.now()
        return next(
            (s for s in self.runtime.stays if s.is_booking and s.event.end_datetime_local > now),
            None,
        )

    @property
    def native_value(self) -> datetime | None:
        """Arrival time."""
        stay = self._next()
        return stay.event.start_datetime_local if stay else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Source, nights and departure."""
        stay = self._next()
        if stay is None:
            return {}
        return {
            "bron": stay.source or "eigenaar",
            "nachten": stay.count_nights,
            "vertrek": stay.event.end_datetime_local.isoformat(),
        }


class OpenRequests(GastEntity, SensorEntity):
    """Website requests waiting for the owner."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        """Count of options."""
        return self.runtime.website.open_requests(self.runtime.stays)


class FeedLastSync(GastEntity, SensorEntity):
    """When a channel feed was last read successfully."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: GastConfigEntry, key: str, platform: str, feed: str) -> None:
        """One per configured feed."""
        super().__init__(entry, key, platform)
        self._feed = feed
        self._attr_translation_key = "feed_laatste_sync"
        self._attr_translation_placeholders = {"feed": label(feed)}

    @property
    def native_value(self) -> datetime | None:
        """Last successful read."""
        return self.runtime.hub.status[self._feed].last_sync

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the last read changed."""
        status = self.runtime.hub.status[self._feed]
        return {**status.counts, "mislukt_op_rij": status.failures}
