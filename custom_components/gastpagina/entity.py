"""Base class for the management entities on the "Gastpagina" device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_UPDATE
from .runtime import GastConfigEntry, GastRuntime


class GastEntity(Entity):
    """Pushes its state whenever the runtime says something changed."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: GastConfigEntry, key: str, platform: str) -> None:
        """Fixed entity ids, whatever the UI language: automations rely on them."""
        self.runtime: GastRuntime = entry.runtime_data
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self.entity_id = f"{platform}.{DOMAIN}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Gastpagina",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Follow runtime updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )
