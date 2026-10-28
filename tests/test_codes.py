"""Automatic codes (v0.2): acceptance criteria 16-26 and the details around them."""

from __future__ import annotations

from datetime import date, timedelta
import json
import logging
import re
from typing import Any
from unittest.mock import patch

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.gastpagina import codegen
from custom_components.gastpagina.bookings import normalize_code
from custom_components.gastpagina.const import (
    CONF_AUTO_CODES,
    CONF_CALENDAR,
    CONF_CODE_PREFIX,
    CONF_HOURS_AFTER,
    CONF_HOURS_BEFORE,
    CONF_LOOKAHEAD_DAYS,
    CONF_MAX_ATTEMPTS,
    CONF_MIN_CODE_LENGTH,
    CONF_NOTIFY_NEW_CODE,
    DOMAIN,
    EVENT_CODE_CREATED,
    TOKEN_HEADER,
)
from custom_components.gastpagina.diagnostics import async_get_config_entry_diagnostics
from custom_components.gastpagina.words_nl import WORDS

from .conftest import CHECK_IN, CHECK_OUT, CODE, Harness

CODE_LINE = re.compile(r"^code: (.+)$", re.MULTILINE)
FORMAT = re.compile(r"^(?:[A-Z0-9]{1,6}-)?[A-Z]{3,7}-\d{4}-[A-Z]{3,7}$")
NEW = {"uid": "nieuw", "summary": "Familie Janssens"}


def _code(harness: Harness, uid: str) -> str:
    description = harness.calendar.get(uid).description or ""
    codes = CODE_LINE.findall(description)
    assert len(codes) == 1, description
    return codes[0]


def _issue_keys(harness: Harness) -> set[str]:
    registry = ir.async_get(harness.hass)
    return {i.translation_key for (d, _), i in registry.issues.items() if d == DOMAIN}


def _entry(**overrides: Any) -> dict[str, Any]:
    return {
        CONF_CALENDAR: "calendar.gasten",
        CONF_HOURS_BEFORE: 24,
        CONF_HOURS_AFTER: 0,
        CONF_MIN_CODE_LENGTH: 10,
        CONF_MAX_ATTEMPTS: 5,
        **overrides,
    }


# --- 16. new booking gets a code line at the bottom, nothing else changes ------------------------


async def test_new_booking_gets_a_code(harness: Harness) -> None:
    harness.calendar.add(
        **NEW,
        start=CHECK_OUT + timedelta(days=10),
        end=CHECK_OUT + timedelta(days=14),
        code=None,
        description="Aankomst rond 17u\nHond mee",
        location="Zeedijk 1",
    )
    before = harness.calendar.get("nieuw")
    await harness.tick(timedelta(seconds=61))

    after = harness.calendar.get("nieuw")
    code = _code(harness, "nieuw")
    assert FORMAT.match(code), code
    assert after.description == f"Aankomst rond 17u\nHond mee\ncode: {code}"
    assert (after.summary, after.start, after.end, after.location, after.uid) == (
        before.summary,
        before.start,
        before.end,
        before.location,
        before.uid,
    )
    (update,) = harness.calendar.updates
    assert update[0] == "nieuw"


async def test_booking_beyond_lookahead_waits(harness: Harness) -> None:
    harness.calendar.add(
        **NEW, start=CHECK_IN + timedelta(days=100), end=CHECK_IN + timedelta(days=103), code=None
    )
    await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []


async def test_past_booking_is_left_alone(harness: Harness) -> None:
    harness.calendar.add(
        **NEW, start=CHECK_IN - timedelta(days=9), end=CHECK_IN - timedelta(days=7), code=None
    )
    await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []


# --- 17. the generated code works at once, in any spelling ---------------------------------------


async def test_generated_code_works_at_once(harness: Harness) -> None:
    harness.calendar.add(**NEW, start=CHECK_IN, end=CHECK_OUT, code=None)
    response = await harness.hass.services.async_call(
        DOMAIN, "fill_codes", blocking=True, return_response=True
    )
    assert response == {"created": 1}
    code = _code(harness, "nieuw")
    sloppy = code.lower().replace("-", " ")
    assert (await harness.login(sloppy))[0] == 200


# --- 18. code: geen -> no code, no access, no warnings -------------------------------------------


@pytest.mark.parametrize("opt_out", ["geen", "GEEN", "-"])
async def test_opt_out(harness: Harness, opt_out: str) -> None:
    harness.calendar.add(uid="nieuw", summary="Poetsdag", code=opt_out)
    await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []
    assert _code(harness, "nieuw") == opt_out
    assert (await harness.login(opt_out))[0] == 401
    assert not _issue_keys(harness)


# --- 19. own code stays --------------------------------------------------------------------------


async def test_own_code_is_kept(harness: Harness) -> None:
    harness.calendar.add(**NEW, code="mijn eigen code 42")
    await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []
    assert (await harness.login("MIJN-EIGEN-CODE-42"))[0] == 200


# --- 20. all-day and recurring events: no code, a repair issue -----------------------------------


async def test_all_day_event(harness: Harness) -> None:
    harness.calendar.add(**NEW, start=date(2026, 11, 20), end=date(2026, 11, 23), code=None)
    await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []
    assert "all_day_booking" in _issue_keys(harness)

    harness.calendar.events.pop()
    await harness.tick(timedelta(seconds=61))
    assert "all_day_booking" not in _issue_keys(harness)


async def test_recurring_event(harness: Harness) -> None:
    harness.calendar.add(uid="nieuw", summary="Wekelijks", rrule="FREQ=WEEKLY", code=None)
    await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []
    assert "recurring_booking" in _issue_keys(harness)


async def test_all_day_with_opt_out_raises_nothing(harness: Harness) -> None:
    harness.calendar.add(**NEW, start=date(2026, 11, 20), end=date(2026, 11, 21), code="geen")
    await harness.tick(timedelta(seconds=61))
    assert not _issue_keys(harness)


# --- 21. read-only calendar ----------------------------------------------------------------------


@pytest.mark.parametrize("calendar_writable", [False])
async def test_read_only_calendar(harness: Harness) -> None:
    harness.calendar.add(**NEW, code=None)
    for _ in range(3):
        await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []
    registry = ir.async_get(harness.hass)
    readonly = [i for (d, _), i in registry.issues.items() if d == DOMAIN]
    assert [i.translation_key for i in readonly] == ["calendar_read_only"]
    assert (await harness.login())[0] == 200


# --- 22. no loop, no second code -----------------------------------------------------------------


async def test_no_second_code(harness: Harness) -> None:
    harness.calendar.add(**NEW, code=None)
    for _ in range(3):
        await harness.tick(timedelta(seconds=61))
    _code(harness, "nieuw")  # exactly one line
    assert len(harness.calendar.updates) == 1


# --- 23. generate_code replaces a leaked code ----------------------------------------------------


async def test_generate_code(harness: Harness) -> None:
    token = await harness.token()
    response = await harness.hass.services.async_call(
        DOMAIN, "generate_code", {"booking": "familie peeters"}, blocking=True, return_response=True
    )
    new = response["code"]
    assert FORMAT.match(new)
    assert _code(harness, "booking-1") == new
    assert harness.calendar.get("booking-1").description.startswith("Twee volwassenen, een kind\n")
    assert (await harness.get("state", token)).status == 401
    assert (await harness.login())[0] == 401
    assert (await harness.login(new))[0] == 200


async def test_generate_code_by_uid_and_errors(harness: Harness) -> None:
    response = await harness.hass.services.async_call(
        DOMAIN, "generate_code", {"booking": "booking-1"}, blocking=True, return_response=True
    )
    assert FORMAT.match(response["code"])
    with pytest.raises(ServiceValidationError):
        await harness.hass.services.async_call(
            DOMAIN, "generate_code", {"booking": "niemand"}, blocking=True, return_response=True
        )
    harness.calendar.add(
        uid="tweede", start=CHECK_OUT + timedelta(days=3), end=CHECK_OUT + timedelta(days=5)
    )
    with pytest.raises(ServiceValidationError):  # two bookings with the same title
        await harness.hass.services.async_call(
            DOMAIN,
            "generate_code",
            {"booking": "Familie Peeters"},
            blocking=True,
            return_response=True,
        )


# --- 24. the event carries the code; logs, diagnostics and entities do not -----------------------


async def test_code_only_in_event(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    events = async_capture_events(harness.hass, EVENT_CODE_CREATED)
    harness.calendar.add(**NEW, code=None)
    await harness.tick(timedelta(seconds=61))

    code = _code(harness, "nieuw")
    (event,) = events
    assert event.data == {
        "booking": "Familie Janssens",
        "start": CHECK_IN.isoformat(),
        "end": CHECK_OUT.isoformat(),
        "code": code,
    }
    assert code not in caplog.text
    diagnostics = json.dumps(await async_get_config_entry_diagnostics(harness.hass, harness.entry))
    assert code not in diagnostics
    for state in harness.hass.states.async_all():
        assert code not in json.dumps({**state.attributes, "state": state.state}, default=str)


async def test_notification_without_code(harness: Harness) -> None:
    harness.calendar.add(**NEW, code=None)
    await harness.tick(timedelta(seconds=61))
    code = _code(harness, "nieuw")
    notifications = harness.hass.data["persistent_notification"]
    created = [n for n in notifications.values() if "Familie Janssens" in n["message"]]
    assert len(created) == 1
    assert "03-11-2026" in created[0]["message"]
    assert code not in json.dumps(created[0], default=str)


@pytest.mark.parametrize("entry_data", [_entry(**{CONF_NOTIFY_NEW_CODE: False})])
async def test_notification_can_be_switched_off(harness: Harness) -> None:
    harness.calendar.add(**NEW, code=None)
    await harness.tick(timedelta(seconds=61))
    notifications = harness.hass.data.get("persistent_notification", {})
    assert not [n for n in notifications.values() if "Familie Janssens" in n["message"]]


# --- 25. 1000 codes: unique, right format, long enough -------------------------------------------


def test_thousand_codes() -> None:
    codes: set[str] = set()
    for _ in range(1000):
        code = codegen.generate(taken=codes, min_length=10)
        assert FORMAT.match(code), code
        assert len(normalize_code(code)) >= 10
        codes.add(normalize_code(code))
    assert len(codes) == 1000


def test_prefix_and_long_minimum() -> None:
    code = codegen.generate(taken=set(), min_length=10, prefix="zm")
    assert code.startswith("ZM-")
    assert FORMAT.match(code)
    long = codegen.generate(taken=set(), min_length=30)
    assert len(normalize_code(long)) >= 30


def test_collision_is_regenerated() -> None:
    first = codegen.generate(taken=set(), min_length=10)
    picks = iter([first, first, "DUIN-0937-KREEFT"])
    with patch.object(codegen, "_candidate", lambda prefix: next(picks)):
        assert codegen.generate(taken={normalize_code(first)}, min_length=10) == "DUIN-0937-KREEFT"


def test_word_list() -> None:
    assert len(WORDS) >= 256
    assert len(set(WORDS)) == len(WORDS)
    for word in WORDS:
        assert re.fullmatch(r"[A-Z]{3,7}", word), word

    def close(a: str, b: str) -> bool:
        """Edit distance at most one."""
        if abs(len(a) - len(b)) > 1:
            return False
        if len(a) == len(b):
            return sum(x != y for x, y in zip(a, b, strict=True)) <= 1
        short, longer = sorted((a, b), key=len)
        return any(longer[:i] + longer[i + 1 :] == short for i in range(len(longer)))

    pairs = [(a, b) for i, a in enumerate(WORDS) for b in WORDS[i + 1 :] if close(a, b)]
    assert pairs == []
    assert "GEEN" not in WORDS


# --- 26. switched off -> no codes ----------------------------------------------------------------


@pytest.mark.parametrize("entry_data", [_entry(**{CONF_AUTO_CODES: False})])
async def test_auto_codes_off(harness: Harness) -> None:
    harness.calendar.add(**NEW, code=None)
    await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []
    response = await harness.hass.services.async_call(
        DOMAIN, "fill_codes", blocking=True, return_response=True
    )
    assert response == {"created": 0}


# --- details -------------------------------------------------------------------------------------


@pytest.mark.parametrize("entry_data", [_entry(**{CONF_CODE_PREFIX: "ZM"})])
async def test_prefix_option(harness: Harness) -> None:
    harness.calendar.add(**NEW, code=None)
    await harness.tick(timedelta(seconds=61))
    assert _code(harness, "nieuw").startswith("ZM-")


@pytest.mark.parametrize("entry_data", [_entry(**{CONF_LOOKAHEAD_DAYS: 5})])
async def test_lookahead_option(harness: Harness) -> None:
    harness.calendar.add(
        **NEW, start=CHECK_IN + timedelta(days=6), end=CHECK_IN + timedelta(days=8), code=None
    )
    await harness.tick(timedelta(seconds=61))
    assert harness.calendar.updates == []


async def test_write_failures(harness: Harness) -> None:
    harness.calendar.fail_updates = 3
    harness.calendar.add(**NEW, code=None)
    for _ in range(2):
        await harness.tick(timedelta(seconds=61))
    assert "code_write_failed" not in _issue_keys(harness)
    await harness.tick(timedelta(seconds=61))
    assert "code_write_failed" in _issue_keys(harness)
    await harness.tick(timedelta(seconds=61))  # fourth try succeeds
    assert "code_write_failed" not in _issue_keys(harness)
    _code(harness, "nieuw")


async def test_generated_code_avoids_existing_codes(harness: Harness) -> None:
    harness.calendar.add(**NEW, code=None)
    picks = iter([CODE, "DUIN-0937-KREEFT"])  # CODE belongs to booking-1
    with patch.object(codegen, "_candidate", lambda prefix: next(picks)):
        await harness.tick(timedelta(seconds=61))
    assert _code(harness, "nieuw") == "DUIN-0937-KREEFT"


async def test_new_code_on_page_after_session_revoke(harness: Harness) -> None:
    """A guest whose code was replaced is sent back to the code screen."""
    token = await harness.token()
    await harness.hass.services.async_call(
        DOMAIN, "generate_code", {"booking": "booking-1"}, blocking=True, return_response=True
    )
    resp = await harness.client.get("/api/gastpagina/state", headers={TOKEN_HEADER: token})
    assert resp.status == 401


async def test_options_flow_checks_prefix(harness: Harness) -> None:
    hass = harness.hass
    base = {
        CONF_CALENDAR: "calendar.gasten",
        CONF_HOURS_BEFORE: 24,
        CONF_HOURS_AFTER: 0,
        CONF_MIN_CODE_LENGTH: 10,
        CONF_MAX_ATTEMPTS: 5,
        CONF_AUTO_CODES: True,
        CONF_NOTIFY_NEW_CODE: True,
        CONF_LOOKAHEAD_DAYS: 90,
    }
    result = await hass.config_entries.options.async_init(harness.entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, CONF_CODE_PREFIX: "Z-M"}
    )
    assert result["errors"] == {CONF_CODE_PREFIX: "invalid_prefix"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, CONF_CODE_PREFIX: "zm"}
    )
    assert result["data"][CONF_CODE_PREFIX] == "ZM"
