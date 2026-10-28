"""Everything one config entry keeps alive: bookings, sessions, devices, throttling."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml

from .bookings import Booking, BookingIndex
from .const import (
    CONF_CALENDAR,
    CONF_HOURS_AFTER,
    CONF_HOURS_BEFORE,
    CONF_MAX_ATTEMPTS,
    CONF_MIN_CODE_LENGTH,
    CONFIG_FILE,
    DEFAULT_HOURS_AFTER,
    DEFAULT_HOURS_BEFORE,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MIN_CODE_LENGTH,
    DOMAIN,
    EVENT_ACTION,
    EVENT_LOGIN,
    GLOBAL_FAILURE_LIMIT,
    SIGNAL_CONFIG,
    SIGNAL_RECHECK,
    SIGNAL_UPDATE,
)
from .controller import Controller
from .devices import DeviceConfig, Problem, parse_config
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
        self.bookings = BookingIndex(
            hass,
            options[CONF_CALENDAR],
            before=timedelta(hours=int(options.get(CONF_HOURS_BEFORE, DEFAULT_HOURS_BEFORE))),
            after=timedelta(hours=int(options.get(CONF_HOURS_AFTER, DEFAULT_HOURS_AFTER))),
            min_length=int(options.get(CONF_MIN_CODE_LENGTH, DEFAULT_MIN_CODE_LENGTH)),
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
        """Every minute: reread the calendar and drop sessions that lost their booking."""
        await self.bookings.async_refresh()
        self.prune_sessions()
        self.notify()
        async_dispatcher_send(self.hass, SIGNAL_RECHECK)

    async def async_started(self, _hass: HomeAssistant) -> None:
        """Once HA runs, every integration has had a chance to add its entities."""
        self.check_entities()
        await self.async_tick()

    @callback
    def prune_sessions(self) -> int:
        """Remove sessions whose booking is gone, expired or has a new code."""
        return self.sessions.remove_where(lambda s: not self._session_ok(s))

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
        for issue_id in self._config_issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._config_issues = set()

    # --- sessions ------------------------------------------------------------

    def _session_ok(self, session: Session) -> bool:
        booking = self.bookings.get(session.booking)
        if booking is None or booking.fingerprint != session.fingerprint:
            return False
        self.sessions.extend(session, booking.valid_until)
        return True

    async def async_authenticate(self, token: str | None) -> Session | None:
        """The session for a token, if access is on and its booking still holds."""
        if self.closed or not self.access_enabled or not token:
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
        if self.closed or not self.access_enabled:
            return None
        await self.bookings.async_ensure_fresh()
        session = self.sessions.redeem_ticket(ticket)
        return session if session is not None and self._session_ok(session) else None

    def session_valid(self, session: Session) -> bool:
        """Re-check a session held by an open stream."""
        return (
            not self.closed
            and self.access_enabled
            and self.sessions.is_live(session)
            and self._session_ok(session)
        )

    def revoke(self, query: str | None) -> int:
        """End all sessions, or those of bookings matching a title or uid."""
        if query is None:
            count = self.sessions.remove_where(lambda _s: True)
        else:
            keys = {b.key for b in self.bookings.find(query)}
            count = self.sessions.remove_where(lambda s: s.booking in keys)
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
        """Check a code; returns HTTP status and body."""
        ip = ip or "unknown"
        if (wait := self.limiter.retry_after(ip)) is not None:
            return 429, {"error": "te_veel_pogingen", "retry_after": wait}
        booking = await self.bookings.async_match_fresh(code)
        if booking is None:
            blocked = self.limiter.record_failure(ip)
            if blocked is not None:
                self._notify_block(blocked, ip)
            self.notify()
            return 401, {"error": "code"}
        self.limiter.record_success(ip)
        token = self.sessions.create(booking)
        self.hass.bus.async_fire(EVENT_LOGIN, {}, context=Context())
        self.notify()
        return 200, {"token": token, "expires_at": _iso(booking)}

    async def async_action(self, device: str, action: str, value: Any) -> dict[str, Any]:
        """Run an action under a context of its own, announced by an event first.

        The logbook attributes the resulting state changes to that event, so it
        shows "Gastpagina" rather than a user. Raises ActionError.
        """
        context = Context()

        @callback
        def announce() -> None:
            self.hass.bus.async_fire(
                EVENT_ACTION, {"device": device, "action": action}, context=context
            )

        return await self.controller.async_act(device, action, value, context, announce)

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
