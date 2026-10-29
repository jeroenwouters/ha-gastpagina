"""Logbook entries: actions show as "Gastpagina" (or "Gastpagina (eigenaar …)"), never a user."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ACTION, EVENT_LOGIN, ROLE_OWNER


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe login and action events."""

    def who(event: Event[Any]) -> tuple[str, str]:
        if event.data.get("role") == ROLE_OWNER:
            return f"Gastpagina (eigenaar {event.data.get('owner', '?')})", "eigenaar"
        return "Gastpagina", "gast"

    @callback
    def describe_action(event: Event[Any]) -> dict[str, str]:
        device = event.data.get("device", "?")
        action = event.data.get("action", "?")
        name, role = who(event)
        return {
            LOGBOOK_ENTRY_NAME: name,
            LOGBOOK_ENTRY_MESSAGE: f"{role}: {action} op {device}",
        }

    @callback
    def describe_login(event: Event[Any]) -> dict[str, str]:
        name, role = who(event)
        return {LOGBOOK_ENTRY_NAME: name, LOGBOOK_ENTRY_MESSAGE: f"{role} aangemeld"}

    async_describe_event(DOMAIN, EVENT_ACTION, describe_action)
    async_describe_event(DOMAIN, EVENT_LOGIN, describe_login)
