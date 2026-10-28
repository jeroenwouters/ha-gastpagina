"""Logbook entries: guest actions show as "Gastpagina", never as a user."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ACTION, EVENT_LOGIN


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe login and action events."""

    @callback
    def describe_action(event: Event[Any]) -> dict[str, str]:
        device = event.data.get("device", "?")
        action = event.data.get("action", "?")
        return {
            LOGBOOK_ENTRY_NAME: "Gastpagina",
            LOGBOOK_ENTRY_MESSAGE: f"gast: {action} op {device}",
        }

    @callback
    def describe_login(event: Event[Any]) -> dict[str, str]:
        return {LOGBOOK_ENTRY_NAME: "Gastpagina", LOGBOOK_ENTRY_MESSAGE: "gast aangemeld"}

    async_describe_event(DOMAIN, EVENT_ACTION, describe_action)
    async_describe_event(DOMAIN, EVENT_LOGIN, describe_login)
