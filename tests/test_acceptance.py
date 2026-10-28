"""Acceptance criteria 1-14 from the requirements (§12), one test (or a few) each."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import Any

from homeassistant.components.http.ban import KEY_BAN_MANAGER
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.gastpagina.const import (
    DOMAIN,
    EVENT_ACTION,
    EVENT_LOGIN,
    STORAGE_KEY,
    TOKEN_HEADER,
)

from .conftest import (
    CHECK_IN,
    CHECK_OUT,
    CODE,
    ENTITY_IDS,
    STATION_URL,
    TITLE,
    Harness,
    read_event_within,
)

# --- 1. valid code inside the window -> token; outside -> 401 ---------------


async def test_valid_code_gives_token(harness: Harness) -> None:
    status, body = await harness.login()
    assert status == 200
    assert set(body) == {"token", "expires_at"}
    assert len(body["token"]) >= 40
    assert body["expires_at"] == CHECK_OUT.isoformat()


async def test_code_is_case_space_and_dash_insensitive(harness: Harness) -> None:
    status, _ = await harness.login("zee monster-2026")
    assert status == 200


async def test_code_outside_window_is_refused(harness: Harness) -> None:
    await harness.move_to(CHECK_IN - timedelta(hours=24, minutes=1))
    status, body = await harness.login()
    assert status == 401
    assert body == {"error": "code"}


async def test_wrong_code_is_refused(harness: Harness) -> None:
    status, body = await harness.login("NIET-DE-JUISTE-CODE")
    assert status == 401
    assert body == {"error": "code"}


# --- 2. 24 h before check-in works; 1 min after the end nothing works --------


async def test_window_edges(harness: Harness) -> None:
    await harness.move_to(CHECK_IN - timedelta(hours=24))
    status, body = await harness.login()
    assert status == 200
    token = body["token"]

    await harness.move_to(CHECK_OUT + timedelta(minutes=1))
    resp = await harness.get("state", token)
    assert resp.status == 401
    assert await resp.json() == {"error": "sessie"}
    status, _ = await harness.login()
    assert status == 401


# --- 3. deleted booking or changed code -> sessions gone within 60 s --------


async def test_deleting_booking_ends_sessions(harness: Harness) -> None:
    token = await harness.token()
    harness.calendar.events.clear()
    await harness.tick(timedelta(seconds=61))
    resp = await harness.get("state", token)
    assert resp.status == 401


async def test_changing_code_ends_sessions(harness: Harness) -> None:
    token = await harness.token()
    harness.calendar.events.clear()
    harness.calendar.add(code="NIEUWE-CODE-12345")
    await harness.tick(timedelta(seconds=61))
    assert (await harness.get("state", token)).status == 401
    status, _ = await harness.login("NIEUWE-CODE-12345")
    assert status == 200


async def test_calendar_change_is_seen_without_timer(harness: Harness) -> None:
    """Even when the periodic refresh is late, a request older than 60 s refetches."""
    token = await harness.token()
    harness.calendar.events.clear()
    harness.freezer.tick(timedelta(seconds=61))  # no timer fired
    assert (await harness.get("state", token)).status == 401


# --- 4. 6th wrong attempt within 15 min -> 429; afterwards possible again ----


async def test_rate_limit(harness: Harness) -> None:
    for _ in range(5):
        status, _ = await harness.login("FOUTE-CODE-0000")
        assert status == 401
    status, body = await harness.login()  # even the right code is refused now
    assert status == 429
    assert body["error"] == "te_veel_pogingen"
    assert 0 < body["retry_after"] <= 15 * 60

    await harness.tick(timedelta(minutes=15, seconds=1))
    status, _ = await harness.login()
    assert status == 200


async def test_rate_limit_notifies_owner(harness: Harness) -> None:
    for _ in range(5):
        await harness.login("FOUTE-CODE-0000")
    notifications = harness.hass.data["persistent_notification"]
    assert any(n.startswith(DOMAIN) for n in notifications)


# --- 5. unknown device or action -> 400 and nothing happens -----------------


@pytest.mark.parametrize(
    ("device", "action", "value"),
    [
        ("living.onbekend", "on", {}),
        ("light.living_plafond", "on", {}),  # entity ids are not device keys
        ("living.plafond", "explode", {}),
        ("living.plafond", "brightness", {"value": 0}),
        ("living.plafond", "brightness", {"value": 101}),
        ("living.plafond", "brightness", {"value": "50"}),
        ("living.bollen", "brightness", {"value": 50}),  # switch is not dimmable
        ("living.plafond", "on", {"value": 1}),
        ("living.warmte", "target", {"value": "warm"}),
        ("living.warmte", "target", {"value": True}),
        ("living.warmte", "power", {"value": "max"}),
        ("radio", "play", {"value": "onbekende_zender"}),
        ("radio", "play", {"value": STATION_URL}),  # urls from the client are refused
        ("radio", "volume", {"value": 1.5}),
        ("scene", "feest", {}),
    ],
)
async def test_invalid_actions_are_refused(
    harness: Harness, device: str, action: str, value: dict[str, Any]
) -> None:
    token = await harness.token()
    status, _ = await harness.act(token, device, action, **value)
    assert status == 400
    assert harness.calls == []


async def test_unknown_fields_are_refused(harness: Harness) -> None:
    token = await harness.token()
    resp = await harness.client.post(
        "/api/gastpagina/action",
        json={"device": "living.plafond", "action": "on", "entity_id": "lock.voordeur"},
        headers={TOKEN_HEADER: token},
    )
    assert resp.status == 400
    resp = await harness.client.post("/api/gastpagina/login", json={"code": CODE, "user": "admin"})
    assert resp.status == 400
    assert harness.calls == []


async def test_action_without_session(harness: Harness) -> None:
    status, body = await harness.act(None, "living.plafond", "on")
    assert status == 401
    assert body == {"error": "sessie"}
    assert harness.calls == []


async def test_unavailable_device_gives_409(harness: Harness) -> None:
    token = await harness.token()
    harness.hass.states.async_set("light.living_plafond", "unavailable")
    status, _ = await harness.act(token, "living.plafond", "on")
    assert status == 409
    assert harness.calls == []


# --- 6. clamping ---------------------------------------------------------------


@pytest.mark.parametrize(("asked", "set_to"), [(30, 22.0), (10, 16.0), (19.3, 19.5), (20, 20.0)])
async def test_target_is_rounded_and_clamped(harness: Harness, asked: float, set_to: float) -> None:
    token = await harness.token()
    status, _ = await harness.act(token, "living.warmte", "target", value=asked)
    assert status == 200
    (call,) = harness.calls_for("climate", "set_temperature")
    assert call.data["temperature"] == set_to
    assert call.data["entity_id"] == "climate.living"


async def test_volume_is_capped(harness: Harness) -> None:
    token = await harness.token()
    status, _ = await harness.act(token, "radio", "volume", value=0.9)
    assert status == 200
    (call,) = harness.calls_for("media_player", "volume_set")
    assert call.data["volume_level"] == 0.6


async def test_power_sets_hvac_mode(harness: Harness) -> None:
    token = await harness.token()
    await harness.act(token, "living.warmte", "power", value="off")
    await harness.act(token, "living.warmte", "power", value="on")
    modes = [c.data["hvac_mode"] for c in harness.calls_for("climate", "set_hvac_mode")]
    assert modes == ["off", "heat"]


# --- 7. switch lights use switch.*, not light.* --------------------------------


async def test_switch_light_uses_switch_domain(harness: Harness) -> None:
    token = await harness.token()
    status, _ = await harness.act(token, "living.bollen", "on")
    assert status == 200
    assert [(c.domain, c.service) for c in harness.calls] == [("switch", "turn_on")]
    assert harness.calls[0].data["entity_id"] == "switch.living_bollen"


async def test_light_actions(harness: Harness) -> None:
    token = await harness.token()
    await harness.act(token, "living.plafond", "toggle")
    await harness.act(token, "living.plafond", "brightness", value=40)
    await harness.act(token, "living.zeekant", "close")
    assert [(c.domain, c.service) for c in harness.calls] == [
        ("light", "toggle"),
        ("light", "turn_on"),
        ("cover", "close_cover"),
    ]
    assert harness.calls[1].data["brightness_pct"] == 40


# --- 8. play uses the url from the config; the url never leaves the server -----


async def test_play_station(harness: Harness) -> None:
    token = await harness.token()
    status, _ = await harness.act(token, "radio", "play", value="mnm")
    assert status == 200
    (call,) = harness.calls_for("media_player", "play_media")
    assert call.data["media_content_id"] == STATION_URL
    assert call.data["media_content_type"] == "music"

    resp = await harness.get("config")
    assert resp.status == 200
    text = await resp.text()
    assert "icecast" not in text
    assert json.loads(text)["radio"]["stations"] == [{"key": "mnm", "label": "MNM"}]


async def test_station_is_reported_back(harness: Harness) -> None:
    token = await harness.token()
    harness.hass.states.async_set(
        "media_player.radio", "playing", {"volume_level": 0.3, "media_content_id": STATION_URL}
    )
    state = await (await harness.get("state", token)).json()
    assert state["radio"] == {
        "available": True,
        "state": "playing",
        "volume": 0.3,
        "station": "mnm",
    }


# --- 9. no entity ids, codes, names, history or other entities -----------------


async def test_nothing_leaks(harness: Harness) -> None:
    harness.hass.states.async_set("person.vorige_gast", "home")
    _, login = await harness.login()
    token = login["token"]
    bodies = [json.dumps(login)]
    for path in ("config", "state"):
        resp = await harness.get(path, token)
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == "no-store"
        bodies.append(await resp.text())
    _, action = await harness.act(token, "scene", "slaapwel")
    bodies.append(json.dumps(action))

    joined = "\n".join(bodies)
    for secret in (*ENTITY_IDS, "calendar.gasten", "person.", CODE, "zeemonster2026", TITLE):
        assert secret.lower() not in joined.lower(), secret


async def test_state_model(harness: Harness) -> None:
    token = await harness.token()
    state = await (await harness.get("state", token)).json()
    assert state == {
        "living.plafond": {"available": True, "state": "on", "brightness": 128},
        "living.bollen": {"available": True, "state": "off", "brightness": 255},
        "living.zeekant": {"available": True, "position": 100, "moving": None, "state": "open"},
        "living.warmte": {
            "available": True,
            "state": "heat",
            "current": 19.5,
            "target": 20,
            "action": "idle",
        },
        "radio": {"available": True, "state": "idle", "volume": 0.2, "station": None},
    }


async def test_state_model_edge_cases(harness: Harness) -> None:
    token = await harness.token()
    hass = harness.hass
    hass.states.async_set("cover.living_zeekant", "closing", {"current_position": 40})
    hass.states.async_set("climate.living", "off", {"current_temperature": 18})
    hass.states.async_remove("light.living_plafond")
    state = await (await harness.get("state", token)).json()
    assert state["living.zeekant"] == {
        "available": True,
        "position": 40,
        "moving": "close",
        "state": "moving",
    }
    assert state["living.warmte"]["state"] == "off"
    assert state["living.plafond"]["available"] is False


# --- 10. an Authorization header never leads to a HA ban --------------------------


@pytest.mark.parametrize("http_config", [{"ip_ban_enabled": True, "login_attempts_threshold": 1}])
async def test_authorization_header_does_not_ban(harness: Harness) -> None:
    bad = {"Authorization": "Bearer gastcode-in-de-verkeerde-header"}
    client = harness.client
    for _ in range(3):
        assert (await client.get("/api/gastpagina/state", headers=bad)).status == 401
        assert (await client.get("/api/gastpagina/config", headers=bad)).status == 200
        resp = await client.post(
            "/api/gastpagina/login", json={"code": "FOUT-0000000"}, headers=bad
        )
        assert resp.status == 401
    assert not client.app[KEY_BAN_MANAGER].ip_bans_lookup
    assert (await client.get("/api/gastpagina/config")).status == 200

    # Control: the ban machinery is live in this test; a real HA view (the
    # calendar API) bans after one wrong Authorization header.
    assert (await client.get("/api/calendars", headers=bad)).status == 401
    assert (await client.get("/api/gastpagina/config")).status == 403


# --- 11. access switch off -> everything except config refused -------------------


async def test_access_switch(harness: Harness) -> None:
    hass = harness.hass
    token = await harness.token()
    switch = hass.data["switch"].get_entity("switch.gastpagina_toegang")
    await switch.async_turn_off()
    await hass.async_block_till_done()
    assert hass.states.get("switch.gastpagina_toegang").state == "off"

    assert (await harness.get("config")).status == 200
    assert (await harness.get("state", token)).status == 503
    assert (await harness.get("stream", token)).status == 503
    assert (await harness.login())[0] == 503
    assert (await harness.act(token, "living.plafond", "on"))[0] == 503
    assert harness.calls == []

    await switch.async_turn_on()
    assert (await harness.get("state", token)).status == 200


# --- 12. sessions survive a restart ------------------------------------------------


async def test_sessions_survive_restart(harness: Harness, hass_storage: dict[str, Any]) -> None:
    hass = harness.hass
    token = await harness.token()
    assert await hass.config_entries.async_unload(harness.entry.entry_id)
    assert DOMAIN not in hass.data
    stored = json.dumps(hass_storage[STORAGE_KEY])
    assert token not in stored
    assert CODE not in stored

    assert await hass.config_entries.async_setup(harness.entry.entry_id)
    await hass.async_block_till_done()
    assert (await harness.get("state", token)).status == 200


# --- 13. the stream delivers a physical change within a second ---------------------


async def test_stream_update(harness: Harness) -> None:
    token = await harness.token()
    async with harness.client.get("/api/gastpagina/stream", headers={TOKEN_HEADER: token}) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        name, snapshot = await read_event_within(resp, 1)
        assert name == "snapshot"
        assert snapshot["living.plafond"]["state"] == "on"

        harness.hass.states.async_set("light.living_plafond", "off")
        name, update = await read_event_within(resp, 1)
        assert name is None
        assert update == {
            "device": "living.plafond",
            "state": {"available": True, "state": "off", "brightness": 0},
        }


async def test_stream_ticket_is_single_use(harness: Harness) -> None:
    token = await harness.token()
    resp = await harness.client.post("/api/gastpagina/stream-ticket", headers={TOKEN_HEADER: token})
    assert resp.status == 200
    ticket = (await resp.json())["ticket"]
    async with harness.client.get(f"/api/gastpagina/stream?ticket={ticket}") as stream:
        assert stream.status == 200
        name, _ = await read_event_within(stream, 1)
        assert name == "snapshot"
    again = await harness.client.get(f"/api/gastpagina/stream?ticket={ticket}")
    assert again.status == 401


async def test_stream_ends_when_session_is_revoked(harness: Harness) -> None:
    token = await harness.token()
    async with harness.client.get("/api/gastpagina/stream", headers={TOKEN_HEADER: token}) as resp:
        await read_event_within(resp, 1)
        await harness.hass.services.async_call(DOMAIN, "revoke_sessions", blocking=True)
        name, _ = await read_event_within(resp, 1)
        assert name == "sessie"


# --- 14. logbook shows "gastpagina", never a user; no code in logs -----------------


async def test_context_and_logs(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    actions = async_capture_events(harness.hass, EVENT_ACTION)
    logins = async_capture_events(harness.hass, EVENT_LOGIN)

    await harness.login("FOUTE-CODE-0000")
    token = await harness.token()
    await harness.act(token, "living.plafond", "off")
    await harness.hass.async_block_till_done()

    (call,) = harness.calls
    (event,) = actions
    assert call.context.user_id is None
    assert call.context.id == event.context.id  # logbook attributes the change to the event
    assert event.data == {"device": "living.plafond", "action": "off"}
    assert len(logins) == 1
    assert logins[0].data == {}

    for secret in (CODE, "zeemonster2026", "FOUTE-CODE-0000", TITLE, token):
        assert secret not in caplog.text


async def test_logbook_description(hass: HomeAssistant) -> None:
    from homeassistant.core import Event

    from custom_components.gastpagina import logbook

    described: dict[str, Any] = {}
    logbook.async_describe_events(
        hass, lambda domain, event_type, fn: described.setdefault(event_type, fn)
    )
    entry = described[EVENT_ACTION](
        Event(EVENT_ACTION, {"device": "living.plafond", "action": "off"})
    )
    assert entry["name"] == "Gastpagina"
    assert "living.plafond" in entry["message"]
    assert described[EVENT_LOGIN](Event(EVENT_LOGIN, {}))["name"] == "Gastpagina"
