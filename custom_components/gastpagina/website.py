"""The public website: availability, an indicative price and booking requests.

A request becomes an *option* in the bookings calendar: its nights are blocked in
the outgoing feeds at once, but it gets no code until the owner confirms it.
Options the owner leaves alone expire.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import re
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import metadata as md, occupancy
from .calendar_io import CalendarIO, CalendarUnavailable
from .const import (
    DOMAIN,
    EVENT_REQUEST,
    EVENT_REQUEST_CONFIRMED,
    EVENT_REQUEST_EXPIRED,
    EVENT_REQUEST_REJECTED,
    MAX_STAY_NIGHTS,
    OPEN_REQUESTS_MAX,
    OPEN_REQUESTS_PER_EMAIL,
    PERSONAL_DATA_DAYS,
    REQUESTS_PER_DAY,
    REQUESTS_PER_IP_HOUR,
)
from .devices import Problem

WEEKDAYS = ("ma", "di", "wo", "do", "vr", "za", "zo")
_MONTH_DAY = re.compile(r"(\d{2})-(\d{2})")
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,24}")
_PHONE = re.compile(r"\+?[0-9 ./()-]{6,30}")


# --- configuration -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Season:
    """A period of the year (may wrap past New Year) with its own price or minimum."""

    start: tuple[int, int]
    end: tuple[int, int]
    per_night: float | None
    min_nights: int | None

    def covers(self, day: date) -> bool:
        """Whether this day (as month-day) lies in the season, both ends included."""
        md_ = (day.month, day.day)
        if self.start <= self.end:
            return self.start <= md_ <= self.end
        return md_ >= self.start or md_ <= self.end


@dataclass(frozen=True, slots=True)
class RentalConfig:
    """The `verhuur:` section of gastpagina.yaml."""

    enabled: bool = False
    min_nights: int = 2
    max_guests: int = 4
    lead_hours: int = 24
    months_ahead: int = 18
    arrival_days: frozenset[int] | None = None
    standard: float | None = None
    seasons: tuple[Season, ...] = ()
    cleaning: float = 0.0
    tourist_tax: float = 0.0

    def season(self, day: date) -> Season | None:
        """The first season that covers the day."""
        return next((s for s in self.seasons if s.covers(day)), None)

    def min_nights_for(self, arrival: date) -> int:
        """Minimum stay, as set for the season of the arrival day."""
        season = self.season(arrival)
        return season.min_nights if season and season.min_nights else self.min_nights

    def night_price(self, day: date) -> float | None:
        """Price of one night."""
        season = self.season(day)
        if season is not None and season.per_night is not None:
            return season.per_night
        return self.standard


def _month_day(value: Any) -> tuple[int, int]:
    match = _MONTH_DAY.fullmatch(str(value).strip())
    if not match:
        raise vol.Invalid("verwacht MM-DD")
    month, day = int(match.group(1)), int(match.group(2))
    date(2024, month, day)  # raises for 02-30 and the like (2024: 02-29 is fine)
    return month, day


def _checked_month_day(value: Any) -> tuple[int, int]:
    try:
        return _month_day(value)
    except ValueError as err:
        raise vol.Invalid("geen geldige datum") from err


_MONEY = vol.All(vol.Coerce(float), vol.Range(min=0, max=100000))
_SEASON = vol.Schema(
    {
        vol.Required("van"): _checked_month_day,
        vol.Required("tot"): _checked_month_day,
        vol.Optional("per_nacht"): _MONEY,
        vol.Optional("min_nachten"): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
    }
)
_PRICES = vol.Schema(
    {
        vol.Optional("standaard"): _MONEY,
        vol.Optional("seizoenen", default=list): [_SEASON],
        vol.Optional("schoonmaak", default=0): _MONEY,
        vol.Optional("toeristenbelasting_pp_pn", default=0): _MONEY,
    }
)
_RENTAL = vol.Schema(
    {
        vol.Optional("min_nachten", default=2): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
        vol.Optional("max_personen", default=4): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
        vol.Optional("termijn_uren", default=24): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=24 * 60)
        ),
        vol.Optional("maanden_vooruit", default=18): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=24)
        ),
        vol.Optional("aankomstdagen"): [vol.In(WEEKDAYS)],
        vol.Optional("prijzen", default=dict): _PRICES,
    }
)


def parse_rental(raw: Any) -> tuple[RentalConfig, list[Problem]]:
    """Read the `verhuur:` section; absent means the website API is off."""
    if raw is None:
        return RentalConfig(), []
    try:
        data = _RENTAL(raw)
    except vol.Invalid as err:
        return RentalConfig(), [
            Problem("rental_invalid", "rental_invalid", {"error": str(err)[:200]})
        ]
    prices = data["prijzen"]
    days = data.get("aankomstdagen")
    return (
        RentalConfig(
            enabled=True,
            min_nights=data["min_nachten"],
            max_guests=data["max_personen"],
            lead_hours=data["termijn_uren"],
            months_ahead=data["maanden_vooruit"],
            arrival_days=frozenset(WEEKDAYS.index(d) for d in days) if days else None,
            standard=prices.get("standaard"),
            seasons=tuple(
                Season(s["van"], s["tot"], s.get("per_nacht"), s.get("min_nachten"))
                for s in prices["seizoenen"]
            ),
            cleaning=prices["schoonmaak"],
            tourist_tax=prices["toeristenbelasting_pp_pn"],
        ),
        [],
    )


# --- requests ------------------------------------------------------------------------------


def _one_line(max_len: int) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        text = " ".join(str(value).split())
        if not 1 <= len(text) <= max_len:
            raise vol.Invalid("lengte")
        return text

    return check


def _iso_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as err:
        raise vol.Invalid("datum") from err


REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required("naam"): vol.All(_one_line(80), vol.Length(min=2)),
        vol.Required("email"): vol.All(_one_line(254), vol.Match(_EMAIL)),
        vol.Required("telefoon"): vol.All(_one_line(30), vol.Match(_PHONE)),
        vol.Required("personen"): vol.All(int, vol.Range(min=1, max=30)),
        vol.Required("aankomst"): _iso_date,
        vol.Required("vertrek"): _iso_date,
        vol.Optional("bericht", default=""): vol.All(str, vol.Length(max=1000)),
        # Honeypot: hidden on the page, so only bots fill it in.
        vol.Optional("website", default=""): vol.All(str, vol.Length(max=200)),
    }
)


@dataclass(slots=True)
class _Limiter:
    per_ip: dict[str, deque[datetime]] = field(default_factory=dict)
    total: deque[datetime] = field(default_factory=deque)

    def allow(self, ip: str, now: datetime) -> bool:
        hits = self.per_ip.setdefault(ip, deque())
        while hits and now - hits[0] >= timedelta(hours=1):
            hits.popleft()
        while self.total and now - self.total[0] >= timedelta(days=1):
            self.total.popleft()
        if len(hits) >= REQUESTS_PER_IP_HOUR or len(self.total) >= REQUESTS_PER_DAY:
            return False
        hits.append(now)
        self.total.append(now)
        return True


@dataclass(frozen=True, slots=True)
class WebsiteSettings:
    """Times and the option lifetime, from the options flow."""

    check_in: time
    check_out: time
    option_lifetime: timedelta


class RequestError(Exception):
    """A request the website should refuse; `error` is the code in the JSON reply."""

    def __init__(self, error: str, status: int = 400) -> None:
        """Remember the reply."""
        super().__init__(error)
        self.error = error
        self.status = status


class Website:
    """Availability, prices and requests."""

    def __init__(
        self,
        hass: HomeAssistant,
        io: CalendarIO,
        settings: WebsiteSettings,
        config: Callable[[], RentalConfig],
    ) -> None:
        """`config` returns the current `verhuur:` section (it reloads with the file)."""
        self.hass = hass
        self.io = io
        self.settings = settings
        self._config = config
        self._limiter = _Limiter()

    @property
    def config(self) -> RentalConfig:
        """The current rental settings."""
        return self._config()

    async def _stays(self) -> list[occupancy.Stay]:
        return occupancy.stays(await self.io.events(*occupancy.window(dt_util.now())))

    # --- read ------------------------------------------------------------------------------

    async def async_availability(self, first: date | None, end: date | None) -> dict[str, Any]:
        """Busy nights as date ranges, and the rules the form needs; no guest data."""
        config = self.config
        today = dt_util.now().date()
        horizon = _add_months(today, config.months_ahead)
        first = max(first or today, today)
        end = min(end or horizon, horizon)
        ranges = [
            (max(a, first), min(b, end))
            for a, b in occupancy.busy(await self._stays())
            if b > first and a < end
        ]
        return {
            "bezet": [[a.isoformat(), b.isoformat()] for a, b in ranges],
            "van": first.isoformat(),
            "tot": end.isoformat(),
            "min_nachten": config.min_nights,
            "max_personen": config.max_guests,
            "termijn_uren": config.lead_hours,
            "aankomstdagen": sorted(WEEKDAYS[d] for d in config.arrival_days)
            if config.arrival_days
            else None,
            "seizoenen": [
                {
                    "van": f"{s.start[0]:02d}-{s.start[1]:02d}",
                    "tot": f"{s.end[0]:02d}-{s.end[1]:02d}",
                    "min_nachten": s.min_nights,
                }
                for s in config.seasons
                if s.min_nights
            ],
            "inchecken": self.settings.check_in.strftime("%H:%M"),
            "uitchecken": self.settings.check_out.strftime("%H:%M"),
        }

    def price(self, arrival: date, departure: date, guests: int) -> dict[str, Any]:
        """Indicative price; raises RequestError."""
        config = self.config
        self._check_stay(arrival, departure, guests, check_lead=False)
        nights = [arrival + timedelta(days=i) for i in range((departure - arrival).days)]
        per_night = [config.night_price(day) for day in nights]
        if any(p is None for p in per_night):
            raise RequestError("geen_prijs", 404)
        lodging = round(sum(p for p in per_night if p is not None), 2)
        tax = round(config.tourist_tax * guests * len(nights), 2)
        return {
            "nachten": len(nights),
            "logies": lodging,
            "schoonmaak": config.cleaning,
            "toeristenbelasting": tax,
            "totaal": round(lodging + config.cleaning + tax, 2),
            "indicatief": True,
        }

    def _check_stay(
        self, arrival: date, departure: date, guests: int, *, check_lead: bool = True
    ) -> None:
        config = self.config
        now = dt_util.now()
        nights = (departure - arrival).days
        if nights < 1 or nights > MAX_STAY_NIGHTS:
            raise RequestError("datums")
        if nights < config.min_nights_for(arrival):
            raise RequestError("te_kort")
        if not 1 <= guests <= config.max_guests:
            raise RequestError("personen")
        if departure > _add_months(now.date(), config.months_ahead):
            raise RequestError("te_ver")
        if arrival < now.date():
            raise RequestError("verleden")
        if check_lead:
            tz = dt_util.get_default_time_zone()
            if datetime.combine(arrival, self.settings.check_in, tz) - now < timedelta(
                hours=config.lead_hours
            ):
                raise RequestError("te_laat")
        if config.arrival_days is not None and arrival.weekday() not in config.arrival_days:
            raise RequestError("aankomstdag")

    # --- requests ----------------------------------------------------------------------------

    async def async_request(self, body: dict[str, Any], ip: str) -> dict[str, Any]:
        """Turn a validated request into an option; raises RequestError."""
        now = dt_util.now()
        if body["website"]:
            return {"status": md.STATUS_OPTION}  # a bot: pretend it worked
        if not self._limiter.allow(ip, now):
            raise RequestError("te_veel_aanvragen", 429)
        arrival, departure = body["aankomst"], body["vertrek"]
        self._check_stay(arrival, departure, body["personen"])
        try:
            price = self.price(arrival, departure, body["personen"])
        except RequestError:
            price = None
        tz = dt_util.get_default_time_zone()
        start = datetime.combine(arrival, self.settings.check_in, tz)
        end = datetime.combine(departure, self.settings.check_out, tz)
        email = body["email"].casefold()
        async with self.io.lock:
            try:
                all_stays = await self._stays()
            except CalendarUnavailable as err:
                raise RequestError("niet_beschikbaar", 503) from err
            options = [s for s in all_stays if s.is_option]
            if len(options) >= OPEN_REQUESTS_MAX or (
                sum(s.meta.get(md.EMAIL, "").casefold() == email for s in options)
                >= OPEN_REQUESTS_PER_EMAIL
            ):
                raise RequestError("te_veel_aanvragen", 429)
            if not occupancy.is_free(all_stays, arrival, departure):
                raise RequestError("bezet", 409)
            description = _quoted(body["bericht"])
            description = md.write(
                description,
                {
                    md.SOURCE: "website",
                    md.STATUS: md.STATUS_OPTION,
                    md.GUESTS: str(body["personen"]),
                    md.EMAIL: body["email"],
                    md.PHONE: body["telefoon"],
                    md.REQUESTED: dt_util.utcnow().isoformat(timespec="seconds"),
                },
            )
            try:
                await self.io.create(f"{body['naam']} (website)", start, end, description)
            except CalendarUnavailable as err:
                raise RequestError("niet_beschikbaar", 503) from err
        expires = dt_util.utcnow() + self.settings.option_lifetime
        self.hass.bus.async_fire(
            EVENT_REQUEST,
            {
                "naam": body["naam"],
                "email": body["email"],
                "telefoon": body["telefoon"],
                "personen": body["personen"],
                "aankomst": start.isoformat(),
                "vertrek": end.isoformat(),
                "bericht": body["bericht"],
                "prijs": price["totaal"] if price else None,
                "vervalt": dt_util.as_local(expires).isoformat(),
            },
        )
        persistent_notification.async_create(
            self.hass,
            f"{body['naam']}, {body['personen']} personen, {arrival:%d-%m-%Y} tot "
            f"{departure:%d-%m-%Y}. Bevestig of weiger met gastpagina.bevestig of "
            "gastpagina.weiger.",
            title="Nieuwe aanvraag via de website",
            notification_id=f"{DOMAIN}_request_{arrival:%Y%m%d}_{email[:20]}",
        )
        return {"status": md.STATUS_OPTION, "nachten": (departure - arrival).days, "prijs": price}

    async def _find_option(self, query: str) -> occupancy.Stay:
        wanted = query.strip().casefold()
        matches = [
            s
            for s in await self._stays()
            if s.is_option
            and wanted in ((s.event.uid or "").casefold(), s.event.summary.strip().casefold())
        ]
        if not matches:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="request_not_found",
                translation_placeholders={"booking": query},
            )
        if len(matches) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="booking_ambiguous",
                translation_placeholders={"booking": query},
            )
        return matches[0]

    async def async_confirm(self, query: str) -> dict[str, Any]:
        """The owner accepts: the option becomes a booking (the code follows)."""
        async with self.io.lock:
            stay = await self._find_option(query)
            await self.io.update(
                stay.event,
                description=md.write(stay.event.description, {md.STATUS: md.STATUS_CONFIRMED}),
            )
        data = self._public(stay)
        self.hass.bus.async_fire(EVENT_REQUEST_CONFIRMED, data)
        persistent_notification.async_dismiss(self.hass, self._notification_id(stay))
        return data

    async def async_reject(self, query: str, reason: str | None = None) -> dict[str, Any]:
        """The owner declines: the option goes, the nights are free again."""
        async with self.io.lock:
            stay = await self._find_option(query)
            await self.io.delete(stay.event)
        data = {**self._public(stay), "reden": reason or ""}
        self.hass.bus.async_fire(EVENT_REQUEST_REJECTED, data)
        persistent_notification.async_dismiss(self.hass, self._notification_id(stay))
        return data

    def _public(self, stay: occupancy.Stay) -> dict[str, Any]:
        return {
            "naam": stay.event.summary.removesuffix(" (website)"),
            "email": stay.meta.get(md.EMAIL, ""),
            "telefoon": stay.meta.get(md.PHONE, ""),
            "personen": stay.meta.get(md.GUESTS, ""),
            "aankomst": dt_util.as_local(stay.event.start_datetime_local).isoformat(),
            "vertrek": dt_util.as_local(stay.event.end_datetime_local).isoformat(),
        }

    @staticmethod
    def _notification_id(stay: occupancy.Stay) -> str:
        email = stay.meta.get(md.EMAIL, "").casefold()
        return f"{DOMAIN}_request_{stay.first:%Y%m%d}_{email[:20]}"

    async def async_expire(self) -> int:
        """Remove options older than their lifetime; returns how many."""
        now = dt_util.utcnow()
        expired: list[occupancy.Stay] = []
        async with self.io.lock:
            for stay in await self._stays():
                if not stay.is_option:
                    continue
                requested = dt_util.parse_datetime(stay.meta.get(md.REQUESTED, ""))
                if requested is None or now - requested < self.settings.option_lifetime:
                    continue
                await self.io.delete(stay.event)
                expired.append(stay)
        for stay in expired:
            data = self._public(stay)
            self.hass.bus.async_fire(EVENT_REQUEST_EXPIRED, data)
            persistent_notification.async_dismiss(self.hass, self._notification_id(stay))
            persistent_notification.async_create(
                self.hass,
                f"De aanvraag van {data['naam']} ({stay.first:%d-%m-%Y}) is vervallen; "
                "de nachten zijn weer vrij.",
                title="Aanvraag vervallen",
                notification_id=f"{DOMAIN}_expired_{stay.key}",
            )
        return len(expired)

    async def async_cleanup(self) -> int:
        """Remove guests' personal data some weeks after they left; returns how many."""
        now = dt_util.now()
        cutoff = now - timedelta(days=PERSONAL_DATA_DAYS)
        cleaned = 0
        async with self.io.lock:
            events = await self.io.events(now - timedelta(days=800), cutoff)
            for event in events:
                if event.end_datetime_local > cutoff:
                    continue
                meta = md.read(event.description)
                source = meta.get(md.SOURCE, "").casefold()
                personal = [k for k in md.PERSONAL if k in meta]
                summary: str | None = None
                if source in ("website", "facebook") and not event.summary.startswith("Boeking ("):
                    summary = f"Boeking ({source})"
                elif (phone := meta.get(md.PHONE4)) and phone in event.summary:
                    summary = source.capitalize() or "Boeking"
                if not personal and summary is None:
                    continue
                await self.io.update(
                    event,
                    summary=summary,
                    description=md.write(event.description, dict.fromkeys(personal)),
                )
                cleaned += 1
        return cleaned

    def open_requests(self, all_stays: list[occupancy.Stay]) -> int:
        """Options waiting for the owner."""
        return sum(s.is_option for s in all_stays)


def _quoted(message: str) -> str:
    """The guest's message, each line quoted so it can never pass for metadata or a code."""
    lines = [" ".join(line.split()) for line in message.splitlines()]
    return "\n".join(f"> {line}" for line in lines if line)[:1200]


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    return day  # unreachable
