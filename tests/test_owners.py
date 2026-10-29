"""Owner codes (v0.3): acceptance criteria 27-36 and the details around them."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
import re
from typing import Any
from unittest.mock import patch

from homeassistant.core import Event
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.gastpagina import codegen
from custom_components.gastpagina.bookings import normalize_code
from custom_components.gastpagina.const import (
    DOMAIN,
    EVENT_ACTION,
    EVENT_LOGIN,
    OWNER_STORAGE_KEY,
    TOKEN_HEADER,
)
from custom_components.gastpagina.diagnostics import async_get_config_entry_diagnostics

from .conftest import CHECK_OUT, CODE, Harness

FORMAT = re.compile(r"^[A-Z]{3,7}-\d{4}-[A-Z]{3,7}$")
OWNER_CODE = "EIGEN-CODE-JEROEN-42"


async def _add(harness: Harness, name: str, code: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"name": name}
    if code is not None:
        data["code"] = code
    return await harness.hass.services.async_call(
        DOMAIN, "add_owner_code", data, blocking=True, return_response=True
    )


async def _owner_token(harness: Harness, code: str = OWNER_CODE) -> str:
    await _add(harness, "Jeroen", code)
    status, body = await harness.login(code)
    assert status == 200, body
    return body["token"]


def _state(harness: Harness, entity_id: str) -> str:
    return harness.hass.states.get(entity_id).state


# --- 27. generated owner code, shown once, works -------------------------------------------------


async def test_add_owner_code_generates_one(harness: Harness) -> None:
    response = await _add(harness, "Jeroen")
    assert response["name"] == "Jeroen"
    code = response["code"]
    assert FORMAT.match(code), code

    status, body = await harness.login(code.lower().replace("-", " "))
    assert status == 200
    assert body["role"] == "owner"
    assert set(body) == {"token", "expires_at", "role"}


async def test_given_code_is_not_echoed(harness: Harness) -> None:
    response = await _add(harness, "Partner", OWNER_CODE)
    assert response == {"name": "Partner"}


# --- 28. works without a booking, is not a booking -----------------------------------------------


async def test_owner_without_booking(harness: Harness) -> None:
    harness.calendar.events.clear()
    await harness.tick(timedelta(seconds=61))
    token = await _owner_token(harness)
    assert (await harness.get("state", token)).status == 200
    assert (await harness.act(token, "living.plafond", "off"))[0] == 200
    await harness.hass.async_block_till_done()
    assert _state(harness, "binary_sensor.gastpagina_boeking_actief") == "off"
    assert _state(harness, "sensor.gastpagina_actieve_sessies") == "0"
    assert _state(harness, "sensor.gastpagina_eigenaarssessies") == "1"


async def test_owner_outside_and_after_bookings(harness: Harness) -> None:
    token = await _owner_token(harness)
    await harness.move_to(CHECK_OUT + timedelta(days=3))
    assert (await harness.get("state", token)).status == 200
    assert (await harness.login(OWNER_CODE))[0] == 200


# --- 29. access switch off: owners yes, guests no ------------------------------------------------


async def test_owner_with_access_switch_off(harness: Harness) -> None:
    hass = harness.hass
    guest = await harness.token()
    owner = await _owner_token(harness)
    switch = hass.data["switch"].get_entity("switch.gastpagina_toegang")
    await switch.async_turn_off()
    await hass.async_block_till_done()

    assert (await harness.login())[0] == 503
    assert (await harness.get("state", guest)).status == 503
    assert (await harness.login(OWNER_CODE))[0] == 200
    assert (await harness.get("state", owner)).status == 200
    assert (await harness.act(owner, "living.bollen", "on"))[0] == 200
    resp = await harness.client.post("/api/gastpagina/stream-ticket", headers={TOKEN_HEADER: owner})
    assert resp.status == 200


async def test_wrong_code_with_switch_off_counts(harness: Harness) -> None:
    await _add(harness, "Jeroen", OWNER_CODE)
    switch = harness.hass.data["switch"].get_entity("switch.gastpagina_toegang")
    await switch.async_turn_off()
    for _ in range(5):
        assert (await harness.login("FOUTE-CODE-0000"))[0] == 401
    assert (await harness.login(OWNER_CODE))[0] == 429


# --- 30. list shows names only; storage has no readable code -------------------------------------


async def test_list_and_storage(harness: Harness, hass_storage: dict[str, Any]) -> None:
    generated = (await _add(harness, "Partner"))["code"]
    await _add(harness, "Jeroen", OWNER_CODE)
    await harness.login(OWNER_CODE)
    response = await harness.hass.services.async_call(
        DOMAIN, "list_owner_codes", blocking=True, return_response=True
    )
    owners = {o["name"]: o for o in response["owners"]}
    assert set(owners) == {"Jeroen", "Partner"}
    assert set(owners["Jeroen"]) == {"name", "created", "last_used"}
    assert owners["Jeroen"]["last_used"] is not None
    assert owners["Partner"]["last_used"] is None

    await harness.hass.async_block_till_done()
    await harness.tick(timedelta(seconds=2))  # let the delayed save run
    stored = json.dumps(hass_storage[OWNER_STORAGE_KEY]).lower()
    text = json.dumps(response).lower()
    for code in (OWNER_CODE, generated):
        for form in (code.lower(), normalize_code(code)):
            assert form not in stored
            assert form not in text


# --- 31. removing a code ends its sessions at once -----------------------------------------------


async def test_remove_owner_code(harness: Harness) -> None:
    token = await _owner_token(harness)
    await harness.hass.services.async_call(
        DOMAIN, "remove_owner_code", {"name": "jeroen"}, blocking=True
    )
    assert (await harness.get("state", token)).status == 401
    assert (await harness.login(OWNER_CODE))[0] == 401
    with pytest.raises(ServiceValidationError):
        await harness.hass.services.async_call(
            DOMAIN, "remove_owner_code", {"name": "Jeroen"}, blocking=True
        )


async def test_changing_code_ends_sessions(harness: Harness) -> None:
    token = await _owner_token(harness)
    await _add(harness, "Jeroen", "NIEUWE-EIGEN-CODE-7")
    assert (await harness.get("state", token)).status == 401
    assert (await harness.login(OWNER_CODE))[0] == 401
    assert (await harness.login("NIEUWE-EIGEN-CODE-7"))[0] == 200


# --- 32. revoke_sessions leaves owners alone unless asked ----------------------------------------


async def test_revoke_sessions_and_owners(harness: Harness) -> None:
    guest = await harness.token()
    owner = await _owner_token(harness)
    response = await harness.hass.services.async_call(
        DOMAIN, "revoke_sessions", blocking=True, return_response=True
    )
    assert response == {"revoked": 1}
    assert (await harness.get("state", guest)).status == 401
    assert (await harness.get("state", owner)).status == 200

    await harness.hass.services.async_call(
        DOMAIN, "revoke_sessions", {"owner": True}, blocking=True
    )
    assert (await harness.get("state", owner)).status == 401


# --- 33. 30 days, sliding ------------------------------------------------------------------------


async def test_owner_session_slides(harness: Harness) -> None:
    token = await _owner_token(harness)
    await harness.tick(timedelta(days=20))
    assert (await harness.get("state", token)).status == 200
    await harness.tick(timedelta(days=20))  # 40 days after login, 20 after last use
    assert (await harness.get("state", token)).status == 200
    await harness.tick(timedelta(days=30, minutes=1))
    assert (await harness.get("state", token)).status == 401


async def test_owner_session_survives_restart(harness: Harness) -> None:
    hass = harness.hass
    token = await _owner_token(harness)
    assert await hass.config_entries.async_unload(harness.entry.entry_id)
    assert await hass.config_entries.async_setup(harness.entry.entry_id)
    await hass.async_block_till_done()
    assert (await harness.get("state", token)).status == 200


# --- 34. same limits as guests -------------------------------------------------------------------


async def test_owner_limits(harness: Harness) -> None:
    token = await _owner_token(harness)
    assert (await harness.act(token, "living.warmte", "target", value=30))[0] == 200
    assert harness.calls_for("climate", "set_temperature")[0].data["temperature"] == 22.0
    assert (await harness.act(token, "lock.voordeur", "open"))[0] == 400


# --- 35. logbook and events ----------------------------------------------------------------------


async def test_owner_events_and_logbook(harness: Harness) -> None:
    logins = async_capture_events(harness.hass, EVENT_LOGIN)
    actions = async_capture_events(harness.hass, EVENT_ACTION)
    token = await _owner_token(harness)
    await harness.act(token, "living.plafond", "off")
    guest = await harness.token()
    await harness.act(guest, "living.plafond", "on")
    await harness.hass.async_block_till_done()

    assert logins[0].data == {"role": "owner", "owner": "Jeroen"}
    assert logins[1].data == {"role": "guest"}
    assert actions[0].data == {
        "device": "living.plafond",
        "action": "off",
        "role": "owner",
        "owner": "Jeroen",
    }
    assert actions[1].data == {"device": "living.plafond", "action": "on", "role": "guest"}

    from custom_components.gastpagina import logbook

    described: dict[str, Any] = {}
    logbook.async_describe_events(
        harness.hass, lambda domain, event_type, fn: described.setdefault(event_type, fn)
    )
    assert described[EVENT_ACTION](Event(EVENT_ACTION, actions[0].data))["name"] == (
        "Gastpagina (eigenaar Jeroen)"
    )
    assert described[EVENT_ACTION](Event(EVENT_ACTION, actions[1].data))["name"] == "Gastpagina"


# --- 36. generated booking codes never equal an owner code ---------------------------------------


async def test_generator_avoids_owner_codes(harness: Harness) -> None:
    await _add(harness, "Jeroen", "DUIN-0937-KREEFT")
    harness.calendar.add(uid="nieuw", summary="Familie Nieuw", code=None)
    picks = iter(["DUIN-0937-KREEFT", "ZEE-4821-MONSTER"])
    with patch.object(codegen, "_candidate", lambda prefix: next(picks)):
        await harness.tick(timedelta(seconds=61))
    assert harness.calendar.get("nieuw").description.endswith("code: ZEE-4821-MONSTER")


async def test_manual_booking_code_equal_to_owner_code(harness: Harness) -> None:
    await _add(harness, "Jeroen", OWNER_CODE)
    harness.calendar.add(uid="botsing", summary="Familie Botsing", code=OWNER_CODE.lower())
    await harness.tick(timedelta(seconds=61))
    registry = ir.async_get(harness.hass)
    keys = [i.translation_key for (d, _), i in registry.issues.items() if d == DOMAIN]
    assert "owner_code_clash" in keys
    status, body = await harness.login(OWNER_CODE)
    assert status == 200
    assert body["role"] == "owner"


# --- validation and details ----------------------------------------------------------------------


async def test_add_owner_code_validation(harness: Harness) -> None:
    with pytest.raises(ServiceValidationError):
        await _add(harness, "Jeroen", "KORT-1")  # shorter than the minimum length
    with pytest.raises(ServiceValidationError):
        await _add(harness, "Jeroen", CODE.lower())  # equals an active booking code
    await _add(harness, "Jeroen", OWNER_CODE)
    with pytest.raises(ServiceValidationError):
        await _add(harness, "Partner", OWNER_CODE.replace("-", " "))  # taken by Jeroen


async def test_owner_codes_never_logged(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    generated = (await _add(harness, "Partner"))["code"]
    token = await _owner_token(harness)
    await harness.act(token, "living.plafond", "off")
    diagnostics = json.dumps(await async_get_config_entry_diagnostics(harness.hass, harness.entry))
    for secret in (OWNER_CODE, generated, normalize_code(OWNER_CODE), token):
        assert secret not in caplog.text
        assert secret not in diagnostics
    assert "Jeroen" in diagnostics


async def test_owner_login_page_flow(harness: Harness) -> None:
    """The guest flows keep working next to owners: same endpoint, owners first."""
    await _add(harness, "Jeroen", OWNER_CODE)
    status, body = await harness.login()
    assert status == 200
    assert "role" not in body
    assert body["expires_at"] == CHECK_OUT.isoformat()


async def test_options_flow_lists_owner_names(harness: Harness) -> None:
    await _add(harness, "Jeroen", OWNER_CODE)
    await _add(harness, "Partner")
    result = await harness.hass.config_entries.options.async_init(harness.entry.entry_id)
    assert result["description_placeholders"]["owners"] == "Jeroen, Partner"
