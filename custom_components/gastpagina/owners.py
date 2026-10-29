"""Owner codes: valid regardless of bookings, stored only as scrypt hashes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import secrets
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .bookings import normalize_code
from .const import OWNER_LAST_USED_EVERY, OWNER_STORAGE_KEY, STORAGE_VERSION

# scrypt cost: about 16 MB and a few tens of ms per check, always in the executor.
_SCRYPT: dict[str, int] = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def _derive(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(code.encode(), salt=salt, **_SCRYPT)


def _derive_all(code: str, salts: list[bytes]) -> list[bytes]:
    return [_derive(code, salt) for salt in salts]


@dataclass(slots=True)
class Owner:
    """One owner code. `id` changes with the code, which ends that code's sessions."""

    id: str
    name: str
    salt: bytes
    digest: bytes
    created: datetime
    last_used: datetime | None = None


class OwnerCodes:
    """The list of owner codes, kept in its own store."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Set up the store."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, OWNER_STORAGE_KEY, private=True, atomic_writes=True
        )
        self._owners: dict[str, Owner] = {}
        # sha256(normalized code) -> equals some owner code; in memory only.
        self._taken_cache: dict[str, bool] = {}

    async def async_load(self) -> None:
        """Load the stored owners."""
        data = await self._store.async_load() or {}
        now = dt_util.utcnow()
        for raw in data.get("owners", []):
            try:
                owner = Owner(
                    id=raw["id"],
                    name=raw["name"],
                    salt=bytes.fromhex(raw["salt"]),
                    digest=bytes.fromhex(raw["hash"]),
                    created=dt_util.parse_datetime(raw["created"]) or now,
                    last_used=dt_util.parse_datetime(raw["last_used"])
                    if raw.get("last_used")
                    else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._owners[owner.name.casefold()] = owner

    def _data(self) -> dict[str, Any]:
        return {
            "owners": [
                {
                    "id": o.id,
                    "name": o.name,
                    "salt": o.salt.hex(),
                    "hash": o.digest.hex(),
                    "created": o.created.isoformat(),
                    "last_used": o.last_used.isoformat() if o.last_used else None,
                }
                for o in self._owners.values()
            ]
        }

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data, 1)

    async def async_save(self) -> None:
        """Write now (on unload)."""
        await self._store.async_save(self._data())

    # --- lookups -------------------------------------------------------------

    def names(self) -> list[str]:
        """Owner names, in the order they were added."""
        return [o.name for o in self._owners.values()]

    def by_name(self, name: str) -> Owner | None:
        """Owner by name, ignoring case."""
        return self._owners.get(name.strip().casefold())

    def by_id(self, owner_id: str) -> Owner | None:
        """Owner by the id its sessions carry."""
        return next((o for o in self._owners.values() if o.id == owner_id), None)

    def listing(self) -> list[dict[str, Any]]:
        """Names and dates, never codes."""
        return [
            {
                "name": o.name,
                "created": dt_util.as_local(o.created).isoformat(),
                "last_used": dt_util.as_local(o.last_used).isoformat() if o.last_used else None,
            }
            for o in self._owners.values()
        ]

    async def async_match(self, code: str) -> Owner | None:
        """The owner whose code this is; every stored hash is checked, in constant time."""
        normalized = normalize_code(code)
        if not normalized or not self._owners:
            return None
        owners = list(self._owners.values())
        digests = await self.hass.async_add_executor_job(
            _derive_all, normalized, [o.salt for o in owners]
        )
        found: Owner | None = None
        for owner, digest in zip(owners, digests, strict=True):
            if hmac.compare_digest(digest, owner.digest) and found is None:
                found = owner
        return found

    async def async_taken(self, codes: Iterable[str]) -> set[str]:
        """Which of these normalized codes equal an owner code (cached per code)."""
        result: set[str] = set()
        for code in set(codes):
            key = hashlib.sha256(code.encode()).hexdigest()
            if key not in self._taken_cache:
                self._taken_cache[key] = await self.async_match(code) is not None
            if self._taken_cache[key]:
                result.add(code)
        return result

    # --- changes -------------------------------------------------------------

    def touch(self, owner: Owner) -> None:
        """Remember use, at most once an hour (so a busy page does not keep writing)."""
        now = dt_util.utcnow()
        if owner.last_used is None or now - owner.last_used >= OWNER_LAST_USED_EVERY:
            owner.last_used = now
            self._schedule_save()

    async def async_set(self, name: str, normalized_code: str) -> tuple[Owner, Owner | None]:
        """Add an owner or replace one's code; returns the new and the replaced entry."""
        salt = secrets.token_bytes(16)
        digest = await self.hass.async_add_executor_job(_derive, normalized_code, salt)
        old = self._owners.get(name.casefold())
        owner = Owner(
            id=secrets.token_hex(12),
            name=name,
            salt=salt,
            digest=digest,
            created=old.created if old else dt_util.utcnow(),
        )
        self._owners[name.casefold()] = owner
        self._taken_cache.clear()
        await self.async_save()
        return owner, old

    async def async_remove(self, name: str) -> Owner | None:
        """Remove an owner code; returns it, or None if unknown."""
        owner = self._owners.pop(name.strip().casefold(), None)
        if owner is not None:
            self._taken_cache.clear()
            await self.async_save()
        return owner
