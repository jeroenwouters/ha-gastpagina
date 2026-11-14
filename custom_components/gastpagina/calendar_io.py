"""Reading and writing the bookings calendar, shared by the hub, the website and the codes."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.components.calendar.const import (
    DATA_COMPONENT,
    EVENT_DESCRIPTION,
    EVENT_END,
    EVENT_LOCATION,
    EVENT_START,
    EVENT_SUMMARY,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_NEEDED = (
    CalendarEntityFeature.CREATE_EVENT
    | CalendarEntityFeature.UPDATE_EVENT
    | CalendarEntityFeature.DELETE_EVENT
)


class CalendarUnavailable(HomeAssistantError):
    """The calendar is missing, unreadable or cannot be written."""


class CalendarIO:
    """One calendar entity; `lock` serializes every read-modify-write on it."""

    def __init__(self, hass: HomeAssistant, entity_id: str, lock: asyncio.Lock) -> None:
        """Remember the entity; the lock is shared with the code filler."""
        self.hass = hass
        self.entity_id = entity_id
        self.lock = lock

    def entity(self) -> CalendarEntity | None:
        """The calendar entity, if it is loaded."""
        component = self.hass.data.get(DATA_COMPONENT)
        return component.get_entity(self.entity_id) if component else None

    def writable(self) -> bool:
        """Whether events can be created, updated and deleted (Local Calendar can)."""
        entity = self.entity()
        features = (entity.supported_features or 0) if entity is not None else 0
        return entity is not None and features & _NEEDED == _NEEDED

    def _writable_entity(self) -> CalendarEntity:
        entity = self.entity()
        if entity is None or not self.writable():
            raise CalendarUnavailable(f"calendar {self.entity_id} cannot be written")
        return entity

    async def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Events overlapping the range; raises CalendarUnavailable."""
        entity = self.entity()
        if entity is None:
            raise CalendarUnavailable(f"calendar {self.entity_id} not found")
        try:
            return await entity.async_get_events(self.hass, start, end)
        except Exception as err:  # calendar platforms raise all kinds of errors
            raise CalendarUnavailable(type(err).__name__) from err

    async def create(
        self, summary: str, start: datetime | date, end: datetime | date, description: str
    ) -> None:
        """Add an event."""
        entity = self._writable_entity()
        try:
            await entity.async_create_event(
                **{
                    EVENT_SUMMARY: summary,
                    EVENT_START: start,
                    EVENT_END: end,
                    EVENT_DESCRIPTION: description,
                }
            )
        except Exception as err:  # never log the message: it might echo the description
            raise CalendarUnavailable(type(err).__name__) from err

    async def update(
        self,
        event: CalendarEvent,
        *,
        summary: str | None = None,
        start: datetime | date | None = None,
        end: datetime | date | None = None,
        description: str | None = None,
    ) -> None:
        """Change some fields; the others keep their value."""
        entity = self._writable_entity()
        data = {
            EVENT_SUMMARY: event.summary if summary is None else summary,
            EVENT_START: event.start if start is None else start,
            EVENT_END: event.end if end is None else end,
            EVENT_DESCRIPTION: event.description if description is None else description,
        }
        if event.location:
            data[EVENT_LOCATION] = event.location
        try:
            await entity.async_update_event(event.uid or "", data)
        except Exception as err:  # never log the message: it might echo the description
            raise CalendarUnavailable(type(err).__name__) from err

    async def delete(self, event: CalendarEvent) -> None:
        """Remove an event."""
        entity = self._writable_entity()
        try:
            await entity.async_delete_event(event.uid or "")
        except Exception as err:
            raise CalendarUnavailable(type(err).__name__) from err
