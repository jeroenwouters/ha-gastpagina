"""Config flow: which calendar, and how long a code is valid around a booking."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
import voluptuous as vol

from .const import (
    CONF_AUTO_CODES,
    CONF_CALENDAR,
    CONF_CHECK_IN,
    CONF_CHECK_OUT,
    CONF_CODE_PREFIX,
    CONF_FEED_AIRBNB,
    CONF_FEED_BOOKING,
    CONF_HOURS_AFTER,
    CONF_HOURS_BEFORE,
    CONF_LOOKAHEAD_DAYS,
    CONF_MAX_ATTEMPTS,
    CONF_MIN_CODE_LENGTH,
    CONF_NOTIFY_NEW_CODE,
    CONF_OPTION_HOURS,
    CONF_SYNC_MINUTES,
    DEFAULT_AUTO_CODES,
    DEFAULT_CALENDAR,
    DEFAULT_CHECK_IN,
    DEFAULT_CHECK_OUT,
    DEFAULT_CODE_PREFIX,
    DEFAULT_HOURS_AFTER,
    DEFAULT_HOURS_BEFORE,
    DEFAULT_LOOKAHEAD_DAYS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MIN_CODE_LENGTH,
    DEFAULT_NOTIFY_NEW_CODE,
    DEFAULT_OPTION_HOURS,
    DEFAULT_SYNC_MINUTES,
    DOMAIN,
)

_PREFIX = re.compile(r"[A-Za-z0-9]{0,6}")
_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_TEXT_KEYS = (CONF_CALENDAR, CONF_CHECK_IN, CONF_CHECK_OUT)
_FEED_KEYS = (CONF_FEED_AIRBNB, CONF_FEED_BOOKING)


def _number(minimum: int, maximum: int, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_CALENDAR, default=current.get(CONF_CALENDAR, DEFAULT_CALENDAR)
            ): EntitySelector(EntitySelectorConfig(domain="calendar")),
            vol.Required(
                CONF_HOURS_BEFORE, default=current.get(CONF_HOURS_BEFORE, DEFAULT_HOURS_BEFORE)
            ): _number(0, 168, "h"),
            vol.Required(
                CONF_HOURS_AFTER, default=current.get(CONF_HOURS_AFTER, DEFAULT_HOURS_AFTER)
            ): _number(0, 48, "h"),
            vol.Required(
                CONF_MIN_CODE_LENGTH,
                default=current.get(CONF_MIN_CODE_LENGTH, DEFAULT_MIN_CODE_LENGTH),
            ): _number(6, 64),
            vol.Required(
                CONF_MAX_ATTEMPTS, default=current.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS)
            ): _number(1, 50),
            vol.Required(
                CONF_AUTO_CODES, default=current.get(CONF_AUTO_CODES, DEFAULT_AUTO_CODES)
            ): BooleanSelector(),
            vol.Optional(
                CONF_CODE_PREFIX, default=current.get(CONF_CODE_PREFIX, DEFAULT_CODE_PREFIX)
            ): TextSelector(),
            vol.Required(
                CONF_NOTIFY_NEW_CODE,
                default=current.get(CONF_NOTIFY_NEW_CODE, DEFAULT_NOTIFY_NEW_CODE),
            ): BooleanSelector(),
            vol.Required(
                CONF_LOOKAHEAD_DAYS,
                default=current.get(CONF_LOOKAHEAD_DAYS, DEFAULT_LOOKAHEAD_DAYS),
            ): _number(1, 365, "d"),
            vol.Optional(
                CONF_FEED_AIRBNB,
                description={"suggested_value": current.get(CONF_FEED_AIRBNB, "")},
            ): _URL,
            vol.Optional(
                CONF_FEED_BOOKING,
                description={"suggested_value": current.get(CONF_FEED_BOOKING, "")},
            ): _URL,
            vol.Required(
                CONF_CHECK_IN, default=current.get(CONF_CHECK_IN, DEFAULT_CHECK_IN)
            ): TimeSelector(),
            vol.Required(
                CONF_CHECK_OUT, default=current.get(CONF_CHECK_OUT, DEFAULT_CHECK_OUT)
            ): TimeSelector(),
            vol.Required(
                CONF_SYNC_MINUTES, default=current.get(CONF_SYNC_MINUTES, DEFAULT_SYNC_MINUTES)
            ): _number(5, 240, "min"),
            vol.Required(
                CONF_OPTION_HOURS, default=current.get(CONF_OPTION_HOURS, DEFAULT_OPTION_HOURS)
            ): _number(1, 336, "h"),
        }
    )


def _clean(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Whole numbers from number selectors, an upper-case prefix; plus form errors."""
    cleaned: dict[str, Any] = {}
    for key, value in user_input.items():
        if key in _TEXT_KEYS or isinstance(value, bool):
            cleaned[key] = value
        elif key in _FEED_KEYS:
            cleaned[key] = str(value or "").strip()
        elif key == CONF_CODE_PREFIX:
            cleaned[key] = str(value).strip().upper()
        else:
            cleaned[key] = int(value)
    errors = {}
    if not _PREFIX.fullmatch(cleaned.get(CONF_CODE_PREFIX, "")):
        errors[CONF_CODE_PREFIX] = "invalid_prefix"
    for key in _FEED_KEYS:
        if (url := cleaned.get(key)) and not url.startswith("https://"):
            errors[key] = "invalid_feed"
    return cleaned, errors


class GastpaginaConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per Home Assistant (one property)."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the calendar and the validity window."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _clean(user_input)
            if not errors:
                return self.async_create_entry(title="Gastpagina", data=data)
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Same settings, changeable later."""
        return GastpaginaOptionsFlow()


class GastpaginaOptionsFlow(OptionsFlow):
    """Change calendar or window; the entry reloads."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the current values."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _clean(user_input)
            if not errors:
                return self.async_create_entry(data=data)
        current = user_input or {**self.config_entry.data, **self.config_entry.options}
        runtime = getattr(self.config_entry, "runtime_data", None)
        names = runtime.owners.names() if runtime is not None else []
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(current),
            errors=errors,
            description_placeholders={"owners": ", ".join(names) or "geen"},
        )
