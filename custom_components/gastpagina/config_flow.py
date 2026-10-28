"""Config flow: which calendar, and how long a code is valid around a booking."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
import voluptuous as vol

from .const import (
    CONF_CALENDAR,
    CONF_HOURS_AFTER,
    CONF_HOURS_BEFORE,
    CONF_MAX_ATTEMPTS,
    CONF_MIN_CODE_LENGTH,
    DEFAULT_CALENDAR,
    DEFAULT_HOURS_AFTER,
    DEFAULT_HOURS_BEFORE,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MIN_CODE_LENGTH,
    DOMAIN,
)


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
        }
    )


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Number selectors return floats; the settings are whole numbers."""
    return {k: v if k == CONF_CALENDAR else int(v) for k, v in user_input.items()}


class GastpaginaConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per Home Assistant (one property)."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the calendar and the validity window."""
        if user_input is not None:
            return self.async_create_entry(title="Gastpagina", data=_clean(user_input))
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Same settings, changeable later."""
        return GastpaginaOptionsFlow()


class GastpaginaOptionsFlow(OptionsFlow):
    """Change calendar or window; the entry reloads."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the current values."""
        if user_input is not None:
            return self.async_create_entry(data=_clean(user_input))
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
