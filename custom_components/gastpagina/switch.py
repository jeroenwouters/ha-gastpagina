"""switch.gastpagina_toegang: master switch; off refuses every guest request."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import GastEntity
from .runtime import GastConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the master switch."""
    async_add_entities([Access(entry, "toegang", Platform.SWITCH)])


class Access(GastEntity, SwitchEntity, RestoreEntity):
    """Survives restarts: a switched-off page stays off."""

    async def async_added_to_hass(self) -> None:
        """Restore the last position."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_OFF:
            self.runtime.set_access(False)

    @property
    def is_on(self) -> bool:
        """Guests may use the page."""
        return self.runtime.access_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Open the page."""
        self.runtime.set_access(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Close the page; open streams end too."""
        self.runtime.set_access(False)
