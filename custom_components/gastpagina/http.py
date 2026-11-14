"""HTTP API under /api/gastpagina/, without HA authentication.

The views never raise HTTPUnauthorized: HA's ban middleware counts that as a
failed HA login. A refused guest gets a plain JSON 401 instead.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
import logging
from typing import Any, Final

from aiohttp import web
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView
import voluptuous as vol

from .const import (
    DOMAIN,
    SIGNAL_CONFIG,
    SIGNAL_RECHECK,
    STREAM_KEEPALIVE,
    TICKET_TTL,
    TOKEN_HEADER,
)
from .controller import ActionError
from .http_site import PUBLIC_VIEWS
from .runtime import GastRuntime
from .sessions import Session

_LOGGER = logging.getLogger(__name__)

_VIEWS_REGISTERED: Final = f"{DOMAIN}_views"
_NO_STORE: Final = {"Cache-Control": "no-store"}
_MAX_BODY: Final = 2048

_LOGIN_SCHEMA = vol.Schema({vol.Required("code"): vol.All(str, vol.Length(min=1, max=100))})
_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("device"): vol.All(str, vol.Length(min=1, max=100)),
        vol.Required("action"): vol.All(str, vol.Length(min=1, max=40)),
        vol.Optional("value"): vol.Any(None, bool, int, float, vol.All(str, vol.Length(max=100))),
    }
)

_CLOSE: Final = object()
_RECHECK: Final = object()
_CONFIG: Final = object()


@callback
def async_register_views(hass: HomeAssistant) -> None:
    """Register once per HA run; views cannot be removed again."""
    if hass.data.get(_VIEWS_REGISTERED):
        return
    hass.data[_VIEWS_REGISTERED] = True
    for view in (
        LoginView,
        LogoutView,
        ConfigView,
        StateView,
        StreamTicketView,
        StreamView,
        ActionView,
        *PUBLIC_VIEWS,
    ):
        hass.http.register_view(view())


class _GastView(HomeAssistantView):
    requires_auth = False

    def _reply(
        self, body: Any, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None
    ) -> web.Response:
        return self.json(body, status, {**_NO_STORE, **(headers or {})})

    def _runtime(self, request: web.Request) -> GastRuntime | None:
        runtime: GastRuntime | None = request.app[KEY_HASS].data.get(DOMAIN)
        return runtime if runtime is not None and not runtime.closed else None

    async def _guard(self, request: web.Request) -> tuple[GastRuntime, Session] | web.Response:
        """Runtime and session, or the response that refuses the request.

        With the access switch off only owner sessions get through.
        """
        runtime = self._runtime(request)
        if runtime is None:
            return self._reply({"error": "uit"}, HTTPStatus.SERVICE_UNAVAILABLE)
        session = await runtime.async_authenticate(request.headers.get(TOKEN_HEADER))
        if session is not None and runtime.allowed(session):
            return runtime, session
        if not runtime.access_enabled:
            return self._reply({"error": "uit"}, HTTPStatus.SERVICE_UNAVAILABLE)
        return self._reply({"error": "sessie"}, HTTPStatus.UNAUTHORIZED)


async def _body(request: web.Request, schema: vol.Schema) -> dict[str, Any] | None:
    raw = await request.content.read(_MAX_BODY + 1)
    if len(raw) > _MAX_BODY:
        return None
    try:
        validated: dict[str, Any] = schema(json.loads(raw))
    except (ValueError, vol.Invalid):
        return None
    return validated


class LoginView(_GastView):
    """Exchange a booking code for a session token."""

    url = "/api/gastpagina/login"
    name = "api:gastpagina:login"

    async def post(self, request: web.Request) -> web.Response:
        """Check the code; with the access switch off only owner codes work."""
        runtime = self._runtime(request)
        if runtime is None:
            return self._reply({"error": "uit"}, HTTPStatus.SERVICE_UNAVAILABLE)
        body = await _body(request, _LOGIN_SCHEMA)
        if body is None:
            return self._reply({"error": "ongeldig"}, HTTPStatus.BAD_REQUEST)
        status, result = await runtime.async_login(body["code"], request.remote)
        headers = {"Retry-After": str(result["retry_after"])} if status == 429 else None
        return self._reply(result, status, headers)


class LogoutView(_GastView):
    """End the session of this device."""

    url = "/api/gastpagina/logout"
    name = "api:gastpagina:logout"

    async def post(self, request: web.Request) -> web.Response:
        """Forget the token."""
        runtime = self._runtime(request)
        if runtime is not None and (
            session := runtime.sessions.get(request.headers.get(TOKEN_HEADER))
        ):
            runtime.sessions.remove(session)
            runtime.notify()
        return web.Response(status=HTTPStatus.NO_CONTENT, headers=_NO_STORE)


class ConfigView(_GastView):
    """Rooms, devices, stations and limits; labels and keys only."""

    url = "/api/gastpagina/config"
    name = "api:gastpagina:config"

    async def get(self, request: web.Request) -> web.Response:
        """Also without a session, so the code screen can show labels."""
        runtime = self._runtime(request)
        if runtime is None:
            return self._reply({"error": "uit"}, HTTPStatus.SERVICE_UNAVAILABLE)
        return self._reply(runtime.controller.config.public())


class StateView(_GastView):
    """Current state of every device."""

    url = "/api/gastpagina/state"
    name = "api:gastpagina:state"

    async def get(self, request: web.Request) -> web.Response:
        """All device states."""
        guarded = await self._guard(request)
        if isinstance(guarded, web.Response):
            return guarded
        runtime, _session = guarded
        return self._reply(runtime.controller.states())


class ActionView(_GastView):
    """Run one whitelisted action."""

    url = "/api/gastpagina/action"
    name = "api:gastpagina:action"

    async def post(self, request: web.Request) -> web.Response:
        """Validate, then act."""
        guarded = await self._guard(request)
        if isinstance(guarded, web.Response):
            return guarded
        runtime, session = guarded
        body = await _body(request, _ACTION_SCHEMA)
        if body is None:
            return self._reply({"error": "ongeldig"}, HTTPStatus.BAD_REQUEST)
        try:
            result = await runtime.async_action(
                body["device"], body["action"], body.get("value"), session
            )
        except ActionError as err:
            return self._reply({"error": err.error}, err.status)
        return self._reply({"ok": True, **result})


class StreamTicketView(_GastView):
    """EventSource cannot send headers: trade the token for a 30 s single-use ticket."""

    url = "/api/gastpagina/stream-ticket"
    name = "api:gastpagina:stream-ticket"

    async def post(self, request: web.Request) -> web.Response:
        """Issue a ticket."""
        guarded = await self._guard(request)
        if isinstance(guarded, web.Response):
            return guarded
        runtime, session = guarded
        return self._reply(
            {
                "ticket": runtime.sessions.issue_ticket(session),
                "expires_in": int(TICKET_TTL.total_seconds()),
            }
        )


def _sse(event: str | None, data: Any) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


class StreamView(_GastView):
    """Server-sent events: one message per device change, a keepalive every 25 s."""

    url = "/api/gastpagina/stream"
    name = "api:gastpagina:stream"

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Authenticate with the header (fetch streaming) or a ticket (EventSource)."""
        runtime = self._runtime(request)
        if runtime is None:
            return self._reply({"error": "uit"}, HTTPStatus.SERVICE_UNAVAILABLE)
        if TOKEN_HEADER in request.headers:
            session = await runtime.async_authenticate(request.headers[TOKEN_HEADER])
        else:
            session = await runtime.async_authenticate_ticket(request.query.get("ticket"))
        if session is None or not runtime.allowed(session):
            if not runtime.access_enabled:
                return self._reply({"error": "uit"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return self._reply({"error": "sessie"}, HTTPStatus.UNAUTHORIZED)
        return await _stream(runtime, request, session)


async def _stream(
    runtime: GastRuntime, request: web.Request, session: Session
) -> web.StreamResponse:
    hass = runtime.hass
    config = runtime.controller.config
    by_entity = config.by_entity()
    queue: asyncio.Queue[object] = asyncio.Queue()

    @callback
    def on_state(event: Event[EventStateChangedData]) -> None:
        for device_id in by_entity.get(event.data["entity_id"], ()):
            queue.put_nowait(device_id)

    @callback
    def on_recheck() -> None:
        queue.put_nowait(_RECHECK)

    @callback
    def on_config() -> None:
        queue.put_nowait(_CONFIG)

    @callback
    def on_stop(_event: Event) -> None:
        queue.put_nowait(_CLOSE)

    unsubs = [
        async_track_state_change_event(hass, list(by_entity), on_state),
        async_dispatcher_connect(hass, SIGNAL_RECHECK, on_recheck),
        async_dispatcher_connect(hass, SIGNAL_CONFIG, on_config),
        hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, on_stop),
    ]
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        }
    )
    try:
        await response.prepare(request)
        await response.write(_sse("snapshot", runtime.controller.states()))
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), STREAM_KEEPALIVE)
            except TimeoutError:
                item = None
            if item is _CLOSE or runtime.closed:
                break
            if not runtime.allowed(session):
                await response.write(_sse("uit", {}))
                break
            if not runtime.session_valid(session):
                await response.write(_sse("sessie", {}))
                break
            if item is _CONFIG:
                await response.write(_sse("config", {}))
                break
            if item is None:
                await response.write(b": keepalive\n\n")
            elif isinstance(item, str):
                device = config.devices[item]
                state = runtime.controller.state_of(device)
                await response.write(_sse(None, {"device": item, "state": state}))
    except ConnectionResetError:
        _LOGGER.debug("Guest stream closed by the client")
    finally:
        for unsub in unsubs:
            unsub()
    return response
