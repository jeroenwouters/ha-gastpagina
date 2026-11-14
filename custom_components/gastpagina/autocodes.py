"""Writes a fresh code into bookings that have none.

Only ever adds a `code:` line (or, on request, replaces one); everything else in
the event stays as it was. The code is never logged: the calendar and the
`gastpagina_code_created` event are the only places it goes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.components import persistent_notification
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
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from . import codegen, metadata as md
from .bookings import booking_key, code_value, is_opt_out, normalize_code, with_code
from .const import CODE_WRITE_ATTEMPTS, DOMAIN, EVENT_CODE_CREATED

_LOGGER = logging.getLogger(__name__)

ISSUE_PREFIX = "auto_"


@dataclass(frozen=True, slots=True)
class CodeSettings:
    """Options for generated codes."""

    enabled: bool
    prefix: str
    notify: bool
    lookahead: timedelta
    min_length: int
    after: timedelta


def _writable(entity: CalendarEntity) -> bool:
    return bool((entity.supported_features or 0) & CalendarEntityFeature.UPDATE_EVENT)


def _ineligible(event: CalendarEvent) -> str | None:
    """Why an event cannot get a code, as a translation key."""
    if event.rrule or event.recurrence_id:
        return "recurring_booking"
    if not isinstance(event.start, datetime):
        return "all_day_booking"
    return None


class CodeFiller:
    """Fills in and replaces codes in the bookings calendar."""

    def __init__(
        self,
        hass: HomeAssistant,
        calendar_entity: str,
        settings: CodeSettings,
        owner_taken: Callable[[Iterable[str]], Awaitable[set[str]]] | None = None,
    ) -> None:
        """Remember the calendar and the options; `owner_taken` keeps codes off owner codes."""
        self.hass = hass
        self._owner_taken = owner_taken
        self.calendar_entity = calendar_entity
        self.settings = settings
        self._issues: set[str] = set()
        self._failures: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """Held while the calendar is read and written; shared with the hub and the website."""
        return self._lock

    def _entity(self) -> CalendarEntity | None:
        component = self.hass.data.get(DATA_COMPONENT)
        return component.get_entity(self.calendar_entity) if component else None

    async def _events(self, entity: CalendarEntity) -> list[CalendarEvent]:
        now = dt_util.now()
        return await entity.async_get_events(
            self.hass, now - self.settings.after, now + self.settings.lookahead
        )

    @staticmethod
    def _codes_in_use(events: list[CalendarEvent], skip: str | None = None) -> set[str]:
        taken: set[str] = set()
        for event in events:
            value = code_value(event.description)
            if value is not None and not is_opt_out(value) and event.uid != skip:
                taken.add(normalize_code(value))
        return taken

    async def async_codes_in_use(self) -> set[str]:
        """Normalized codes of current and upcoming bookings (for owner-code checks)."""
        entity = self._entity()
        if entity is None:
            return set()
        try:
            return self._codes_in_use(await self._events(entity))
        except Exception:  # calendar platforms raise all kinds of errors
            return set()

    async def async_new_code(self, taken: set[str]) -> str:
        """A fresh code, not in `taken` and not equal to any owner code."""
        while True:
            code = codegen.generate(
                taken=taken, min_length=self.settings.min_length, prefix=self.settings.prefix
            )
            normalized = normalize_code(code)
            if self._owner_taken is None or not await self._owner_taken([normalized]):
                return code
            taken = taken | {normalized}

    async def async_fill(self) -> int:
        """Give every upcoming booking without a code line a code; returns how many."""
        if not self.settings.enabled:
            self._sync_issues({})
            return 0
        async with self._lock:
            entity = self._entity()
            if entity is None:
                return 0  # the booking index already reports a missing calendar
            if not _writable(entity):
                self._sync_issues(
                    {"calendar_read_only": ("calendar_read_only", {"entity": self.calendar_entity})}
                )
                return 0
            try:
                events = await self._events(entity)
            except Exception as err:  # calendar platforms raise all kinds of errors
                _LOGGER.warning("Calendar %s could not be read: %s", self.calendar_entity, err)
                return 0

            now = dt_util.now()
            taken = self._codes_in_use(events)
            issues: dict[str, tuple[str, dict[str, str]]] = {}
            created = 0
            for event in events:
                if code_value(event.description) is not None or event.uid is None:
                    continue
                if md.is_option(md.read(event.description)):
                    continue  # a website request gets its code once the owner confirms it
                if event.end_datetime_local <= now - self.settings.after:
                    continue
                key = booking_key(event)
                if reason := _ineligible(event):
                    issues[f"{ISSUE_PREFIX}{reason}_{key}"] = (reason, {"title": event.summary})
                    continue
                code = await self.async_new_code(taken)
                if not await self._async_write(entity, event, code, key):
                    if self._failures[key] >= CODE_WRITE_ATTEMPTS:
                        issues[f"{ISSUE_PREFIX}write_{key}"] = (
                            "code_write_failed",
                            {"title": event.summary},
                        )
                    continue
                taken.add(normalize_code(code))
                created += 1
                self._announce(event, code, key)
            self._sync_issues(issues)
            return created

    async def async_replace(self, query: str) -> tuple[str, str]:
        """A new code for one booking (title or uid); returns its key and the code."""
        async with self._lock:
            entity = self._entity()
            if entity is None or not _writable(entity):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="calendar_read_only"
                )
            events = await self._events(entity)
            wanted = query.strip().casefold()
            matches = {
                e.uid: e
                for e in events
                if e.uid is not None
                and (e.summary.strip().casefold() == wanted or e.uid.casefold() == wanted)
            }
            if not matches:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="booking_not_found",
                    translation_placeholders={"booking": query},
                )
            if len(matches) > 1:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="booking_ambiguous",
                    translation_placeholders={"booking": query},
                )
            (event,) = matches.values()
            if _ineligible(event):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="booking_not_eligible",
                    translation_placeholders={"booking": event.summary},
                )
            key = booking_key(event)
            taken = self._codes_in_use(events)  # includes the old code, so the new one differs
            code = await self.async_new_code(taken)
            if not await self._async_write(entity, event, code, key):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="code_write_failed",
                    translation_placeholders={"booking": event.summary},
                )
            self._announce(event, code, key)
            return key, code

    async def _async_write(
        self, entity: CalendarEntity, event: CalendarEvent, code: str, key: str
    ) -> bool:
        data = {
            EVENT_SUMMARY: event.summary,
            EVENT_START: event.start,
            EVENT_END: event.end,
            EVENT_DESCRIPTION: with_code(event.description, code),
        }
        if event.location:
            data[EVENT_LOCATION] = event.location
        try:
            await entity.async_update_event(event.uid or "", data)
        except Exception as err:  # never log the message: it might echo the description
            failures = self._failures[key] = self._failures.get(key, 0) + 1
            _LOGGER.warning(
                "Could not write a code to booking %s (attempt %d): %s",
                key,
                failures,
                type(err).__name__,
            )
            return False
        self._failures.pop(key, None)
        return True

    def _announce(self, event: CalendarEvent, code: str, key: str) -> None:
        start = event.start_datetime_local
        self.hass.bus.async_fire(
            EVENT_CODE_CREATED,
            {
                "booking": event.summary,
                "start": start.isoformat(),
                "end": event.end_datetime_local.isoformat(),
                "code": code,
            },
        )
        if self.settings.notify:
            persistent_notification.async_create(
                self.hass,
                f"Code aangemaakt voor {event.summary} ({start:%d-%m-%Y}). "
                "De code staat in de afspraak.",
                title="Gastpagina",
                notification_id=f"{DOMAIN}_code_{key}",
            )

    def _sync_issues(self, issues: dict[str, tuple[str, dict[str, str]]]) -> None:
        for issue_id in self._issues - issues.keys():
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for issue_id, (translation_key, placeholders) in issues.items():
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
        self._issues = set(issues)

    def clear_issues(self) -> None:
        """Remove every issue this filler raised (on unload)."""
        self._sync_issues({})
