"""Shared fixtures: a fake booking calendar, a small device file and recorded service calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.gastpagina.const import (
    CONF_CALENDAR,
    CONF_HOURS_AFTER,
    CONF_HOURS_BEFORE,
    CONF_MAX_ATTEMPTS,
    CONF_MIN_CODE_LENGTH,
    CONFIG_FILE,
    DOMAIN,
    TOKEN_HEADER,
)

TZ = ZoneInfo("Europe/Brussels")
CHECK_IN = datetime(2026, 11, 3, 16, 0, tzinfo=TZ)
CHECK_OUT = datetime(2026, 11, 7, 10, 0, tzinfo=TZ)
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
CODE = "ZEE-MONSTER 2026"
TITLE = "Familie Peeters"
STATION_URL = "http://icecast.vrtcdn.be/mnm-high.mp3"

DEVICES_YAML = f"""
rooms:
  - key: living
    name: Living
    lights:
      - {{ key: plafond, label: Plafond, entity: light.living_plafond, dimmable: true }}
      - {{ key: bollen, label: Bollen, entity: switch.living_bollen }}
    covers:
      - {{ key: zeekant, label: Zeekant, entity: cover.living_zeekant }}
    climate: {{ key: warmte, label: Warmte living, entity: climate.living }}
radio:
  key: radio
  label: Radio living
  entity: media_player.radio
  max_volume: 0.6
  content_type: music
  stations:
    - {{ key: mnm, label: MNM, url: "{STATION_URL}" }}
climate_limits: {{ min: 16, max: 22, step: 0.5 }}
scenes:
  slaapwel: {{ lights_off: true, covers_close: true, climate_night: 17, radio_stop: true }}
  alles_uit: {{ lights_off: true, covers_close: false, climate_night: null, radio_stop: true }}
"""

ENTITY_IDS = (
    "light.living_plafond",
    "switch.living_bollen",
    "cover.living_zeekant",
    "climate.living",
    "media_player.radio",
)

SERVICES = {
    "light": ("turn_on", "turn_off", "toggle"),
    "switch": ("turn_on", "turn_off", "toggle"),
    "cover": ("open_cover", "close_cover", "stop_cover"),
    "climate": ("set_temperature", "set_hvac_mode"),
    "media_player": ("play_media", "media_stop", "volume_set"),
}


class FakeCalendar(CalendarEntity):
    """A calendar whose events the test controls."""

    _attr_name = "Gasten"

    def __init__(self, writable: bool = True) -> None:
        """Start empty; writable like Local Calendar unless told otherwise."""
        self.entity_id = "calendar.gasten"
        self.events: list[CalendarEvent] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.fail_updates = 0
        self.created = 0
        if writable:
            self._attr_supported_features = (
                CalendarEntityFeature.UPDATE_EVENT
                | CalendarEntityFeature.CREATE_EVENT
                | CalendarEntityFeature.DELETE_EVENT
            )

    @property
    def event(self) -> CalendarEvent | None:
        """No 'current event' needed for these tests."""
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the events overlapping the range."""
        return [
            e
            for e in self.events
            if e.end_datetime_local > start_date and e.start_datetime_local < end_date
        ]

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Replace an event the way Local Calendar does: by uid, with the given fields."""
        if self.fail_updates:
            self.fail_updates -= 1
            raise HomeAssistantError("disk full")
        self.updates.append((uid, dict(event)))
        index = next(i for i, e in enumerate(self.events) if e.uid == uid)
        self.events[index] = CalendarEvent(
            start=event["dtstart"],
            end=event["dtend"],
            summary=event["summary"],
            description=event.get("description"),
            location=event.get("location"),
            uid=uid,
        )

    async def async_create_event(self, **kwargs: Any) -> None:
        """Add an event with a fresh uid, as Local Calendar does."""
        self.created += 1
        self.events.append(
            CalendarEvent(
                start=kwargs["dtstart"],
                end=kwargs["dtend"],
                summary=kwargs["summary"],
                description=kwargs.get("description"),
                uid=f"nieuw-{self.created}",
            )
        )

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Remove an event by uid."""
        self.events = [e for e in self.events if e.uid != uid]

    def add(
        self,
        *,
        uid: str = "booking-1",
        summary: str = TITLE,
        start: datetime | date = CHECK_IN,
        end: datetime | date = CHECK_OUT,
        code: str | None = CODE,
        description: str = "Twee volwassenen, een kind",
        location: str | None = None,
        rrule: str | None = None,
    ) -> CalendarEvent:
        """Add a booking with the code on its own line in the description."""
        if code is not None:
            description += f"\ncode: {code}\n"
        event = CalendarEvent(
            start=start,
            end=end,
            summary=summary,
            uid=uid,
            description=description,
            location=location,
            rrule=rrule,
        )
        self.events.append(event)
        return event

    def get(self, uid: str) -> CalendarEvent:
        """The event with this uid."""
        return next(e for e in self.events if e.uid == uid)


def set_device_states(hass: HomeAssistant) -> None:
    """Give every configured device a plausible state."""
    hass.states.async_set("light.living_plafond", "on", {"brightness": 128})
    hass.states.async_set("switch.living_bollen", "off")
    hass.states.async_set("cover.living_zeekant", "open", {"current_position": 100})
    hass.states.async_set(
        "climate.living",
        "heat",
        {"current_temperature": 19.5, "temperature": 20, "hvac_action": "idle"},
    )
    hass.states.async_set("media_player.radio", "idle", {"volume_level": 0.2})


@dataclass
class Harness:
    """The integration under test plus helpers to talk to it."""

    hass: HomeAssistant
    entry: MockConfigEntry
    calendar: FakeCalendar
    client: TestClient
    calls: list[ServiceCall]
    freezer: FrozenDateTimeFactory

    async def login(self, code: str = CODE) -> tuple[int, dict[str, Any]]:
        """POST the code, return status and body."""
        resp = await self.client.post("/api/gastpagina/login", json={"code": code})
        return resp.status, await resp.json()

    async def token(self) -> str:
        """Log in with the valid code and return the token."""
        status, body = await self.login()
        assert status == 200, body
        return body["token"]

    async def get(self, path: str, token: str | None = None) -> ClientResponse:
        """GET an API path, optionally with a session token."""
        headers = {TOKEN_HEADER: token} if token else {}
        return await self.client.get(f"/api/gastpagina/{path}", headers=headers)

    async def act(
        self, token: str | None, device: str, action: str, **value: Any
    ) -> tuple[int, dict[str, Any]]:
        """POST an action; pass value=... to include a value."""
        body: dict[str, Any] = {"device": device, "action": action, **value}
        headers = {TOKEN_HEADER: token} if token else {}
        resp = await self.client.post("/api/gastpagina/action", json=body, headers=headers)
        return resp.status, await resp.json()

    def calls_for(self, domain: str, service: str) -> list[ServiceCall]:
        """Recorded calls of one service."""
        return [c for c in self.calls if c.domain == domain and c.service == service]

    async def tick(self, delta: timedelta) -> None:
        """Advance time and let timers fire."""
        self.freezer.tick(delta)
        async_fire_time_changed(self.hass)
        # The minute timer runs as a background task; wait for it (scrypt runs in the executor).
        await self.hass.async_block_till_done(wait_background_tasks=True)

    async def move_to(self, moment: datetime) -> None:
        """Jump to a moment and let timers fire."""
        self.freezer.move_to(moment)
        async_fire_time_changed(self.hass)
        # The minute timer runs as a background task; wait for it (scrypt runs in the executor).
        await self.hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def http_config() -> dict[str, Any]:
    """HTTP component config; parametrize to change it."""
    return {}


@pytest.fixture
def devices_yaml() -> str:
    """Contents of gastpagina.yaml; parametrize to change it."""
    return DEVICES_YAML


@pytest.fixture
def calendar_writable() -> bool:
    """Whether the bookings calendar accepts updates; parametrize to change it."""
    return True


@pytest.fixture
def entry_data() -> dict[str, Any]:
    """Config entry data; parametrize to change it."""
    return {
        CONF_CALENDAR: "calendar.gasten",
        CONF_HOURS_BEFORE: 24,
        CONF_HOURS_AFTER: 0,
        CONF_MIN_CODE_LENGTH: 10,
        CONF_MAX_ATTEMPTS: 5,
    }


@pytest.fixture
async def harness(  # noqa: PLR0917 (pytest fixtures)
    hass: HomeAssistant,
    hass_client_no_auth: Any,
    freezer: FrozenDateTimeFactory,
    tmp_path: Any,
    http_config: dict[str, Any],
    devices_yaml: str,
    entry_data: dict[str, Any],
    calendar_writable: bool,
) -> Harness:
    """Set up the integration with one booking, mid-stay."""
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Brussels")
    hass.config.config_dir = str(tmp_path)
    (tmp_path / CONFIG_FILE).write_text(devices_yaml)
    set_device_states(hass)

    assert await async_setup_component(hass, "http", {"http": http_config})
    assert await async_setup_component(hass, "calendar", {})
    calendar = FakeCalendar(writable=calendar_writable)
    calendar.add()
    await hass.data[DATA_COMPONENT].async_add_entities([calendar])

    entry = MockConfigEntry(domain=DOMAIN, title="Gastpagina", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Registered after setup so they replace the real switch services the
    # integration's own switch platform brought in.
    calls: list[ServiceCall] = []

    @callback
    def record(call: ServiceCall) -> None:
        calls.append(call)

    for domain, services in SERVICES.items():
        for service in services:
            hass.services.async_register(domain, service, record)

    client = await hass_client_no_auth()
    return Harness(hass, entry, calendar, client, calls, freezer)


async def read_event(resp: ClientResponse) -> tuple[str | None, Any]:
    """Read one server-sent event, skipping keepalive comments."""
    name: str | None = None
    data: list[str] = []
    while True:
        raw = await resp.content.readline()
        if not raw:
            raise EOFError
        line = raw.decode().rstrip("\n")
        if line.startswith(":"):
            continue
        if line == "":
            if data or name:
                return name, json.loads("\n".join(data)) if data else None
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)


async def read_event_within(resp: ClientResponse, seconds: float) -> tuple[str | None, Any]:
    """read_event with a timeout."""
    return await asyncio.wait_for(read_event(resp), seconds)
