"""gastpagina.yaml: the only entities a guest can ever reach.

Each item is validated on its own, so one bad line raises a repair issue and
the rest keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import SCENE_DEVICE

Kind = Literal["light", "cover", "climate", "radio"]

DOMAINS: dict[Kind, tuple[str, ...]] = {
    "light": ("light", "switch"),
    "cover": ("cover",),
    "climate": ("climate",),
    "radio": ("media_player",),
}

_KEY = vol.All(cv.string, vol.Match(r"^[a-z0-9_]{1,40}$"))
_LABEL = vol.All(cv.string, vol.Length(min=1, max=60))

_LIGHT = vol.Schema(
    {
        vol.Required("key"): _KEY,
        vol.Required("label"): _LABEL,
        vol.Required("entity"): cv.entity_id,
        vol.Optional("dimmable", default=False): cv.boolean,
    }
)
_ENTITY = vol.Schema(
    {vol.Required("key"): _KEY, vol.Required("label"): _LABEL, vol.Required("entity"): cv.entity_id}
)
_STATION = vol.Schema(
    {vol.Required("key"): _KEY, vol.Required("label"): _LABEL, vol.Required("url"): cv.url}
)
_RADIO = vol.Schema(
    {
        vol.Required("key"): _KEY,
        vol.Required("label"): _LABEL,
        vol.Required("entity"): cv.entity_id,
        vol.Optional("max_volume", default=1.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=1)
        ),
        vol.Optional("content_type", default="music"): cv.string,
        vol.Optional("stations", default=list): [dict],
    }
)
_LIMITS = vol.Schema(
    {
        vol.Optional("min", default=16.0): vol.Coerce(float),
        vol.Optional("max", default=22.0): vol.Coerce(float),
        vol.Optional("step", default=0.5): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=5)),
    }
)
_SCENE = vol.Schema(
    {
        vol.Optional("lights_off", default=False): cv.boolean,
        vol.Optional("covers_close", default=False): cv.boolean,
        vol.Optional("climate_night", default=None): vol.Any(None, vol.Coerce(float)),
        vol.Optional("radio_stop", default=False): cv.boolean,
    }
)
_ROOM = vol.Schema(
    {
        vol.Required("key"): _KEY,
        vol.Required("name"): _LABEL,
        vol.Optional("lights", default=list): [dict],
        vol.Optional("covers", default=list): [dict],
        vol.Optional("climate"): dict,
    }
)
_FILE = vol.Schema(
    {
        vol.Optional("rooms", default=list): [dict],
        vol.Optional("radio"): dict,
        vol.Optional("climate_limits", default=dict): dict,
        vol.Optional("scenes", default=dict): {cv.string: vol.Any(dict, None)},
    }
)


@dataclass(frozen=True, slots=True)
class Problem:
    """Something wrong in the file, shown as a repair issue."""

    issue_id: str
    translation_key: str
    placeholders: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Device:
    """A guest-facing device, addressed by its neutral id (`room.key`)."""

    id: str
    key: str
    kind: Kind
    label: str
    entity_id: str
    dimmable: bool = False

    @property
    def domain(self) -> str:
        """Entity domain: decides which services are called."""
        return self.entity_id.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class Room:
    """A room and the ids of its devices, in file order."""

    key: str
    name: str
    lights: tuple[str, ...]
    covers: tuple[str, ...]
    climate: str | None


@dataclass(frozen=True, slots=True)
class Station:
    """A radio station; the url never leaves the server."""

    key: str
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class Limits:
    """Thermostat bounds."""

    min: float = 16.0
    max: float = 22.0
    step: float = 0.5


@dataclass(frozen=True, slots=True)
class Scene:
    """A server-side scene."""

    key: str
    lights_off: bool
    covers_close: bool
    climate_night: float | None
    radio_stop: bool


@dataclass(slots=True)
class DeviceConfig:
    """Parsed gastpagina.yaml."""

    rooms: list[Room] = field(default_factory=list)
    devices: dict[str, Device] = field(default_factory=dict)
    radio: str | None = None
    stations: dict[str, Station] = field(default_factory=dict)
    max_volume: float = 1.0
    content_type: str = "music"
    limits: Limits = field(default_factory=Limits)
    scenes: dict[str, Scene] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    def by_entity(self) -> dict[str, list[str]]:
        """Device ids per entity id."""
        result: dict[str, list[str]] = {}
        for device in self.devices.values():
            result.setdefault(device.entity_id, []).append(device.id)
        return result

    def public(self) -> dict[str, Any]:
        """What the page may know: keys and labels, no entity ids or urls."""

        def light(device_id: str) -> dict[str, Any]:
            d = self.devices[device_id]
            return {"id": d.id, "key": d.key, "label": d.label, "dimmable": d.dimmable}

        def plain(device_id: str) -> dict[str, Any]:
            d = self.devices[device_id]
            return {"id": d.id, "key": d.key, "label": d.label}

        radio = None
        if self.radio is not None:
            radio = {
                **plain(self.radio),
                "max_volume": self.max_volume,
                "stations": [{"key": s.key, "label": s.label} for s in self.stations.values()],
            }
        return {
            "rooms": [
                {
                    "key": room.key,
                    "name": room.name,
                    "lights": [light(i) for i in room.lights],
                    "covers": [plain(i) for i in room.covers],
                    "climate": plain(room.climate) if room.climate else None,
                }
                for room in self.rooms
            ],
            "radio": radio,
            "climate_limits": {
                "min": self.limits.min,
                "max": self.limits.max,
                "step": self.limits.step,
            },
            "scenes": {
                s.key: {
                    "lights_off": s.lights_off,
                    "covers_close": s.covers_close,
                    "climate_night": s.climate_night,
                    "radio_stop": s.radio_stop,
                }
                for s in self.scenes.values()
            },
        }


def _error(err: vol.Invalid) -> str:
    return str(err)[:200]


class _Parser:
    def __init__(self) -> None:
        self.config = DeviceConfig()

    def problem(self, issue_id: str, translation_key: str, **placeholders: str) -> None:
        self.config.problems.append(Problem(issue_id, translation_key, placeholders))

    def device(
        self, raw: Any, kind: Kind, schema: vol.Schema, prefix: str, where: str
    ) -> Device | None:
        try:
            item = schema(raw)
        except vol.Invalid as err:
            name = raw.get("key", "?") if isinstance(raw, dict) else "?"
            self.problem(
                f"device_invalid_{where}",
                "device_invalid",
                device=f"{prefix}{name}",
                error=_error(err),
            )
            return None
        device_id = f"{prefix}{item['key']}"
        entity_id: str = item["entity"]
        domain = entity_id.split(".", 1)[0]
        if domain not in DOMAINS[kind]:
            self.problem(
                f"wrong_domain_{device_id}",
                "wrong_domain",
                device=device_id,
                entity=entity_id,
                expected=" / ".join(DOMAINS[kind]),
            )
            return None
        if device_id in self.config.devices or device_id == SCENE_DEVICE:
            self.problem(f"duplicate_key_{where}", "duplicate_key", device=device_id)
            return None
        device = Device(
            id=device_id,
            key=item["key"],
            kind=kind,
            label=item["label"],
            entity_id=entity_id,
            dimmable=bool(item.get("dimmable")) and domain == "light",
        )
        self.config.devices[device_id] = device
        return device

    def room(self, raw: Any, index: int) -> None:
        try:
            room = _ROOM(raw)
        except vol.Invalid as err:
            self.problem(
                f"room_invalid_{index}", "room_invalid", room=str(index + 1), error=_error(err)
            )
            return
        if any(r.key == room["key"] for r in self.config.rooms):
            self.problem(f"duplicate_room_{room['key']}", "duplicate_key", device=room["key"])
            return
        prefix = f"{room['key']}."
        lights = [
            self.device(item, "light", _LIGHT, prefix, f"{room['key']}_light_{n}")
            for n, item in enumerate(room["lights"])
        ]
        covers = [
            self.device(item, "cover", _ENTITY, prefix, f"{room['key']}_cover_{n}")
            for n, item in enumerate(room["covers"])
        ]
        climate = None
        if "climate" in room:
            climate = self.device(
                room["climate"], "climate", _ENTITY, prefix, f"{room['key']}_climate"
            )
        self.config.rooms.append(
            Room(
                key=room["key"],
                name=room["name"],
                lights=tuple(d.id for d in lights if d),
                covers=tuple(d.id for d in covers if d),
                climate=climate.id if climate else None,
            )
        )

    def radio(self, raw: Any) -> None:
        try:
            radio = _RADIO(raw)
        except vol.Invalid as err:
            self.problem("radio_invalid", "device_invalid", device="radio", error=_error(err))
            return
        device = self.device(
            {k: radio[k] for k in ("key", "label", "entity")}, "radio", _ENTITY, "", "radio"
        )
        if device is None:
            return
        self.config.radio = device.id
        self.config.max_volume = radio["max_volume"]
        self.config.content_type = radio["content_type"]
        for n, item in enumerate(radio["stations"]):
            try:
                station = _STATION(item)
            except vol.Invalid as err:
                self.problem(
                    f"station_invalid_{n}",
                    "device_invalid",
                    device=f"radio/{n + 1}",
                    error=_error(err),
                )
                continue
            if station["key"] in self.config.stations:
                self.problem(f"duplicate_station_{n}", "duplicate_key", device=station["key"])
                continue
            self.config.stations[station["key"]] = Station(
                station["key"], station["label"], station["url"]
            )

    def limits(self, raw: Any) -> None:
        try:
            limits = _LIMITS(raw)
        except vol.Invalid as err:
            self.problem("limits_invalid", "limits_invalid", error=_error(err))
            return
        if limits["min"] > limits["max"]:
            self.problem("limits_invalid", "limits_invalid", error="min > max")
            return
        self.config.limits = Limits(limits["min"], limits["max"], limits["step"])

    def scene(self, key: str, raw: Any) -> None:
        try:
            _KEY(key)
            scene = _SCENE(raw or {})
        except vol.Invalid as err:
            self.problem(f"scene_invalid_{key}"[:60], "scene_invalid", scene=key, error=_error(err))
            return
        self.config.scenes[key] = Scene(key=key, **scene)


def parse_config(raw: Any) -> DeviceConfig:
    """Parse the file contents, collecting problems instead of failing."""
    parser = _Parser()
    try:
        data = _FILE({} if raw is None else raw)
    except vol.Invalid as err:
        parser.problem("config_invalid", "config_invalid", error=_error(err))
        return parser.config
    for index, room in enumerate(data["rooms"]):
        parser.room(room, index)
    if "radio" in data:
        parser.radio(data["radio"])
    parser.limits(data["climate_limits"])
    for key, scene in data["scenes"].items():
        parser.scene(key, scene)
    return parser.config
