"""Diagnostics: configuration (station urls redacted), counts and recent errors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, FEEDS
from .runtime import GastConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GastConfigEntry
) -> dict[str, Any]:
    """No codes, tokens, guest names, feed links or stream urls."""
    runtime = entry.runtime_data
    config = runtime.controller.config
    issues = sorted(
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    )
    options = {**entry.data, **entry.options}
    for key in FEEDS:  # the platform links carry a secret
        if options.get(key):
            options[key] = REDACTED
    hub = runtime.hub
    return {
        "options": options,
        "feeds": {
            name: {
                "ok": status.ok,
                "last_sync": status.last_sync.isoformat() if status.last_sync else None,
                "failures": status.failures,
                "changes": status.counts,
            }
            for name, status in hub.status.items()
        },
        "stays_ahead": len(runtime.stays),
        "open_requests": runtime.website.open_requests(runtime.stays),
        "website_enabled": runtime.rental_config.enabled,
        "devices": {
            d.id: {"kind": d.kind, "entity_id": d.entity_id, "dimmable": d.dimmable}
            for d in config.devices.values()
        },
        "stations": [
            {"key": s.key, "label": s.label, "url": REDACTED} for s in config.stations.values()
        ],
        "climate_limits": config.public()["climate_limits"],
        "scenes": sorted(config.scenes),
        "access_enabled": runtime.access_enabled,
        "bookings_valid_now": len(runtime.bookings.valid()),
        "calendar_error": runtime.bookings.last_error,
        "sessions": runtime.sessions.count,
        "owner_sessions": runtime.sessions.owner_count,
        "owners": runtime.owners.names(),
        "failed_logins_last_hour": runtime.limiter.failures_last_hour(),
        "issues": issues,
        "recent_errors": list(runtime.errors),
    }
