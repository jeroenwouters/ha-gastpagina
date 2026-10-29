"""Gastpagina: guests control a whitelisted set of devices with their booking code.

No HA account is involved. The integration checks the code against a calendar,
hands out its own session tokens and only ever calls the services that belong
to the devices in gastpagina.yaml.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import (
    ATTR_BOOKING,
    ATTR_CODE,
    ATTR_NAME,
    ATTR_OWNER,
    CALENDAR_REFRESH,
    DOMAIN,
    SERVICE_ADD_OWNER_CODE,
    SERVICE_FILL_CODES,
    SERVICE_GENERATE_CODE,
    SERVICE_LIST_OWNER_CODES,
    SERVICE_RELOAD,
    SERVICE_REMOVE_OWNER_CODE,
    SERVICE_REVOKE_SESSIONS,
)
from .http import async_register_views
from .runtime import GastConfigEntry, GastRuntime

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_REVOKE_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_BOOKING): cv.string, vol.Optional(ATTR_OWNER, default=False): cv.boolean}
)
_ADD_OWNER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_CODE): vol.All(cv.string, vol.Length(max=100)),
    }
)
_REMOVE_OWNER_SCHEMA = vol.Schema({vol.Required(ATTR_NAME): cv.string})
_GENERATE_SCHEMA = vol.Schema({vol.Required(ATTR_BOOKING): cv.string})


def _runtime(hass: HomeAssistant) -> GastRuntime:
    runtime: GastRuntime | None = hass.data.get(DOMAIN)
    if runtime is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return runtime


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services; they work once an entry is loaded."""

    async def reload(call: ServiceCall) -> None:
        await _runtime(hass).async_reload_config()

    async def revoke(call: ServiceCall) -> ServiceResponse:
        return {
            "revoked": _runtime(hass).revoke(call.data.get(ATTR_BOOKING), call.data[ATTR_OWNER])
        }

    async def generate(call: ServiceCall) -> ServiceResponse:
        return {"code": await _runtime(hass).async_generate_code(call.data[ATTR_BOOKING])}

    async def fill(call: ServiceCall) -> ServiceResponse:
        return {"created": await _runtime(hass).async_fill_codes()}

    async def add_owner(call: ServiceCall) -> ServiceResponse:
        return await _runtime(hass).async_add_owner(call.data[ATTR_NAME], call.data.get(ATTR_CODE))

    async def remove_owner(call: ServiceCall) -> None:
        await _runtime(hass).async_remove_owner(call.data[ATTR_NAME])

    async def list_owners(call: ServiceCall) -> ServiceResponse:
        return {"owners": _runtime(hass).owners.listing()}

    hass.services.async_register(DOMAIN, SERVICE_RELOAD, reload)
    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_CODE,
        generate,
        schema=_GENERATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_FILL_CODES, fill, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_OWNER_CODE,
        add_owner,
        schema=_ADD_OWNER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_OWNER_CODE, remove_owner, schema=_REMOVE_OWNER_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_OWNER_CODES, list_owners, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REVOKE_SESSIONS,
        revoke,
        schema=_REVOKE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GastConfigEntry) -> bool:
    """Load sessions and devices, read the calendar, open the API."""
    runtime = GastRuntime(hass, entry)
    await runtime.sessions.async_load()
    await runtime.owners.async_load()
    await runtime.async_reload_config()
    await runtime.bookings.async_refresh()
    entry.runtime_data = runtime
    hass.data[DOMAIN] = runtime

    entry.async_on_unload(async_track_time_interval(hass, runtime.async_tick, CALENDAR_REFRESH))
    entry.async_on_unload(async_at_started(hass, runtime.async_started))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_views(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GastConfigEntry) -> bool:
    """Persist sessions and close the API (views stay registered but refuse)."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = entry.runtime_data
    runtime.shutdown()
    await runtime.sessions.async_save()
    await runtime.owners.async_save()
    hass.data.pop(DOMAIN, None)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: GastConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
