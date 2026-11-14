"""Kanalenhub and website (v0.4): acceptance criteria 38-51 and the details around them."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import re
from typing import Any

from aiohttp import ClientError
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gastpagina import ical, metadata as md
from custom_components.gastpagina.const import (
    CONF_CALENDAR,
    CONF_FEED_AIRBNB,
    CONF_FEED_BOOKING,
    CONF_HOURS_AFTER,
    CONF_HOURS_BEFORE,
    CONF_MAX_ATTEMPTS,
    CONF_MIN_CODE_LENGTH,
    DOMAIN,
    EVENT_BOOKING_CANCELLED,
    EVENT_BOOKING_NEW,
    EVENT_CODE_CREATED,
    EVENT_REQUEST,
    EVENT_REQUEST_CONFIRMED,
    EVENT_REQUEST_EXPIRED,
    SITE_DIR,
)
from custom_components.gastpagina.website import parse_rental

from .conftest import DEVICES_YAML, TZ, Harness

AIRBNB = "https://www.airbnb.be/calendar/ical/123.ics?s=geheim"
BOOKING = "https://admin.booking.com/hotel/hoteladmin/ical.html?t=geheim"

RENTAL_YAML = """
verhuur:
  min_nachten: 2
  max_personen: 4
  termijn_uren: 24
  prijzen:
    standaard: 110
    seizoenen:
      - { van: "07-01", tot: "08-31", per_nacht: 150, min_nachten: 7 }
    schoonmaak: 60
    toeristenbelasting_pp_pn: 3
"""


def _entry(**extra: Any) -> dict[str, Any]:
    return {
        CONF_CALENDAR: "calendar.gasten",
        CONF_HOURS_BEFORE: 24,
        CONF_HOURS_AFTER: 0,
        CONF_MIN_CODE_LENGTH: 10,
        CONF_MAX_ATTEMPTS: 5,
        CONF_FEED_AIRBNB: AIRBNB,
        CONF_FEED_BOOKING: BOOKING,
        **extra,
    }


FEEDS = pytest.mark.parametrize("entry_data", [_entry()])
SITE = pytest.mark.parametrize("devices_yaml", [DEVICES_YAML + RENTAL_YAML])


def vevent(uid: str, start: str, end: str, summary: str = "Reserved", desc: str = "") -> str:
    lines = [
        "BEGIN:VEVENT",
        f"DTSTART;VALUE=DATE:{start}",
        f"DTEND;VALUE=DATE:{end}",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
    ]
    if desc:
        lines.append(f"DESCRIPTION:{desc}")
    return "\r\n".join([*lines, "END:VEVENT"])


def feed(*events: str) -> str:
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Airbnb Inc//Hosting Calendar//EN",
            *events,
            "END:VCALENDAR",
            "",
        ]
    )


AIRBNB_DESC = (
    "Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMABCD1234\\n"
    "Phone Number (Last 4 Digits): 4821"
)
STAY = vevent("abc@airbnb.com", "20261210", "20261214", desc=AIRBNB_DESC)


@pytest.fixture(autouse=True)
def _feeds(aioclient_mock: AiohttpClientMocker) -> None:
    """Patch the HTTP session before the integration starts; empty feeds by default."""
    serve(aioclient_mock)


def serve(mock: AiohttpClientMocker, airbnb: str | None = None, booking: str = "") -> None:
    mock.clear_requests()
    mock.get(AIRBNB, text=airbnb if airbnb is not None else feed())
    mock.get(BOOKING, text=booking or feed())


async def sync(harness: Harness) -> dict[str, Any]:
    response = await harness.hass.services.async_call(
        DOMAIN, "sync_now", blocking=True, return_response=True
    )
    await harness.hass.async_block_till_done()
    return response


def by_source(harness: Harness, source: str) -> list[Any]:
    return [e for e in harness.calendar.events if md.read(e.description).get("bron") == source]


def issue_keys(harness: Harness) -> set[str]:
    registry = ir.async_get(harness.hass)
    return {i.translation_key for (d, _), i in registry.issues.items() if d == DOMAIN}


async def links(harness: Harness) -> dict[str, str]:
    response = await harness.hass.services.async_call(
        DOMAIN, "feed_links", blocking=True, return_response=True
    )
    return response["links"]


async def export(harness: Harness, channel: str) -> tuple[int, str]:
    url = (await links(harness))[channel]
    path = url[url.index("/api/") :]
    resp = await harness.client.get(path)
    return resp.status, await resp.text()


# --- 38. a new Airbnb booking becomes a timed event, then gets a code -----------------------------


@FEEDS
async def test_new_airbnb_booking(harness: Harness, aioclient_mock: AiohttpClientMocker) -> None:
    serve(aioclient_mock, feed(STAY))
    news = async_capture_events(harness.hass, EVENT_BOOKING_NEW)
    codes = async_capture_events(harness.hass, EVENT_CODE_CREATED)
    result = await sync(harness)
    assert result["feeds"]["airbnb"]["nieuw"] == 1

    (event,) = by_source(harness, "airbnb")
    meta = md.read(event.description)
    assert meta["extern"] == "abc@airbnb.com"
    assert meta["tel4"] == "4821"
    assert meta["reservatie"].endswith("/HMABCD1234")
    assert event.summary == "Airbnb - 4821"
    assert event.start == datetime(2026, 12, 10, 16, 0, tzinfo=TZ)
    assert event.end == datetime(2026, 12, 14, 10, 0, tzinfo=TZ)
    assert re.search(r"^code: \S+", event.description, re.MULTILINE)
    assert len(news) == 1
    assert len(codes) == 1
    assert harness.hass.states.get("binary_sensor.gastpagina_feed_airbnb_ok").state == "on"


@FEEDS
async def test_same_feed_twice_changes_nothing(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    serve(aioclient_mock, feed(STAY))
    await sync(harness)
    before = list(harness.calendar.events)
    result = await sync(harness)
    assert result["feeds"]["airbnb"] == {"nieuw": 0, "gewijzigd": 0, "geannuleerd": 0}
    assert harness.calendar.events == before


# --- 39. new dates: the event moves, the code stays -----------------------------------------------


@FEEDS
async def test_changed_dates_keep_the_code(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    serve(aioclient_mock, feed(STAY))
    await sync(harness)
    (event,) = by_source(harness, "airbnb")
    code = re.search(r"^code: (\S+)", event.description, re.MULTILINE).group(1)

    serve(aioclient_mock, feed(vevent("abc@airbnb.com", "20261211", "20261216", desc=AIRBNB_DESC)))
    result = await sync(harness)
    assert result["feeds"]["airbnb"]["gewijzigd"] == 1
    (event,) = by_source(harness, "airbnb")
    assert event.start == datetime(2026, 12, 11, 16, 0, tzinfo=TZ)
    assert event.end == datetime(2026, 12, 16, 10, 0, tzinfo=TZ)
    assert f"code: {code}" in event.description


# --- 40. gone from the feed: removed, notified ----------------------------------------------------


@FEEDS
async def test_cancelled_booking_is_removed(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    serve(aioclient_mock, feed(STAY))
    await sync(harness)
    cancelled = async_capture_events(harness.hass, EVENT_BOOKING_CANCELLED)
    serve(aioclient_mock, feed())
    result = await sync(harness)
    assert result["feeds"]["airbnb"]["geannuleerd"] == 1
    assert by_source(harness, "airbnb") == []
    assert len(cancelled) == 1
    notes = harness.hass.data["persistent_notification"]
    assert any(n["title"] == "Boeking geannuleerd" for n in notes.values())


@FEEDS
async def test_mass_removal_is_held_back(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    stays = [
        vevent(f"s{i}@airbnb.com", f"202612{10 + 2 * i}", f"202612{11 + 2 * i}") for i in range(5)
    ]
    serve(aioclient_mock, feed(*stays))
    await sync(harness)
    assert len(by_source(harness, "airbnb")) == 5
    serve(aioclient_mock, feed())
    await sync(harness)
    assert len(by_source(harness, "airbnb")) == 5
    assert "feed_mass_removal" in issue_keys(harness)


# --- 41. a failing feed: issue after 3, nothing removed ------------------------------------------


@FEEDS
async def test_failing_feed(harness: Harness, aioclient_mock: AiohttpClientMocker) -> None:
    serve(aioclient_mock, feed(STAY))
    await sync(harness)
    aioclient_mock.clear_requests()
    aioclient_mock.get(AIRBNB, exc=ClientError())
    aioclient_mock.get(BOOKING, text=feed())
    for _ in range(3):
        await sync(harness)
    assert "feed_failing" in issue_keys(harness)
    assert len(by_source(harness, "airbnb")) == 1
    assert harness.hass.states.get("binary_sensor.gastpagina_feed_airbnb_ok").state == "off"

    serve(aioclient_mock, feed(STAY))
    await sync(harness)
    assert "feed_failing" not in issue_keys(harness)


@FEEDS
async def test_not_a_calendar_counts_as_failure(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    serve(aioclient_mock, "<html>login</html>")
    await sync(harness)
    assert harness.hass.states.get("binary_sensor.gastpagina_feed_airbnb_ok").state == "off"


# --- 42. outgoing feeds: per channel, without guest data ----------------------------------------


@FEEDS
async def test_export_per_channel(harness: Harness, aioclient_mock: AiohttpClientMocker) -> None:
    serve(
        aioclient_mock,
        feed(STAY),
        feed(vevent("b1", "20261220", "20261223", "CLOSED - Not available")),
    )
    await sync(harness)

    status, airbnb = await export(harness, "airbnb")
    assert status == 200
    days = {(e.start.isoformat(), e.end.isoformat()) for e in ical.parse(airbnb)}
    assert ("2026-12-10", "2026-12-14") not in days  # its own booking
    assert ("2026-12-20", "2026-12-23") in days  # from Booking.com
    assert ("2026-11-03", "2026-11-07") in days  # the owner's booking

    _status, booking = await export(harness, "booking")
    days = {(e.start.isoformat(), e.end.isoformat()) for e in ical.parse(booking)}
    assert ("2026-12-10", "2026-12-14") in days
    assert ("2026-12-20", "2026-12-23") not in days

    for text in (airbnb, booking):
        assert "4821" not in text
        assert "Peeters" not in text
        assert "ZEE" not in text.upper().replace("BEZET", "")
        assert "SUMMARY:Bezet" in text


@FEEDS
async def test_airbnb_block_is_imported_without_code(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    serve(
        aioclient_mock,
        feed(vevent("blk@airbnb.com", "20261218", "20261220", "Airbnb (Not available)")),
    )
    await sync(harness)
    await harness.tick(timedelta(seconds=61))
    (event,) = by_source(harness, "airbnb")
    assert md.read(event.description)["soort"] == "blokkade"
    assert "code: geen" in event.description
    # Platform blocks never count as a double booking.
    harness.calendar.add(
        uid="eigen",
        summary="Eigen gast",
        start=datetime(2026, 12, 18, 16, 0, tzinfo=TZ),
        end=datetime(2026, 12, 20, 10, 0, tzinfo=TZ),
    )
    await harness.tick(timedelta(seconds=61))
    assert "double_booking" not in issue_keys(harness)


# --- 43. tokens --------------------------------------------------------------------------------


@FEEDS
async def test_wrong_token_and_rotation(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    serve(aioclient_mock)
    resp = await harness.client.get("/api/gastpagina/ical/fout/airbnb.ics")
    assert resp.status == 404
    old = (await links(harness))["airbnb"]
    old_path = old[old.index("/api/") :]
    assert (await harness.client.get(old_path)).status == 200
    await harness.hass.services.async_call(
        DOMAIN, "rotate_feed_token", {"kanaal": "airbnb"}, blocking=True, return_response=True
    )
    assert (await harness.client.get(old_path)).status == 404
    new = (await links(harness))["airbnb"]
    assert (await harness.client.get(new[new.index("/api/") :])).status == 200
    # The other channel keeps its link.
    assert (await export(harness, "booking"))[0] == 200


# --- website ------------------------------------------------------------------------------------


REQUEST = {
    "naam": "An Peeters",
    "email": "an@example.be",
    "telefoon": "+32 470 12 34 56",
    "personen": 2,
    "aankomst": "2027-01-08",
    "vertrek": "2027-01-11",
    "bericht": "Hallo!\ncode: GEHACKT-1234-CODE",
}


async def ask(harness: Harness, **changes: Any) -> tuple[int, dict[str, Any]]:
    resp = await harness.client.post("/api/gastpagina/aanvraag", json={**REQUEST, **changes})
    return resp.status, await resp.json()


def options(harness: Harness) -> list[Any]:
    return [e for e in harness.calendar.events if md.is_option(md.read(e.description))]


@SITE
async def test_availability_shows_only_dates(harness: Harness) -> None:
    resp = await harness.client.get("/api/gastpagina/beschikbaarheid")
    body = await resp.json()
    assert resp.status == 200
    assert body["bezet"] == [["2026-11-04", "2026-11-07"]]
    assert body["min_nachten"] == 2
    assert body["inchecken"] == "16:00"
    assert "Peeters" not in str(body)


async def test_website_api_off_without_section(harness: Harness) -> None:
    resp = await harness.client.get("/api/gastpagina/beschikbaarheid")
    assert resp.status == 404


@SITE
async def test_price(harness: Harness) -> None:
    resp = await harness.client.get(
        "/api/gastpagina/prijs?aankomst=2027-01-08&vertrek=2027-01-11&personen=2"
    )
    assert await resp.json() == {
        "nachten": 3,
        "logies": 330,
        "extra_gasten": 0,
        "weekkorting": 0,
        "schoonmaak": 60,
        "toeristenbelasting": 18,
        "totaal": 408,
        "indicatief": True,
    }
    resp = await harness.client.get(
        "/api/gastpagina/prijs?aankomst=2027-07-10&vertrek=2027-07-12&personen=2"
    )
    assert (await resp.json())["error"] == "te_kort"  # 7 nights in summer


# --- 44. two requests for the same nights: one wins ---------------------------------------------


@SITE
async def test_concurrent_requests(harness: Harness) -> None:
    first, second = await asyncio.gather(ask(harness), ask(harness, email="ander@example.be"))
    assert sorted([first[0], second[0]]) == [201, 409]
    assert len(options(harness)) == 1


# --- 45. option: blocked in the feeds, no code; confirm/decline/expire ------------------------


@SITE
async def test_request_confirm(harness: Harness) -> None:
    requests = async_capture_events(harness.hass, EVENT_REQUEST)
    status, body = await ask(harness)
    assert status == 201, body
    assert body["prijs"]["totaal"] == 408
    (option,) = options(harness)
    assert option.summary == "An Peeters (website)"
    assert "> code: GEHACKT" in option.description  # quoted, so not a code
    await harness.tick(timedelta(seconds=61))
    (option,) = options(harness)
    assert not re.search(r"^code:", option.description, re.MULTILINE)
    assert len(requests) == 1
    assert harness.hass.states.get("sensor.gastpagina_openstaande_aanvragen").state == "1"

    _status, text = await export(harness, "airbnb")
    assert ("2027-01-08", "2027-01-11") in {
        (e.start.isoformat(), e.end.isoformat()) for e in ical.parse(text)
    }

    confirmed = async_capture_events(harness.hass, EVENT_REQUEST_CONFIRMED)
    codes = async_capture_events(harness.hass, EVENT_CODE_CREATED)
    await harness.hass.services.async_call(
        DOMAIN, "bevestig", {"booking": "An Peeters (website)"}, blocking=True
    )
    await harness.hass.async_block_till_done()
    assert options(harness) == []
    (booking,) = [e for e in harness.calendar.events if e.summary == "An Peeters (website)"]
    assert md.read(booking.description)["status"] == "bevestigd"
    assert re.search(r"^code: \S+", booking.description, re.MULTILINE)
    assert confirmed[0].data["email"] == "an@example.be"
    assert len(codes) == 1


@SITE
async def test_request_reject_frees_the_nights(harness: Harness) -> None:
    await ask(harness)
    await harness.hass.services.async_call(
        DOMAIN, "weiger", {"booking": "An Peeters (website)", "reden": "vol"}, blocking=True
    )
    assert options(harness) == []
    status, _body = await ask(harness, email="ander@example.be")
    assert status == 201


@SITE
async def test_option_expires(harness: Harness) -> None:
    expired = async_capture_events(harness.hass, EVENT_REQUEST_EXPIRED)
    await ask(harness)
    await harness.tick(timedelta(hours=47))
    assert len(options(harness)) == 1
    await harness.tick(timedelta(hours=1, minutes=1))
    assert options(harness) == []
    assert len(expired) == 1


@SITE
@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"aankomst": "2026-11-05", "vertrek": "2026-11-08"}, "bezet"),
        ({"aankomst": "2027-01-08", "vertrek": "2027-01-09"}, "te_kort"),
        ({"personen": 5}, "personen"),
        ({"aankomst": "2026-11-03", "vertrek": "2026-11-06"}, "verleden"),
        ({"aankomst": "2026-11-04", "vertrek": "2026-11-06"}, "te_laat"),
        ({"aankomst": "2029-01-08", "vertrek": "2029-01-11"}, "te_ver"),
    ],
)
async def test_request_rules(harness: Harness, changes: dict[str, Any], error: str) -> None:
    status, body = await ask(harness, **changes)
    assert body["error"] == error, body
    assert status in (400, 409)
    assert options(harness) == []


@SITE
async def test_invalid_email(harness: Harness) -> None:
    status, body = await ask(harness, email="geen-mail")
    assert (status, body["veld"]) == (400, "email")


# --- 46. overlap between an import and an option ------------------------------------------------


@SITE
@FEEDS
async def test_double_booking(harness: Harness, aioclient_mock: AiohttpClientMocker) -> None:
    await ask(harness)
    serve(aioclient_mock, feed(vevent("x@airbnb.com", "20270109", "20270112", desc=AIRBNB_DESC)))
    await sync(harness)
    assert "double_booking" in issue_keys(harness)
    assert len(options(harness)) == 1
    assert len(by_source(harness, "airbnb")) == 1


# --- 47. honeypot and rate limit ---------------------------------------------------------------


@SITE
async def test_honeypot(harness: Harness) -> None:
    status, _body = await ask(harness, website="http://spam.example")
    assert status == 201
    assert options(harness) == []


@SITE
async def test_rate_limit(harness: Harness) -> None:
    statuses = [
        (
            await ask(
                harness,
                email=f"g{i}@example.be",
                aankomst=f"2027-02-{10 + 3 * i:02d}",
                vertrek=f"2027-02-{12 + 3 * i:02d}",
            )
        )[0]
        for i in range(4)
    ]
    assert statuses[:1] == [201]
    assert 429 in statuses
    assert len(options(harness)) <= 3


@SITE
async def test_one_open_request_per_email(harness: Harness) -> None:
    await ask(harness)
    status, body = await ask(harness, aankomst="2027-02-08", vertrek="2027-02-11")
    assert (status, body["error"]) == (429, "te_veel_aanvragen")


# --- 49. personal data goes 30 days after departure -------------------------------------------


@SITE
async def test_personal_data_cleanup(harness: Harness) -> None:
    await ask(harness)
    await harness.hass.services.async_call(
        DOMAIN, "bevestig", {"booking": "An Peeters (website)"}, blocking=True
    )
    await harness.move_to(datetime(2027, 2, 12, 12, 0, tzinfo=TZ))
    await harness.tick(timedelta(hours=1, minutes=1))
    (booking,) = [
        e for e in harness.calendar.events if md.read(e.description).get("bron") == "website"
    ]
    meta = md.read(booking.description)
    assert booking.summary == "Boeking (website)"
    assert "email" not in meta
    assert "telefoon" not in meta
    assert meta["personen"] == "2"


# --- 50. the website files ---------------------------------------------------------------------


async def test_site_files(harness: Harness, tmp_path: Any) -> None:
    site = tmp_path / SITE_DIR
    site.mkdir()
    (site / "index.html").write_text("<h1>Zin in het zeetje</h1>")
    (tmp_path / "geheim.txt").write_text("nee")
    resp = await harness.client.get("/verhuur", allow_redirects=False)
    assert resp.status == 302
    resp = await harness.client.get("/verhuur/")
    assert resp.status == 200
    assert "Content-Security-Policy" in resp.headers
    assert "zeetje" in await resp.text()
    for path in ("/verhuur/../geheim.txt", "/verhuur/%2e%2e/geheim.txt", "/verhuur/nope.html"):
        assert (await harness.client.get(path)).status == 404
    # Files the owner did not override come with the integration.
    resp = await harness.client.get("/verhuur/privacy.html")
    assert resp.status == 200
    assert "Privacy." in await resp.text()
    assert (await harness.client.get("/verhuur/css/site.css")).status == 200


# --- 51. the importer leaves manual events alone -----------------------------------------------


@FEEDS
async def test_manual_events_untouched(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    harness.calendar.add(
        uid="fb-1",
        summary="Familie Maes",
        start=datetime(2027, 3, 1, 16, 0, tzinfo=TZ),
        end=datetime(2027, 3, 4, 10, 0, tzinfo=TZ),
        description="bron: facebook\nextern: abc@airbnb.com",
    )
    before = harness.calendar.get("fb-1")
    serve(aioclient_mock, feed())
    await sync(harness)
    assert harness.calendar.get("fb-1") == before


@FEEDS
async def test_diagnostics_hide_feed_links(
    harness: Harness, aioclient_mock: AiohttpClientMocker
) -> None:
    from custom_components.gastpagina.diagnostics import async_get_config_entry_diagnostics

    serve(aioclient_mock, feed(STAY))
    await sync(harness)
    text = str(await async_get_config_entry_diagnostics(harness.hass, harness.entry))
    assert "geheim" not in text
    assert "4821" not in text
    for token in (await links(harness)).values():
        assert token.rsplit("/", 2)[-2] not in text


# --- weekend surcharge: Friday and Saturday nights, seasons override it -------------------------


def test_weekend_surcharge() -> None:
    config, problems = parse_rental(
        {
            "prijzen": {
                "standaard": 105,
                "weekendtoeslag_pct": 12,
                "seizoenen": [{"van": "12-19", "tot": "01-03", "per_nacht": 130}],
            }
        }
    )
    assert problems == []
    assert config.night_price(date(2026, 11, 19)) == 105  # Thursday
    assert config.night_price(date(2026, 11, 20)) == 118  # Friday: 117.60 rounded
    assert config.night_price(date(2026, 11, 21)) == 118  # Saturday
    assert config.night_price(date(2026, 11, 22)) == 105  # Sunday
    assert config.night_price(date(2026, 12, 25)) == 130  # Friday in a season


def test_weekend_surcharge_invalid() -> None:
    config, problems = parse_rental({"prijzen": {"standaard": 105, "weekendtoeslag_pct": 500}})
    assert not config.enabled
    assert [p.issue_id for p in problems] == ["rental_invalid"]


# --- extra guests and the weekly discount ---------------------------------------------------------

EXTRAS_YAML = """
verhuur:
  prijzen:
    standaard: 100
    extra_gast_pn: 6
    weekkorting_pct: 10
    schoonmaak: 50
"""


@pytest.mark.parametrize("devices_yaml", [DEVICES_YAML + EXTRAS_YAML])
async def test_price_extra_guests_and_week_discount(harness: Harness) -> None:
    resp = await harness.client.get(
        "/api/gastpagina/prijs?aankomst=2027-01-04&vertrek=2027-01-06&personen=3"
    )
    body = await resp.json()
    assert (body["logies"], body["extra_gasten"], body["weekkorting"]) == (200, 12, 0)
    assert body["totaal"] == 262  # two nights: no weekly discount
    resp = await harness.client.get(
        "/api/gastpagina/prijs?aankomst=2027-01-04&vertrek=2027-01-11&personen=4"
    )
    body = await resp.json()
    assert (body["logies"], body["extra_gasten"], body["weekkorting"]) == (700, 84, 78.4)
    assert body["totaal"] == 755.6  # 10% off the nights and the extra guests, not the cleaning
