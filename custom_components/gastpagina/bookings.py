"""Bookings and their guest codes, read through HA's calendar API.

Codes are never stored by this integration and never logged: the calendar is
the only place they live.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import hmac
import logging
import re

from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    CALENDAR_LOOKAHEAD,
    CALENDAR_MARGIN,
    CALENDAR_RECHECK,
    CALENDAR_REFRESH,
    CALENDAR_STALE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_CODE_LINE = re.compile(r"^[ \t]*code[ \t]*:[ \t]*(\S.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_SEPARATORS = re.compile(r"[\s\-\u2010-\u2015\u2212]+")  # spaces, dashes, minus

ISSUE_PREFIX = "booking_"
ISSUE_CALENDAR = "calendar_missing"


def normalize_code(raw: str) -> str:
    """Compare codes case-insensitively, ignoring spaces and dashes."""
    return _SEPARATORS.sub("", raw).casefold()


def extract_code(description: str | None) -> str | None:
    """Return the normalized code from a `code: ...` line, if any."""
    if not description:
        return None
    match = _CODE_LINE.search(description)
    return normalize_code(match.group(1)) if match else None


def _booking_key(event: CalendarEvent) -> str:
    """Opaque, stable id for a booking (the calendar uid, hashed)."""
    source = f"{event.uid or ''}|{event.recurrence_id or ''}"
    if not event.uid:
        source += f"|{event.summary}|{event.start.isoformat()}"
    return hashlib.sha256(source.encode()).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class Booking:
    """A calendar event with a usable code."""

    key: str
    uid: str | None
    title: str = field(repr=False)
    code: str = field(repr=False)
    valid_from: datetime
    valid_until: datetime

    @property
    def fingerprint(self) -> str:
        """Changes when the code changes; lets sessions notice a new code."""
        return hashlib.sha256(f"{self.key}\0{self.code}".encode()).hexdigest()

    def is_valid(self, now: datetime) -> bool:
        """Whether the code works at this moment."""
        return self.valid_from <= now < self.valid_until


class BookingIndex:
    """Cached view of the bookings calendar."""

    def __init__(
        self,
        hass: HomeAssistant,
        calendar_entity: str,
        before: timedelta,
        after: timedelta,
        min_length: int,
    ) -> None:
        """Remember where to look and how to read it."""
        self.hass = hass
        self.calendar_entity = calendar_entity
        self.before = before
        self.after = after
        self.min_length = min_length
        self._bookings: list[Booking] = []
        self._fetched_at: datetime | None = None
        self._issues: set[str] = set()
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    async def async_refresh(self) -> None:
        """Read the calendar again."""
        async with self._lock:
            await self._async_refresh()

    async def async_ensure_fresh(self, max_age: timedelta = CALENDAR_REFRESH) -> bool:
        """Refresh when the cache is older than `max_age`; says whether it did."""
        fetched = self._fetched_at
        if fetched is None or not timedelta(0) <= dt_util.now() - fetched < max_age:
            await self.async_refresh()
            return True
        return False

    async def async_match_fresh(self, code: str) -> Booking | None:
        """Match a code; on a miss, reread a calendar older than a few seconds and retry."""
        await self.async_ensure_fresh()
        booking = self.match(code)
        if booking is None and await self.async_ensure_fresh(CALENDAR_RECHECK):
            booking = self.match(code)
        return booking

    async def _async_refresh(self) -> None:
        now = dt_util.now()
        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(self.calendar_entity) if component else None
        if entity is None:
            self.last_error = f"calendar {self.calendar_entity} not found"
            self._sync_issues(
                {ISSUE_CALENDAR: ("calendar_missing", {"entity": self.calendar_entity})}
            )
            self._drop_if_stale(now)
            return
        try:
            events = await entity.async_get_events(
                self.hass,
                now - self.after - CALENDAR_MARGIN,
                now + self.before + CALENDAR_LOOKAHEAD,
            )
        except Exception as err:  # calendar platforms raise all kinds of errors
            self.last_error = f"calendar {self.calendar_entity} unreadable: {type(err).__name__}"
            _LOGGER.warning("Calendar %s could not be read: %s", self.calendar_entity, err)
            self._drop_if_stale(now)
            return
        self._bookings, issues = self._parse(events)
        self._fetched_at = now
        self.last_error = None
        self._sync_issues(issues)

    def _drop_if_stale(self, now: datetime) -> None:
        if self._fetched_at is None or now - self._fetched_at > CALENDAR_STALE:
            self._bookings = []

    def _parse(
        self, events: list[CalendarEvent]
    ) -> tuple[list[Booking], dict[str, tuple[str, dict[str, str]]]]:
        issues: dict[str, tuple[str, dict[str, str]]] = {}
        candidates: list[Booking] = []
        for event in events:
            code = extract_code(event.description)
            if code is None:
                continue
            key = _booking_key(event)
            if len(code) < self.min_length:
                issues[f"{ISSUE_PREFIX}short_{key}"] = (
                    "code_too_short",
                    {"title": event.summary, "min_length": str(self.min_length)},
                )
                continue
            candidates.append(
                Booking(
                    key=key,
                    uid=event.uid,
                    title=event.summary,
                    code=code,
                    valid_from=event.start_datetime_local - self.before,
                    valid_until=event.end_datetime_local + self.after,
                )
            )

        clashing: set[str] = set()
        for i, first in enumerate(candidates):
            for second in candidates[i + 1 :]:
                if (
                    first.key != second.key
                    and hmac.compare_digest(first.code, second.code)
                    and first.valid_from < second.valid_until
                    and second.valid_from < first.valid_until
                ):
                    clashing |= {first.key, second.key}
                    pair = "_".join(sorted((first.key, second.key)))
                    issues[f"{ISSUE_PREFIX}duplicate_{pair}"] = (
                        "duplicate_code",
                        {"first": first.title, "second": second.title},
                    )
        return [b for b in candidates if b.key not in clashing], issues

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
        """Remove every issue this index raised (on unload)."""
        self._sync_issues({})

    def valid(self, now: datetime | None = None) -> list[Booking]:
        """Bookings whose code works right now."""
        now = now or dt_util.now()
        return [b for b in self._bookings if b.is_valid(now)]

    def get(self, key: str, now: datetime | None = None) -> Booking | None:
        """A currently valid booking by key."""
        return next((b for b in self.valid(now) if b.key == key), None)

    def match(self, code: str, now: datetime | None = None) -> Booking | None:
        """Find the valid booking with this code, comparing in constant time."""
        given = normalize_code(code).encode()
        found: Booking | None = None
        for booking in self.valid(now):
            # No early exit: every comparison runs.
            if hmac.compare_digest(given, booking.code.encode()) and found is None:
                found = booking
        return found

    def find(self, query: str) -> list[Booking]:
        """Bookings in the cache whose uid or title matches (for revoking)."""
        wanted = query.strip().casefold()
        return [
            b
            for b in self._bookings
            if b.title.strip().casefold() == wanted or (b.uid or "").casefold() == wanted
        ]
