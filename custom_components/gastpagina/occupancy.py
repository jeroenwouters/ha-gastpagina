"""The calendar seen as nights: who has which nights, and what blocks the market."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEvent
from homeassistant.util import dt as dt_util

from . import metadata as md
from .bookings import booking_key, code_value, is_opt_out

# Sources that are guests paying for nights.
GUEST_SOURCES = ("airbnb", "booking", "vrbo", "website", "facebook")


def nights(event: CalendarEvent) -> tuple[date, date] | None:
    """First night and the day after the last night; None when no night is covered."""
    if isinstance(event.start, datetime):
        first = dt_util.as_local(event.start_datetime_local).date()
        end = dt_util.as_local(event.end_datetime_local).date()
    else:
        first, end = event.start, event.end
        if isinstance(end, datetime):  # mixed, should not happen
            end = end.date()
    return (first, end) if end > first else None


@dataclass(frozen=True, slots=True)
class Stay:
    """An event that covers at least one night."""

    event: CalendarEvent = field(repr=False)
    meta: dict[str, str] = field(repr=False)
    key: str
    first: date
    end: date

    @property
    def source(self) -> str:
        """Where it came from ('' when the owner typed it in)."""
        return self.meta.get(md.SOURCE, "").casefold()

    @property
    def is_option(self) -> bool:
        """A website request waiting for the owner."""
        return md.is_option(self.meta)

    @property
    def is_feed_block(self) -> bool:
        """Dates a platform marks unavailable (often an echo of our own feed)."""
        return self.meta.get(md.KIND, "").casefold() == md.KIND_BLOCK

    @property
    def blocks(self) -> bool:
        """Whether these nights are off the market."""
        return md.blocks(self.meta)

    @property
    def is_booking(self) -> bool:
        """Guests staying (confirmed): from a channel, or entered with a guest code."""
        if self.is_option or self.is_feed_block:
            return False
        if self.source in GUEST_SOURCES:
            return True
        value = code_value(self.event.description)
        return value is not None and not is_opt_out(value)

    @property
    def count_nights(self) -> int:
        """Number of nights."""
        return (self.end - self.first).days

    def overlaps(self, first: date, end: date) -> bool:
        """Whether it shares a night with [first, end)."""
        return self.first < end and first < self.end


def stays(events: Iterable[CalendarEvent]) -> list[Stay]:
    """Events that cover nights, sorted by first night."""
    found: list[Stay] = []
    for event in events:
        span = nights(event)
        if span is None:
            continue
        found.append(
            Stay(
                event=event,
                meta=md.read(event.description),
                key=booking_key(event),
                first=span[0],
                end=span[1],
            )
        )
    return sorted(found, key=lambda s: (s.first, s.end, s.key))


def busy(all_stays: Iterable[Stay]) -> list[tuple[date, date]]:
    """Merged ranges of blocked nights."""
    ranges = sorted((s.first, s.end) for s in all_stays if s.blocks)
    merged: list[tuple[date, date]] = []
    for first, end in ranges:
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((first, end))
    return merged


def is_free(all_stays: Iterable[Stay], first: date, end: date) -> bool:
    """No blocking stay shares a night with [first, end)."""
    return not any(s.blocks and s.overlaps(first, end) for s in all_stays)


def clashes(all_stays: list[Stay]) -> list[tuple[Stay, Stay]]:
    """Pairs of guest stays (bookings and options) that share a night.

    Platform blocks are left out: they are usually our own feed coming back.
    """
    guests = [
        s for s in all_stays if s.blocks and not s.is_feed_block and (s.is_booking or s.is_option)
    ]
    pairs: list[tuple[Stay, Stay]] = []
    for i, first in enumerate(guests):
        for second in guests[i + 1 :]:
            if second.first >= first.end:
                break
            if first.key != second.key and first.overlaps(second.first, second.end):
                pairs.append((first, second))
    return pairs


def window(now: datetime) -> tuple[datetime, datetime]:
    """The span the hub reads: a little past, two years ahead."""
    return now - timedelta(days=2), now + timedelta(days=760)
