"""Guest and owner sessions: random tokens, persisted only as SHA-256 hashes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import secrets
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .bookings import Booking
from .const import (
    MAX_SESSIONS_PER_BOOKING,
    OWNER_SESSION_TTL,
    ROLE_GUEST,
    ROLE_OWNER,
    STORAGE_KEY,
    STORAGE_VERSION,
    TICKET_TTL,
)
from .owners import Owner

_MAX_TOKEN_LENGTH = 128


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(slots=True)
class Session:
    """One device of one guest or owner.

    For a guest, `booking` is the booking key and `fingerprint` changes with its
    code. For an owner, both hold the owner code's id, which changes with the code.
    """

    token_hash: str
    booking: str
    fingerprint: str
    expires_at: datetime
    created_at: datetime
    role: str = ROLE_GUEST
    owner: str | None = None

    @property
    def is_owner(self) -> bool:
        """Signed in with an owner code."""
        return self.role == ROLE_OWNER


class SessionManager:
    """Creates, looks up and persists sessions.

    Lookups go through the SHA-256 of the token, so timing reveals nothing
    about stored tokens; the raw token is never kept.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Set up the store."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._sessions: dict[str, Session] = {}
        self._tickets: dict[str, tuple[str, datetime]] = {}

    async def async_load(self) -> None:
        """Load sessions that have not expired."""
        data = await self._store.async_load() or {}
        now = dt_util.utcnow()
        for raw in data.get("sessions", []):
            try:
                session = Session(
                    token_hash=raw["h"],
                    booking=raw["b"],
                    fingerprint=raw["f"],
                    expires_at=dt_util.parse_datetime(raw["e"]) or now,
                    created_at=dt_util.parse_datetime(raw["c"]) or now,
                    role=raw.get("r", ROLE_GUEST),
                    owner=raw.get("o"),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if session.expires_at > now:
                self._sessions[session.token_hash] = session

    def _data(self) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "h": s.token_hash,
                    "b": s.booking,
                    "f": s.fingerprint,
                    "e": s.expires_at.isoformat(),
                    "c": s.created_at.isoformat(),
                    "r": s.role,
                    "o": s.owner,
                }
                for s in self._sessions.values()
            ]
        }

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data, 1)

    async def async_save(self) -> None:
        """Write now (on unload)."""
        await self._store.async_save(self._data())

    def create(self, booking: Booking) -> str:
        """Start a guest session for a booking; the oldest one past the limit goes."""
        return self._create(
            key=booking.key, fingerprint=booking.fingerprint, expires_at=booking.valid_until
        )

    def create_owner(self, owner: Owner) -> tuple[str, Session]:
        """Start an owner session, valid for 30 days after its last use."""
        token = self._create(
            key=owner.id,
            fingerprint=owner.id,
            expires_at=dt_util.utcnow() + OWNER_SESSION_TTL,
            role=ROLE_OWNER,
            owner=owner.name,
        )
        return token, self._sessions[_hash(token)]

    def _create(
        self,
        *,
        key: str,
        fingerprint: str,
        expires_at: datetime,
        role: str = ROLE_GUEST,
        owner: str | None = None,
    ) -> str:
        now = dt_util.utcnow()
        own = sorted(
            (s for s in self._sessions.values() if s.booking == key),
            key=lambda s: s.created_at,
        )
        for old in own[: max(0, len(own) - MAX_SESSIONS_PER_BOOKING + 1)]:
            del self._sessions[old.token_hash]
        token = secrets.token_urlsafe(32)
        self._sessions[_hash(token)] = Session(
            token_hash=_hash(token),
            booking=key,
            fingerprint=fingerprint,
            expires_at=expires_at,
            created_at=now,
            role=role,
            owner=owner,
        )
        self._schedule_save()
        return token

    def get(self, token: str | None) -> Session | None:
        """The session for a raw token, if it exists and has not expired."""
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            return None
        session = self._sessions.get(_hash(token))
        if session is None:
            return None
        if session.expires_at <= dt_util.utcnow():
            self.remove(session)
            return None
        return session

    def is_live(self, session: Session) -> bool:
        """Whether a session object is still the stored, unexpired one."""
        return (
            self._sessions.get(session.token_hash) is session
            and session.expires_at > dt_util.utcnow()
        )

    def remove(self, session: Session) -> None:
        """End one session."""
        if self._sessions.pop(session.token_hash, None) is not None:
            self._schedule_save()

    def extend(self, session: Session, until: datetime) -> None:
        """Follow a booking whose end moved, or slide an owner session."""
        if session.expires_at != until:
            session.expires_at = until
            self._schedule_save()

    def remove_where(self, predicate: Callable[[Session], bool]) -> int:
        """End all sessions matching; returns how many."""
        doomed = [s for s in self._sessions.values() if predicate(s)]
        for session in doomed:
            del self._sessions[session.token_hash]
        if doomed:
            self._schedule_save()
        return len(doomed)

    def all(self) -> list[Session]:
        """All stored sessions."""
        return list(self._sessions.values())

    @property
    def count(self) -> int:
        """Number of guest sessions."""
        return sum(not s.is_owner for s in self._sessions.values())

    @property
    def owner_count(self) -> int:
        """Number of owner sessions."""
        return sum(s.is_owner for s in self._sessions.values())

    def issue_ticket(self, session: Session) -> str:
        """A short-lived, single-use ticket for EventSource, which cannot send headers."""
        now = dt_util.utcnow()
        self._tickets = {h: t for h, t in self._tickets.items() if t[1] > now}
        ticket = secrets.token_urlsafe(24)
        self._tickets[_hash(ticket)] = (session.token_hash, now + TICKET_TTL)
        return ticket

    def redeem_ticket(self, ticket: str | None) -> Session | None:
        """Exchange a ticket for its session, once."""
        if not ticket or len(ticket) > _MAX_TOKEN_LENGTH:
            return None
        entry = self._tickets.pop(_hash(ticket), None)
        if entry is None or entry[1] <= dt_util.utcnow():
            return None
        session = self._sessions.get(entry[0])
        return session if session is not None and self.is_live(session) else None
