"""Kanalenhub: Airbnb and Booking.com in, one feed per channel out.

The bookings calendar is the single source of truth. Each channel's iCal feed is
read every few minutes and mirrored into the calendar as events tagged with
`bron:` and `extern:`; the importer touches no other events. Outgoing feeds list
every blocking event except those that came from the channel itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import hashlib
import hmac
import logging
import re
import secrets
from typing import Any

from aiohttp import ClientError
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import ical, metadata as md, occupancy
from .bookings import booking_key
from .calendar_io import CalendarIO, CalendarUnavailable
from .const import (
    DOMAIN,
    EVENT_BOOKING_CANCELLED,
    EVENT_BOOKING_CHANGED,
    EVENT_BOOKING_NEW,
    EVENT_DOUBLE_BOOKING,
    FEED_FAILURES_FOR_ISSUE,
    FEED_MAX_BYTES,
    FEED_TIMEOUT,
    HUB_STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

EXPORT_CHANNELS = ("airbnb", "booking")
LABELS = {"airbnb": "Airbnb", "booking": "Booking", "vrbo": "Vrbo"}
CHANNEL_NAME = re.compile(r"[a-z][a-z0-9]{1,15}")

_PHONE4 = re.compile(r"Last 4 Digits\)?\s*:\s*(\d{4})", re.IGNORECASE)
_RESERVATION = re.compile(r"https://[a-z0-9.]*airbnb\.[a-z.]+/hosting/reservations/details/\w+")
_NOT_AVAILABLE = re.compile(r"not available|blocked|niet beschikbaar", re.IGNORECASE)
# A feed that suddenly drops many future bookings is more likely broken than empty.
MASS_REMOVAL = 3


def label(channel: str) -> str:
    """Display name of a channel."""
    return LABELS.get(channel, channel.capitalize())


@dataclass(frozen=True, slots=True)
class HubSettings:
    """Options of the hub."""

    feeds: dict[str, str]
    check_in: time
    check_out: time
    interval: timedelta


@dataclass(slots=True)
class FeedStatus:
    """How the last reads of one feed went (kept in memory)."""

    ok: bool | None = None
    last_sync: datetime | None = None
    last_attempt: datetime | None = None
    failures: int = 0
    etag: str | None = None
    modified: str | None = None
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Wanted:
    """What one feed event should look like in the calendar."""

    summary: str
    start: datetime
    end: datetime
    meta: dict[str, str | None]
    block: bool


class FeedError(Exception):
    """A feed could not be read; the message is safe to log (no URL)."""


class ChannelHub:
    """Imports channel feeds into the calendar and renders the outgoing feeds."""

    def __init__(
        self,
        hass: HomeAssistant,
        io: CalendarIO,
        settings: HubSettings,
        on_change: Callable[[], Any] | None = None,
    ) -> None:
        """Remember the calendar and the settings; `on_change` runs after a sync wrote."""
        self.hass = hass
        self.io = io
        self.settings = settings
        self._on_change = on_change
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, HUB_STORAGE_KEY)
        self.tokens: dict[str, str] = {}
        self.status: dict[str, FeedStatus] = {name: FeedStatus() for name in settings.feeds}
        self._issues: set[str] = set()
        self._known_clashes: set[str] = set()
        self._sync_lock = asyncio.Lock()

    # --- tokens ------------------------------------------------------------------

    @property
    def channels(self) -> list[str]:
        """Channels that get an outgoing feed."""
        return sorted({*EXPORT_CHANNELS, *self.settings.feeds})

    async def async_load(self) -> None:
        """Read the feed tokens; make the missing ones."""
        data = await self._store.async_load() or {}
        self.tokens = {k: v for k, v in data.get("tokens", {}).items() if isinstance(v, str)}
        if any(channel not in self.tokens for channel in self.channels):
            for channel in self.channels:
                self.tokens.setdefault(channel, secrets.token_urlsafe(32))
            await self._save()

    async def _save(self) -> None:
        await self._store.async_save({"tokens": self.tokens})

    def token_ok(self, channel: str, token: str) -> bool:
        """Constant-time check of a feed token."""
        expected = self.tokens.get(channel)
        if expected is None:
            hmac.compare_digest(token.encode(), b"x" * 43)  # same work either way
            return False
        return hmac.compare_digest(token.encode(), expected.encode())

    async def async_rotate(self, channel: str | None = None) -> list[str]:
        """New token(s): the old feed URL stops working at once."""
        rotated = [channel] if channel else self.channels
        for name in rotated:
            if name not in self.channels:
                raise KeyError(name)
            self.tokens[name] = secrets.token_urlsafe(32)
        await self._save()
        return rotated

    def paths(self) -> dict[str, str]:
        """Feed path per channel (to paste into the platform, after the base URL)."""
        return {
            channel: f"/api/{DOMAIN}/ical/{self.tokens[channel]}/{channel}.ics"
            for channel in self.channels
            if channel in self.tokens
        }

    # --- import ------------------------------------------------------------------

    def due(self, now: datetime) -> list[str]:
        """Feeds whose interval has passed."""
        return [
            name
            for name in self.settings.feeds
            if (last := self.status[name].last_attempt) is None
            or now - last >= self.settings.interval
        ]

    async def async_sync(self, names: list[str] | None = None) -> dict[str, dict[str, int]]:
        """Read the given feeds (default: all); returns the changes per feed."""
        results: dict[str, dict[str, int]] = {}
        async with self._sync_lock:
            for name in names if names is not None else list(self.settings.feeds):
                if name not in self.settings.feeds:
                    continue
                results[name] = await self._sync_one(name)
            self._sync_issues()
        if any(sum(r.values()) for r in results.values()) and self._on_change is not None:
            await self._on_change()
        return results

    async def _sync_one(self, name: str) -> dict[str, int]:
        status = self.status[name]
        now = dt_util.utcnow()
        status.last_attempt = now
        try:
            text = await self._fetch(name, status)
            changes = {"nieuw": 0, "gewijzigd": 0, "geannuleerd": 0}
            if text is not None:
                changes = await self._reconcile(name, ical.parse(text))
        except (FeedError, ical.ICalError, CalendarUnavailable) as err:
            status.ok = False
            status.failures += 1
            _LOGGER.warning("Feed %s could not be synced: %s", name, err)
            return {}
        status.ok = True
        status.failures = 0
        status.last_sync = now
        status.counts = changes
        return changes

    async def _fetch(self, name: str, status: FeedStatus) -> str | None:
        """The feed text, or None when it did not change since the last read."""
        headers = {"User-Agent": "gastpagina/0.4 (Home Assistant)", "Accept": "text/calendar"}
        if status.etag:
            headers["If-None-Match"] = status.etag
        if status.modified:
            headers["If-Modified-Since"] = status.modified
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(FEED_TIMEOUT.total_seconds()):
                async with session.get(self.settings.feeds[name], headers=headers) as resp:
                    if resp.status == 304:
                        return None
                    if resp.status != 200:
                        raise FeedError(f"HTTP {resp.status}")
                    raw = await resp.content.read(FEED_MAX_BYTES + 1)
                    if len(raw) > FEED_MAX_BYTES:
                        raise FeedError("feed too large")
                    status.etag = resp.headers.get("ETag")
                    status.modified = resp.headers.get("Last-Modified")
        except TimeoutError as err:
            raise FeedError("timeout") from err
        except ClientError as err:
            raise FeedError(type(err).__name__) from err
        return raw.decode("utf-8", errors="replace")

    def _wanted(self, name: str, event: ical.FeedEvent) -> Wanted:
        tz = dt_util.get_default_time_zone()
        block = name == "airbnb" and bool(_NOT_AVAILABLE.search(event.summary))
        meta: dict[str, str | None] = {
            md.SOURCE: name,
            md.EXTERNAL: event.uid,
            md.KIND: md.KIND_BLOCK if block else None,
            md.PHONE4: None,
            md.RESERVATION: None,
        }
        title = label(name)
        if block:
            title = f"{label(name)} - geblokkeerd"
        elif name == "airbnb":
            if phone := _PHONE4.search(event.description):
                meta[md.PHONE4] = phone.group(1)
                title = f"Airbnb - {phone.group(1)}"
            if link := _RESERVATION.search(event.description):
                meta[md.RESERVATION] = link.group(0)
        return Wanted(
            summary=title,
            start=datetime.combine(event.start, self.settings.check_in, tz),
            end=datetime.combine(event.end, self.settings.check_out, tz),
            meta=meta,
            block=block,
        )

    async def _reconcile(self, name: str, feed: list[ical.FeedEvent]) -> dict[str, int]:
        now = dt_util.now()
        today = now.date()
        changes = {"nieuw": 0, "gewijzigd": 0, "geannuleerd": 0}
        async with self.io.lock:
            events = await self.io.events(*occupancy.window(now))
            mine: dict[str, Any] = {}
            for event in events:
                meta = md.read(event.description)
                if meta.get(md.SOURCE, "").casefold() == name and meta.get(md.EXTERNAL):
                    mine.setdefault(meta[md.EXTERNAL], event)

            seen: set[str] = set()
            for item in feed:
                if item.end <= today:
                    continue
                seen.add(item.uid)
                wanted = self._wanted(name, item)
                existing = mine.get(item.uid)
                if existing is None:
                    description = md.write("", wanted.meta)
                    if wanted.block:
                        description += "\ncode: geen"
                    await self.io.create(wanted.summary, wanted.start, wanted.end, description)
                    changes["nieuw"] += 1
                    if not wanted.block:
                        self._fire(EVENT_BOOKING_NEW, name, wanted.start, wanted.end)
                    continue
                if await self._update(existing, wanted):
                    changes["gewijzigd"] += 1
                    if not wanted.block:
                        self._fire(EVENT_BOOKING_CHANGED, name, wanted.start, wanted.end)

            gone = [
                event
                for uid, event in mine.items()
                if uid not in seen and event.start_datetime_local > now
            ]
            issue_id = f"feed_mass_removal_{name}"
            if len(gone) > MASS_REMOVAL and len(gone) > len(seen):
                self._issues.add(issue_id)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="feed_mass_removal",
                    translation_placeholders={"feed": label(name), "count": str(len(gone))},
                )
                return changes
            if issue_id in self._issues:
                self._issues.discard(issue_id)
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            for event in gone:
                await self.io.delete(event)
                if md.read(event.description).get(md.KIND) == md.KIND_BLOCK:
                    continue
                changes["geannuleerd"] += 1
                start, end = event.start_datetime_local, event.end_datetime_local
                self._fire(EVENT_BOOKING_CANCELLED, name, start, end)
                persistent_notification.async_create(
                    self.hass,
                    f"{label(name)} meldt een annulering: {event.summary}, "
                    f"{start:%d-%m-%Y} tot {end:%d-%m-%Y}. De afspraak en de code zijn weg.",
                    title="Boeking geannuleerd",
                    notification_id=f"{DOMAIN}_cancel_{booking_key(event)}",
                )
        return changes

    async def _update(self, event: Any, wanted: Wanted) -> bool:
        current = md.read(event.description)
        meta_changed = any((current.get(k) or None) != v for k, v in wanted.meta.items())
        times_changed = (
            event.start_datetime_local != wanted.start or event.end_datetime_local != wanted.end
        )
        if not meta_changed and not times_changed:
            return False
        # The owner may have renamed the event; keep that unless it is still our default.
        default = event.summary.startswith(label(event_source(current)))
        await self.io.update(
            event,
            summary=wanted.summary if default else None,
            start=wanted.start,
            end=wanted.end,
            description=md.write(event.description, wanted.meta),
        )
        return True

    def _fire(self, event_type: str, source: str, start: datetime, end: datetime) -> None:
        self.hass.bus.async_fire(
            event_type,
            {
                "bron": source,
                "aankomst": dt_util.as_local(start).isoformat(),
                "vertrek": dt_util.as_local(end).isoformat(),
            },
        )

    def _sync_issues(self) -> None:
        for name, status in self.status.items():
            issue_id = f"feed_failing_{name}"
            if status.failures >= FEED_FAILURES_FOR_ISSUE:
                self._issues.add(issue_id)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="feed_failing",
                    translation_placeholders={"feed": label(name)},
                )
            elif issue_id in self._issues:
                self._issues.discard(issue_id)
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # --- double bookings ---------------------------------------------------------------

    def check_clashes(self, all_stays: list[occupancy.Stay]) -> int:
        """Raise an issue (and once a notification) for every pair sharing a night."""
        pairs = occupancy.clashes(all_stays)
        wanted: dict[str, tuple[occupancy.Stay, occupancy.Stay]] = {}
        for first, second in pairs:
            wanted["clash_" + "_".join(sorted((first.key, second.key)))] = (first, second)
        for issue_id in {i for i in self._issues if i.startswith("clash_")} - wanted.keys():
            self._issues.discard(issue_id)
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for issue_id, (first, second) in wanted.items():
            placeholders = {
                "first": first.event.summary,
                "second": second.event.summary,
                "from": f"{max(first.first, second.first):%d-%m-%Y}",
            }
            self._issues.add(issue_id)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.CRITICAL,
                translation_key="double_booking",
                translation_placeholders=placeholders,
            )
            if issue_id not in self._known_clashes:
                self.hass.bus.async_fire(EVENT_DOUBLE_BOOKING, placeholders)
                persistent_notification.async_create(
                    self.hass,
                    f"{first.event.summary} en {second.event.summary} delen een nacht "
                    f"(vanaf {placeholders['from']}). Er is niets verwijderd: los het zelf op.",
                    title="Dubbele boeking",
                    notification_id=f"{DOMAIN}_{issue_id}",
                )
        self._known_clashes = set(wanted)
        return len(wanted)

    def clear_issues(self) -> None:
        """Remove every issue the hub raised (on unload)."""
        for issue_id in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues = set()

    # --- export ------------------------------------------------------------------

    async def async_render(self, channel: str) -> str:
        """The outgoing feed for one channel."""
        now = dt_util.now()
        all_stays = occupancy.stays(await self.io.events(*occupancy.window(now)))
        today = now.date()
        blocks = [
            ical.Block(uid=_feed_uid(s.key), start=s.first, end=s.end)
            for s in all_stays
            if s.blocks and s.source != channel and s.end > today
        ]
        return ical.render(blocks, name=f"Gastpagina {label(channel)}", stamp=dt_util.utcnow())


def event_source(meta: dict[str, str]) -> str:
    """The `bron:` of an event."""
    return meta.get(md.SOURCE, "").casefold()


def _feed_uid(key: str) -> str:
    return hashlib.sha256(f"feed|{key}".encode()).hexdigest()[:24] + "@gastpagina"
