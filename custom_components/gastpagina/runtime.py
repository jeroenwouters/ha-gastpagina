"""Everything one config entry keeps alive: bookings, sessions, devices, throttling."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml

from .autocodes import CodeFiller, CodeSettings
from .bookings import Booking, BookingIndex, normalize_code
from .const import (
    CONF_AUTO_CODES,
    CONF_CALENDAR,
    CONF_CODE_PREFIX,
    CONF_HOURS_AFTER,
    CONF_HOURS_BEFORE,
    CONF_LOOKAHEAD_DAYS,
    CONF_MAX_ATTEMPTS,
    CONF_MIN_CODE_LENGTH,
    CONF_NOTIFY_NEW_CODE,
    CONFIG_FILE,
    DEFAULT_AUTO_CODES,
    DEFAULT_CODE_PREFIX,
    DEFAULT_HOURS_AFTER,
    DEFAULT_HOURS_BEFORE,
    DEFAULT_LOOKAHEAD_DAYS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MIN_CODE_LENGTH,
    DEFAULT_NOTIFY_NEW_CODE,
    DOMAIN,
    EVENT_ACTION,
    EVENT_LOGIN,
    GLOBAL_FAILURE_LIMIT,
    OWNER_SESSION_TTL,
    ROLE_GUEST,
    ROLE_OWNER,
    SIGNAL_CONFIG,
    SIGNAL_RECHECK,
    SIGNAL_UPDATE,
)
from .controller import Controller
from .devices import DeviceConfig, Problem, parse_config
from .owners import OwnerCodes
from .ratelimit import LoginLimiter
from .sessions import Session, SessionManager

type GastConfigEntry = ConfigEntry[GastRuntime]


def _read_file(path: Path) -> Any:
    return load_yaml(path)


class GastRuntime:
    """State of the integration while its entry is loaded."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Build the parts from the entry's options."""
        options = {**entry.data, **entry.options}
        self.hass = hass
        self.owners = OwnerCodes(hass)
        self.bookings = BookingIndex(
            hass,
            options[CONF_CALENDAR],
            before=timedelta(hours=int(options.get(CONF_HOURS_BEFORE, DEFAULT_HOURS_BEFORE))),
            after=timedelta(hours=int(options.get(CONF_HOURS_AFTER, DEFAULT_HOURS_AFTER))),
            min_length=int(options.get(CONF_MIN_CODE_LENGTH, DEFAULT_MIN_CODE_LENGTH)),
            owner_taken=self.owners.async_taken,
        )
        self.codes = CodeFiller(
            hass,
            options[CONF_CALENDAR],
            CodeSettings(
                enabled=bool(options.get(CONF_AUTO_CODES, DEFAULT_AUTO_CODES)),
                prefix=str(options.get(CONF_CODE_PREFIX, DEFAULT_CODE_PREFIX)).upper(),
                notify=bool(options.get(CONF_NOTIFY_NEW_CODE, DEFAULT_NOTIFY_NEW_CODE)),
                lookahead=timedelta(
                    days=int(options.get(CONF_LOOKAHEAD_DAYS, DEFAULT_LOOKAHEAD_DAYS))
                ),
                min_length=self.bookings.min_length,
                after=self.bookings.after,
            ),
            owner_taken=self.owners.async_taken,
        )
        self.sessions = SessionManager(hass)
        self.limiter = LoginLimiter(int(options.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS)))
        self.errors: deque[str] = deque(maxlen=20)
        self.controller = Controller(hass, DeviceConfig(), self.errors)
        self.access_enabled = True
        self.closed = False
        self._config_issues: set[str] = set()
        self._file_problems: list[Problem] = []

    # --- device file -----------------------------------------------------

    async def async_reload_config(self) -> None:
        """(Re)read gastpagina.yaml; a broken file keeps the previous devices."""
        path = Path(self.hass.config.path(CONFIG_FILE))
        try:
            raw = await self.hass.async_add_executor_job(_read_file, path)
        except FileNotFoundError:
            config = DeviceConfig(
                problems=[Problem("config_missing", "config_missing", {"path": str(path)})]
            )
        except HomeAssistantError as err:
            config = DeviceConfig(
                problems=[Problem("config_invalid", "config_invalid", {"error": str(err)[:200]})]
            )
        else:
            config = parse_config(raw)

        file_broken = any(
            p.issue_id in ("config_invalid", "config_missing") for p in config.problems
        )
        if file_broken and self.controller.config.devices:
            self._file_problems = config.problems
        else:
            self.controller.config = config
            self._file_problems = []
        self.check_entities()
        async_dispatcher_send(self.hass, SIGNAL_CONFIG)

    @callback
    def check_entities(self) -> None:
        """Raise issues for entities that do not exist (yet), then sync all file issues."""
        registry = er.async_get(self.hass)
        problems = list(self.controller.config.problems) + self._file_problems
        for device in self.controller.config.devices.values():
            if self.hass.states.get(device.entity_id) is None and not registry.async_get(
                device.entity_id
            ):
                problems.append(
                    Problem(
                        f"missing_entity_{device.id}",
                        "missing_entity",
                        {"device": device.id, "entity": device.entity_id},
                    )
                )
        wanted = {p.issue_id: p for p in problems}
        for issue_id in self._config_issues - wanted.keys():
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for problem in wanted.values():
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                problem.issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR
                if problem.translation_key in ("config_invalid", "config_missing")
                else ir.IssueSeverity.WARNING,
                translation_key=problem.translation_key,
                translation_placeholders=problem.placeholders,
            )
        self._config_issues = set(wanted)

    # --- periodic ------------------------------------------------------------

    async def async_tick(self, _now: datetime | None = None) -> None:
        """Every minute: reread the calendar, fill in missing codes, drop stale sessions."""
        await self.bookings.async_refresh()
        await self.async_fill_codes()
        self.prune_sessions()
        self.notify()
        async_dispatcher_send(self.hass, SIGNAL_RECHECK)

    async def async_started(self, _hass: HomeAssistant) -> None:
        """Once HA runs, every integration has had a chance to add its entities."""
        self.check_entities()
        await self.async_tick()

    async def async_fill_codes(self) -> int:
        """Write codes into bookings without one; reread so they work at once."""
        created = await self.codes.async_fill()
        if created:
            await self.bookings.async_refresh()
        return created

    async def async_generate_code(self, query: str) -> str:
        """Replace a booking's code (it leaked) and sign out its guests."""
        key, code = await self.codes.async_replace(query)
        self.sessions.remove_where(lambda s: s.booking == key)
        await self.bookings.async_refresh()
        self.notify()
        async_dispatcher_send(self.hass, SIGNAL_RECHECK)
        return code

    @callback
    def prune_sessions(self) -> int:
        """Remove sessions whose booking or owner code is gone, changed or expired."""
        return self.sessions.remove_where(lambda s: not self._session_ok(s, touch=False))

    @callback
    def notify(self) -> None:
        """Let the management entities update."""
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    @callback
    def shutdown(self) -> None:
        """Entry unloads: close streams, drop issues."""
        self.closed = True
        async_dispatcher_send(self.hass, SIGNAL_RECHECK)
        self.bookings.clear_issues()
        self.codes.clear_issues()
        for issue_id in self._config_issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._config_issues = set()

    # --- sessions ------------------------------------------------------------

    def _session_ok(self, session: Session, *, touch: bool = True) -> bool:
        """Whether the booking or owner code behind a session still holds.

        `touch` marks the session as used: an owner session then slides another
        30 days ahead. The periodic clean-up checks without touching.
        """
        now = dt_util.utcnow()
        if session.is_owner:
            owner = self.owners.by_id(session.booking)
            if owner is None or session.expires_at <= now:
                return False
            if touch:
                self.sessions.extend(session, now + OWNER_SESSION_TTL)
                self.owners.touch(owner)
            return True
        booking = self.bookings.get(session.booking)
        if booking is None or booking.fingerprint != session.fingerprint:
            return False
        self.sessions.extend(session, booking.valid_until)
        return True

    def allowed(self, session: Session) -> bool:
        """Owners may use the page even with the access switch off."""
        return self.access_enabled or session.is_owner

    async def async_authenticate(self, token: str | None) -> Session | None:
        """The session for a token, if its booking or owner code still holds.

        The access switch is not checked here; see `allowed`.
        """
        if self.closed or not token:
            return None
        await self.bookings.async_ensure_fresh()
        session = self.sessions.get(token)
        if session is None:
            return None
        if not self._session_ok(session):
            self.sessions.remove(session)
            self.notify()
            return None
        return session

    async def async_authenticate_ticket(self, ticket: str | None) -> Session | None:
        """Like async_authenticate, for a single-use stream ticket."""
        if self.closed:
            return None
        await self.bookings.async_ensure_fresh()
        session = self.sessions.redeem_ticket(ticket)
        return session if session is not None and self._session_ok(session) else None

    def session_valid(self, session: Session) -> bool:
        """Re-check a session held by an open stream."""
        return (
            not self.closed
            and self.allowed(session)
            and self.sessions.is_live(session)
            and self._session_ok(session)
        )

    def revoke(self, query: str | None, owners: bool = False) -> int:
        """End guest sessions (all, or of bookings matching a title or uid).

        Owner sessions end only with `owners`.
        """
        keys = {b.key for b in self.bookings.find(query)} if query is not None else None

        def doomed(session: Session) -> bool:
            if session.is_owner:
                return owners
            return keys is None or session.booking in keys

        count = self.sessions.remove_where(doomed)
        self.notify()
        async_dispatcher_send(self.hass, SIGNAL_RECHECK)
        return count

    @callback
    def set_access(self, enabled: bool) -> None:
        """Master switch."""
        self.access_enabled = enabled
        self.notify()
        async_dispatcher_send(self.hass, SIGNAL_RECHECK)

    # --- login and actions -------------------------------------------------------

    async def async_login(self, code: str, ip: str | None) -> tuple[int, dict[str, Any]]:
        """Check a code, owner codes first; returns HTTP status and body."""
        ip = ip or "unknown"
        if (wait := self.limiter.retry_after(ip)) is not None:
            return 429, {"error": "te_veel_pogingen", "retry_after": wait}

        if (owner := await self.owners.async_match(code)) is not None:
            self.limiter.record_success(ip)
            token, session = self.sessions.create_owner(owner)
            self.owners.touch(owner)
            self.hass.bus.async_fire(
                EVENT_LOGIN, {"role": ROLE_OWNER, "owner": owner.name}, context=Context()
            )
            self.notify()
            expires = dt_util.as_local(session.expires_at).isoformat()
            return 200, {"token": token, "expires_at": expires, "role": ROLE_OWNER}

        booking = await self.bookings.async_match_fresh(code)
        if booking is not None and not self.access_enabled:
            return 503, {"error": "uit"}
        if booking is None:
            blocked = self.limiter.record_failure(ip)
            if blocked is not None:
                self._notify_block(blocked, ip)
            self.notify()
            return 401, {"error": "code"}
        self.limiter.record_success(ip)
        token = self.sessions.create(booking)
        self.hass.bus.async_fire(EVENT_LOGIN, {"role": ROLE_GUEST}, context=Context())
        self.notify()
        return 200, {"token": token, "expires_at": _iso(booking)}

    async def async_action(
        self, device: str, action: str, value: Any, session: Session
    ) -> dict[str, Any]:
        """Run an action under a context of its own, announced by an event first.

        The logbook attributes the resulting state changes to that event, so it
        shows "Gastpagina" (or "Gastpagina (eigenaar …)") rather than a user.
        Raises ActionError.
        """
        context = Context()
        data: dict[str, Any] = {"device": device, "action": action, "role": session.role}
        if session.is_owner:
            data["owner"] = session.owner

        @callback
        def announce() -> None:
            self.hass.bus.async_fire(EVENT_ACTION, data, context=context)

        return await self.controller.async_act(device, action, value, context, announce)

    # --- owner codes -------------------------------------------------------------

    async def async_add_owner(self, name: str, code: str | None) -> dict[str, Any]:
        """Add an owner code (or replace one's code); a generated code is returned once."""
        name = name.strip()
        if not 1 <= len(name) <= 40:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="owner_name_invalid"
            )
        in_use = await self.codes.async_codes_in_use()
        in_use |= {b.code for b in self.bookings.valid()}
        generated = code is None
        if code is None:
            code = await self.codes.async_new_code(in_use)
        normalized = normalize_code(code)
        if len(normalized) < self.bookings.min_length:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="owner_code_too_short",
                translation_placeholders={"min_length": str(self.bookings.min_length)},
            )
        if normalized in in_use:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="owner_code_in_use"
            )
        other = await self.owners.async_match(normalized)
        if other is not None and other.name.casefold() != name.casefold():
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="owner_code_in_use"
            )
        _owner, replaced = await self.owners.async_set(name, normalized)
        if replaced is not None:
            self.sessions.remove_where(lambda s: s.is_owner and s.booking == replaced.id)
        await self._owners_changed()
        return {"name": name, "code": code} if generated else {"name": name}

    async def async_remove_owner(self, name: str) -> None:
        """Remove an owner code and end its sessions."""
        owner = await self.owners.async_remove(name)
        if owner is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="owner_not_found",
                translation_placeholders={"name": name},
            )
        self.sessions.remove_where(lambda s: s.is_owner and s.booking == owner.id)
        await self._owners_changed()

    async def _owners_changed(self) -> None:
        # A booking code may now clash with an owner code, or no longer.
        await self.bookings.async_refresh()
        self.prune_sessions()
        self.notify()
        async_dispatcher_send(self.hass, SIGNAL_RECHECK)

    def _notify_block(self, kind: str, ip: str) -> None:
        if kind == "global":
            persistent_notification.async_create(
                self.hass,
                f"{GLOBAL_FAILURE_LIMIT} foute codes in het laatste uur. "
                "Aanmelden op de gastpagina is 15 minuten dicht.",
                title="Gastpagina",
                notification_id=f"{DOMAIN}_global_lock",
            )
        else:
            persistent_notification.async_create(
                self.hass,
                f"Te veel foute codes vanaf {ip}. Dat adres moet 15 minuten wachten.",
                title="Gastpagina",
                notification_id=f"{DOMAIN}_blocked",
            )


def _iso(booking: Booking) -> str:
    return dt_util.as_local(booking.valid_until).isoformat()
