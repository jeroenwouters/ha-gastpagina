"""Public endpoints of the hub: channel feeds, the website API and the website files.

None of these need a session. The feeds are guarded by a long secret token in the
URL; the website API only ever shows dates and prices, never guests.
"""

from __future__ import annotations

from datetime import date
from http import HTTPStatus
import json
import mimetypes
from pathlib import Path
from typing import Any, Final

from aiohttp import web
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView
import voluptuous as vol

from .calendar_io import CalendarUnavailable
from .const import DOMAIN, SITE_DIR, SITE_URL
from .runtime import GastRuntime
from .website import REQUEST_SCHEMA, RequestError

_NO_STORE: Final = {"Cache-Control": "no-store"}
_MAX_BODY: Final = 4096
CSP: Final = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
_SITE_HEADERS: Final = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}


def _runtime(request: web.Request) -> GastRuntime | None:
    runtime: GastRuntime | None = request.app[KEY_HASS].data.get(DOMAIN)
    return runtime if runtime is not None and not runtime.closed else None


class _PublicView(HomeAssistantView):
    requires_auth = False

    def _reply(self, body: Any, status: int = HTTPStatus.OK) -> web.Response:
        return self.json(body, status, _NO_STORE)


class FeedView(_PublicView):
    """The outgoing iCal feed of one channel."""

    url = "/api/gastpagina/ical/{token}/{feed}"
    name = "api:gastpagina:ical"

    async def get(self, request: web.Request, token: str, feed: str) -> web.Response:
        """404 for anything but a known channel with its own token."""
        runtime = _runtime(request)
        channel = feed.removesuffix(".ics")
        if runtime is None or not feed.endswith(".ics") or not runtime.hub.token_ok(channel, token):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            text = await runtime.hub.async_render(channel)
        except CalendarUnavailable:
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
        return web.Response(
            text=text,
            content_type="text/calendar",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )


def _date_arg(request: web.Request, name: str) -> date | None:
    value = request.query.get(name)
    if value is None:
        return None
    return date.fromisoformat(value)


class _WebsiteView(_PublicView):
    def _website(self, request: web.Request) -> GastRuntime | web.Response:
        runtime = _runtime(request)
        if runtime is None:
            return self._reply({"error": "uit"}, HTTPStatus.SERVICE_UNAVAILABLE)
        if not runtime.rental_config.enabled:
            return self._reply({"error": "geen_verhuur"}, HTTPStatus.NOT_FOUND)
        return runtime


class AvailabilityView(_WebsiteView):
    """Busy nights and the booking rules."""

    url = "/api/gastpagina/beschikbaarheid"
    name = "api:gastpagina:beschikbaarheid"

    async def get(self, request: web.Request) -> web.Response:
        """Optional ?van=YYYY-MM-DD&tot=YYYY-MM-DD."""
        runtime = self._website(request)
        if isinstance(runtime, web.Response):
            return runtime
        try:
            first, end = _date_arg(request, "van"), _date_arg(request, "tot")
        except ValueError:
            return self._reply({"error": "datum"}, HTTPStatus.BAD_REQUEST)
        try:
            return self._reply(await runtime.website.async_availability(first, end))
        except CalendarUnavailable:
            return self._reply({"error": "niet_beschikbaar"}, HTTPStatus.SERVICE_UNAVAILABLE)


class PriceView(_WebsiteView):
    """Indicative price of a stay."""

    url = "/api/gastpagina/prijs"
    name = "api:gastpagina:prijs"

    async def get(self, request: web.Request) -> web.Response:
        """?aankomst=&vertrek=&personen=."""
        runtime = self._website(request)
        if isinstance(runtime, web.Response):
            return runtime
        try:
            arrival, departure = _date_arg(request, "aankomst"), _date_arg(request, "vertrek")
            guests = int(request.query.get("personen", "1"))
        except ValueError:
            return self._reply({"error": "datum"}, HTTPStatus.BAD_REQUEST)
        if arrival is None or departure is None:
            return self._reply({"error": "datum"}, HTTPStatus.BAD_REQUEST)
        try:
            return self._reply(runtime.website.price(arrival, departure, guests))
        except RequestError as err:
            return self._reply({"error": err.error}, err.status)


class RequestView(_WebsiteView):
    """A booking request from the website."""

    url = "/api/gastpagina/aanvraag"
    name = "api:gastpagina:aanvraag"

    async def post(self, request: web.Request) -> web.Response:
        """Validate, then make an option."""
        runtime = self._website(request)
        if isinstance(runtime, web.Response):
            return runtime
        raw = await request.content.read(_MAX_BODY + 1)
        if len(raw) > _MAX_BODY:
            return self._reply({"error": "ongeldig"}, HTTPStatus.BAD_REQUEST)
        try:
            body = REQUEST_SCHEMA(json.loads(raw))
        except (ValueError, vol.Invalid) as err:
            field = _field(err)
            return self._reply({"error": "ongeldig", "veld": field}, HTTPStatus.BAD_REQUEST)
        try:
            result = await runtime.website.async_request(body, request.remote or "unknown")
        except RequestError as err:
            return self._reply({"error": err.error}, err.status)
        await runtime.async_calendar_changed()
        return self._reply(result, HTTPStatus.CREATED)


def _field(err: Exception) -> str | None:
    if isinstance(err, vol.MultipleInvalid):
        err = err.errors[0]
    if isinstance(err, vol.Invalid) and err.path:
        return str(err.path[0])
    return None


BUNDLED_SITE: Final = Path(__file__).parent / "site"


class SiteView(_PublicView):
    """Static files of the website.

    A file in /config/gastpagina_site/ wins (photos, local changes); otherwise the
    site that ships with the integration is served, so an update brings it along.
    """

    url = SITE_URL + "/{path:.*}"
    name = "gastpagina:site"

    async def get(self, request: web.Request, path: str) -> web.StreamResponse:
        """index.html for directories; nothing outside the folder."""
        hass = request.app[KEY_HASS]
        own = Path(hass.config.path(SITE_DIR))
        file = await hass.async_add_executor_job(_resolve_first, (own, BUNDLED_SITE), path)
        if file is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, headers=_SITE_HEADERS)
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        cache = "no-cache" if file.suffix == ".html" else "public, max-age=86400"
        return web.FileResponse(
            file,
            headers={**_SITE_HEADERS, "Cache-Control": cache, "Content-Type": content_type},
        )


class SiteRootView(_PublicView):
    """/verhuur → /verhuur/."""

    url = SITE_URL
    name = "gastpagina:site-root"

    async def get(self, request: web.Request) -> web.Response:
        """Redirect, so relative links work."""
        raise web.HTTPFound(SITE_URL + "/")


def _resolve_first(roots: tuple[Path, ...], path: str) -> Path | None:
    for root in roots:
        if (found := _resolve(root, path)) is not None:
            return found
    return None


def _resolve(root: Path, path: str) -> Path | None:
    if "\0" in path or path.startswith(("/", "\\")):
        return None
    try:
        root = root.resolve(strict=True)
        candidate = (root / path).resolve()
    except (OSError, RuntimeError):
        return None
    if not candidate.is_relative_to(root):
        return None
    if candidate.is_dir():
        candidate /= "index.html"
    if candidate.name.startswith(".") or not candidate.is_file():
        return None
    return candidate


PUBLIC_VIEWS: Final = (
    FeedView,
    AvailabilityView,
    PriceView,
    RequestView,
    SiteView,
    SiteRootView,
)
