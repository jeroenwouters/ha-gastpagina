"""Device states for the page and the whitelist of actions guests may run."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import logging
import math
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import SCENE_DEVICE
from .devices import Device, DeviceConfig, Scene

_LOGGER = logging.getLogger(__name__)

_LIGHT_SERVICES = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}
_COVER_SERVICES = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}
_MISSING: Any = object()


class ActionError(Exception):
    """An action that was refused or failed, with its HTTP status."""

    def __init__(self, status: int, error: str) -> None:
        """Keep status and error code for the response."""
        super().__init__(error)
        self.status = status
        self.error = error


def _unavailable(state: State | None) -> bool:
    return state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return value


def _number(value: Any) -> float:
    number = _num(value)
    if number is None:
        raise ActionError(400, "waarde")
    return float(number)


def _no_value(value: Any) -> None:
    if value is not _MISSING and value is not None:
        raise ActionError(400, "waarde")


class Controller:
    """Turns device keys and actions into service calls, and states into JSON."""

    def __init__(self, hass: HomeAssistant, config: DeviceConfig, errors: deque[str]) -> None:
        """Start with a parsed config."""
        self.hass = hass
        self.config = config
        self.errors = errors
        self._last_station: str | None = None

    # --- state -------------------------------------------------------------

    def states(self) -> dict[str, dict[str, Any]]:
        """State of every configured device."""
        return {d.id: self.state_of(d) for d in self.config.devices.values()}

    def state_of(self, device: Device) -> dict[str, Any]:
        """The page's state model for one device."""
        state = self.hass.states.get(device.entity_id)
        available = not _unavailable(state)
        current = state.state if state is not None and available else None
        attrs = state.attributes if state is not None and available else {}

        if device.kind == "light":
            on = current == "on"
            if device.domain == "switch":
                brightness = 255
            else:
                raw = _num(attrs.get("brightness"))
                brightness = int(raw) if raw is not None else (255 if on else 0)
            return {
                "available": available,
                "state": "on" if on else "off",
                "brightness": max(0, min(255, brightness)),
            }

        if device.kind == "cover":
            moving = {"opening": "open", "closing": "close"}.get(current or "")
            position = _num(attrs.get("current_position"))
            if position is None:
                position = 0 if current == "closed" else 100
            closed = current == "closed" if current in ("open", "closed") else position <= 0
            return {
                "available": available,
                "position": round(position),
                "moving": moving,
                "state": "moving" if moving else ("closed" if closed else "open"),
            }

        if device.kind == "climate":
            return {
                "available": available,
                "state": "off" if current in (None, "off") else "heat",
                "current": _num(attrs.get("current_temperature")),
                "target": _num(attrs.get("temperature")),
                "action": "heating" if attrs.get("hvac_action") == "heating" else "idle",
            }

        playing = current == "playing"
        station = None
        if playing:
            content = attrs.get("media_content_id")
            station = next(
                (s.key for s in self.config.stations.values() if s.url == content),
                self._last_station,
            )
        volume = _num(attrs.get("volume_level")) or 0.0
        return {
            "available": available,
            "state": "playing" if playing else "idle",
            "volume": round(volume, 2),
            "station": station,
        }

    # --- actions -----------------------------------------------------------

    def clamp_target(self, value: float) -> float:
        """Round to the step, then keep within min and max."""
        limits = self.config.limits
        rounded = round(value / limits.step) * limits.step
        return round(min(limits.max, max(limits.min, rounded)), 2)

    def _plan(self, device: Device, action: str, value: Any) -> tuple[str, str, dict[str, Any]]:
        if device.kind == "light":
            if action in _LIGHT_SERVICES:
                _no_value(value)
                return device.domain, _LIGHT_SERVICES[action], {}
            if action == "brightness" and device.dimmable:
                pct = _number(value)
                if pct != int(pct) or not 1 <= pct <= 100:
                    raise ActionError(400, "waarde")
                return "light", "turn_on", {"brightness_pct": int(pct)}
        elif device.kind == "cover":
            if action in _COVER_SERVICES:
                _no_value(value)
                return "cover", _COVER_SERVICES[action], {}
        elif device.kind == "climate":
            if action == "target":
                return (
                    "climate",
                    "set_temperature",
                    {"temperature": self.clamp_target(_number(value))},
                )
            if action == "power":
                if value not in ("on", "off"):
                    raise ActionError(400, "waarde")
                return "climate", "set_hvac_mode", {"hvac_mode": "heat" if value == "on" else "off"}
        elif device.kind == "radio":
            if action == "play":
                station = self.config.stations.get(value) if isinstance(value, str) else None
                if station is None:
                    raise ActionError(400, "waarde")
                return (
                    "media_player",
                    "play_media",
                    {
                        "media_content_id": station.url,
                        "media_content_type": self.config.content_type,
                    },
                )
            if action == "stop":
                _no_value(value)
                return "media_player", "media_stop", {}
            if action == "volume":
                volume = _number(value)
                if not 0 <= volume <= 1:
                    raise ActionError(400, "waarde")
                return (
                    "media_player",
                    "volume_set",
                    {"volume_level": round(min(volume, self.config.max_volume), 2)},
                )
        raise ActionError(400, "actie")

    async def async_act(
        self,
        device_id: str,
        action: str,
        value: Any,
        context: Context,
        announce: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        """Run one whitelisted action. Raises ActionError.

        `announce` runs once the action is known to be valid, before any service call.
        """
        if device_id == SCENE_DEVICE:
            scene = self.config.scenes.get(action)
            if scene is None:
                raise ActionError(400, "actie")
            _no_value(value)
            announce()
            return await self._async_scene(scene, context)

        device = self.config.devices.get(device_id)
        if device is None:
            raise ActionError(400, "toestel")
        domain, service, data = self._plan(device, action, value)
        if _unavailable(self.hass.states.get(device.entity_id)):
            raise ActionError(409, "niet_beschikbaar")
        announce()
        await self._async_call(domain, service, {ATTR_ENTITY_ID: device.entity_id, **data}, context)
        if device.kind == "radio" and action in ("play", "stop"):
            self._last_station = value if action == "play" else None
        return {}

    async def _async_scene(self, scene: Scene, context: Context) -> dict[str, Any]:
        previous: dict[str, dict[str, Any]] = {}
        calls: list[tuple[str, str, dict[str, Any]]] = []
        for device in self.config.devices.values():
            state = self.hass.states.get(device.entity_id)
            if state is None or _unavailable(state):
                continue
            target = {ATTR_ENTITY_ID: device.entity_id}
            call: tuple[str, str, dict[str, Any]] | None = None
            if device.kind == "light" and scene.lights_off and state.state == "on":
                call = (device.domain, "turn_off", target)
            elif device.kind == "cover" and scene.covers_close and state.state != "closed":
                call = ("cover", "close_cover", target)
            elif (
                device.kind == "climate"
                and scene.climate_night is not None
                and state.state != "off"
            ):
                temperature = self.clamp_target(scene.climate_night)
                call = ("climate", "set_temperature", {**target, "temperature": temperature})
            elif device.kind == "radio" and scene.radio_stop and state.state == "playing":
                call = ("media_player", "media_stop", target)
            if call is not None:
                previous[device.id] = self.state_of(device)
                calls.append(call)
        results = await asyncio.gather(
            *(self._async_call(*call, context) for call in calls), return_exceptions=True
        )
        failed = sum(isinstance(r, Exception) for r in results)
        if any(c[0] == "media_player" for c in calls):
            self._last_station = None
        return {"previous": previous, "failed": failed}

    async def _async_call(
        self, domain: str, service: str, data: dict[str, Any], context: Context
    ) -> None:
        try:
            await self.hass.services.async_call(
                domain, service, data, blocking=True, context=context
            )
        except (HomeAssistantError, vol.Invalid) as err:
            message = f"{domain}.{service} on {data[ATTR_ENTITY_ID]} failed: {err}"
            _LOGGER.warning("Gastpagina: %s", message)
            self.errors.append(f"{dt_util.now().isoformat(timespec='seconds')} {message}")
            raise ActionError(502, "mislukt") from err
