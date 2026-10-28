"""Configuration, repairs, scenes, sessions and the management side in HA (§4, §5, §6, §9)."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
from custom_components.gastpagina.diagnostics import async_get_config_entry_diagnostics

from .conftest import CHECK_IN, CHECK_OUT, CODE, DEVICES_YAML, STATION_URL, TITLE, Harness


def _issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    registry = ir.async_get(hass)
    return {issue_id: i for (domain, issue_id), i in registry.issues.items() if domain == DOMAIN}


# --- config file ---------------------------------------------------------------

BROKEN_YAML = """
rooms:
  - key: living
    name: Living
    lights:
      - { key: plafond, label: Plafond, entity: light.living_plafond, dimmable: true }
      - { key: deur, label: Deur, entity: lock.voordeur }
      - { key: weg, label: Weg, entity: light.bestaat_niet }
      - { key: plafond, label: Dubbel, entity: light.iets_anders }
      - { label: Zonder sleutel, entity: light.zonder }
"""


@pytest.mark.parametrize("devices_yaml", [BROKEN_YAML])
async def test_bad_devices_raise_repairs_and_the_rest_works(harness: Harness) -> None:
    issues = _issues(harness.hass)
    assert {i.translation_key for i in issues.values()} == {
        "wrong_domain",
        "missing_entity",
        "duplicate_key",
        "device_invalid",
    }
    config = await (await harness.get("config")).json()
    keys = [light["key"] for light in config["rooms"][0]["lights"]]
    assert keys == ["plafond", "weg"]  # weg exists in the config but reports unavailable

    token = await harness.token()
    state = await (await harness.get("state", token)).json()
    assert state["living.weg"]["available"] is False
    assert (await harness.act(token, "living.deur", "on"))[0] == 400
    assert (await harness.act(token, "living.weg", "on"))[0] == 409
    assert harness.calls == []


@pytest.mark.parametrize("devices_yaml", ["rooms: [ this is not: valid"])
async def test_unparsable_file(harness: Harness) -> None:
    assert "config_invalid" in _issues(harness.hass)
    assert (await harness.get("config")).status == 200


async def test_reload_service(harness: Harness, tmp_path: Any) -> None:
    (tmp_path / CONFIG_FILE).write_text(DEVICES_YAML.replace("label: Plafond", "label: Luster"))
    await harness.hass.services.async_call(DOMAIN, "reload", blocking=True)
    config = await (await harness.get("config")).json()
    assert config["rooms"][0]["lights"][0]["label"] == "Luster"


async def test_reload_keeps_previous_devices_when_file_breaks(
    harness: Harness, tmp_path: Any
) -> None:
    (tmp_path / CONFIG_FILE).write_text("rooms: [ broken")
    await harness.hass.services.async_call(DOMAIN, "reload", blocking=True)
    assert "config_invalid" in _issues(harness.hass)
    config = await (await harness.get("config")).json()
    assert config["rooms"][0]["key"] == "living"


async def test_public_config_shape(harness: Harness) -> None:
    config = await (await harness.get("config")).json()
    assert config == {
        "rooms": [
            {
                "key": "living",
                "name": "Living",
                "lights": [
                    {
                        "id": "living.plafond",
                        "key": "plafond",
                        "label": "Plafond",
                        "dimmable": True,
                    },
                    {"id": "living.bollen", "key": "bollen", "label": "Bollen", "dimmable": False},
                ],
                "covers": [{"id": "living.zeekant", "key": "zeekant", "label": "Zeekant"}],
                "climate": {"id": "living.warmte", "key": "warmte", "label": "Warmte living"},
            }
        ],
        "radio": {
            "id": "radio",
            "key": "radio",
            "label": "Radio living",
            "max_volume": 0.6,
            "stations": [{"key": "mnm", "label": "MNM"}],
        },
        "climate_limits": {"min": 16.0, "max": 22.0, "step": 0.5},
        "scenes": {
            "slaapwel": {
                "lights_off": True,
                "covers_close": True,
                "climate_night": 17.0,
                "radio_stop": True,
            },
            "alles_uit": {
                "lights_off": True,
                "covers_close": False,
                "climate_night": None,
                "radio_stop": True,
            },
        },
    }


# --- codes ---------------------------------------------------------------------


async def test_short_code_is_ignored_with_repair(harness: Harness) -> None:
    harness.calendar.add(uid="kort", summary="Korte code", code="ABC-12")
    await harness.tick(timedelta(seconds=61))
    issues = _issues(harness.hass)
    (short,) = [i for i in issues.values() if i.translation_key == "code_too_short"]
    assert short.translation_placeholders["title"] == "Korte code"
    assert (await harness.login("ABC-12"))[0] == 401


async def test_duplicate_code_refuses_both(harness: Harness) -> None:
    token = await harness.token()
    harness.calendar.add(
        uid="booking-2",
        summary="Tweede boeking",
        start=CHECK_OUT - timedelta(hours=2),
        end=CHECK_OUT + timedelta(days=2),
    )
    await harness.tick(timedelta(seconds=61))
    assert any(i.translation_key == "duplicate_code" for i in _issues(harness.hass).values())
    assert (await harness.login())[0] == 401
    assert (await harness.get("state", token)).status == 401


async def test_issues_clear_when_fixed(harness: Harness) -> None:
    harness.calendar.add(uid="kort", summary="Korte code", code="ABC-12")
    await harness.tick(timedelta(seconds=61))
    assert _issues(harness.hass)
    harness.calendar.events.pop()
    await harness.tick(timedelta(seconds=61))
    assert not _issues(harness.hass)


async def test_event_without_code_is_no_booking(harness: Harness) -> None:
    harness.calendar.events.clear()
    harness.calendar.add(code=None)
    await harness.tick(timedelta(seconds=61))
    assert harness.hass.states.get("binary_sensor.gastpagina_boeking_actief").state == "off"


@pytest.mark.parametrize(
    "entry_data",
    [
        {
            CONF_CALENDAR: "calendar.gasten",
            CONF_HOURS_BEFORE: 2,
            CONF_HOURS_AFTER: 3,
            CONF_MIN_CODE_LENGTH: 10,
            CONF_MAX_ATTEMPTS: 5,
        }
    ],
)
async def test_window_follows_options(harness: Harness) -> None:
    await harness.move_to(CHECK_IN - timedelta(hours=3))
    assert (await harness.login())[0] == 401
    await harness.move_to(CHECK_IN - timedelta(hours=2))
    assert (await harness.login())[0] == 200
    await harness.move_to(CHECK_OUT + timedelta(hours=2, minutes=59))
    assert (await harness.login())[0] == 200
    await harness.move_to(CHECK_OUT + timedelta(hours=3))
    assert (await harness.login())[0] == 401


async def test_missing_calendar(harness: Harness) -> None:
    await harness.hass.data["calendar"].async_remove_entity("calendar.gasten")
    await harness.tick(timedelta(seconds=61))
    assert "calendar_missing" in _issues(harness.hass)
    await harness.tick(timedelta(minutes=6))  # cache is no longer trusted
    assert (await harness.login())[0] == 401


# --- sessions --------------------------------------------------------------------


async def test_at_most_ten_sessions_per_booking(harness: Harness) -> None:
    tokens = [await harness.token() for _ in range(11)]
    assert (await harness.get("state", tokens[0])).status == 401
    assert (await harness.get("state", tokens[1])).status == 200
    assert (await harness.get("state", tokens[10])).status == 200
    assert harness.hass.states.get("sensor.gastpagina_actieve_sessies").state == "10"


async def test_logout(harness: Harness) -> None:
    token = await harness.token()
    resp = await harness.client.post("/api/gastpagina/logout", headers={TOKEN_HEADER: token})
    assert resp.status == 204
    assert (await harness.get("state", token)).status == 401


async def test_extending_a_booking_extends_its_sessions(harness: Harness) -> None:
    token = await harness.token()
    harness.calendar.events.clear()
    harness.calendar.add(end=CHECK_OUT + timedelta(days=1))
    await harness.tick(timedelta(seconds=61))
    await harness.move_to(CHECK_OUT + timedelta(hours=1))
    assert (await harness.get("state", token)).status == 200


async def test_revoke_per_booking(harness: Harness) -> None:
    token = await harness.token()
    response = await harness.hass.services.async_call(
        DOMAIN, "revoke_sessions", {"booking": "andere gast"}, blocking=True, return_response=True
    )
    assert response == {"revoked": 0}
    assert (await harness.get("state", token)).status == 200
    response = await harness.hass.services.async_call(
        DOMAIN, "revoke_sessions", {"booking": TITLE.lower()}, blocking=True, return_response=True
    )
    assert response == {"revoked": 1}
    assert (await harness.get("state", token)).status == 401


# --- scenes ----------------------------------------------------------------------


async def test_scene_slaapwel(harness: Harness) -> None:
    hass = harness.hass
    hass.states.async_set(
        "media_player.radio", "playing", {"volume_level": 0.3, "media_content_id": STATION_URL}
    )
    hass.states.async_set("climate.slaapkamer_uit", "off")
    token = await harness.token()
    status, body = await harness.act(token, "scene", "slaapwel")
    assert status == 200
    assert body["ok"] is True
    assert set(body["previous"]) == {"living.plafond", "living.zeekant", "living.warmte", "radio"}
    assert body["previous"]["living.plafond"]["brightness"] == 128
    assert sorted((c.domain, c.service) for c in harness.calls) == [
        ("climate", "set_temperature"),
        ("cover", "close_cover"),
        ("light", "turn_off"),
        ("media_player", "media_stop"),
    ]
    assert harness.calls_for("climate", "set_temperature")[0].data["temperature"] == 17.0


async def test_scene_alles_uit_leaves_covers_and_heat(harness: Harness) -> None:
    token = await harness.token()
    status, body = await harness.act(token, "scene", "alles_uit")
    assert status == 200
    assert set(body["previous"]) == {"living.plafond"}
    assert [(c.domain, c.service) for c in harness.calls] == [("light", "turn_off")]


# --- entities, notifications, diagnostics ---------------------------------------------


async def test_entities(harness: Harness) -> None:
    hass = harness.hass
    assert hass.states.get("binary_sensor.gastpagina_boeking_actief").state == "on"
    assert hass.states.get("sensor.gastpagina_actieve_sessies").state == "0"
    assert hass.states.get("sensor.gastpagina_foute_pogingen").state == "0"
    assert hass.states.get("switch.gastpagina_toegang").state == "on"

    await harness.token()
    await harness.login("FOUTE-CODE-0000")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.gastpagina_actieve_sessies").state == "1"
    assert hass.states.get("sensor.gastpagina_foute_pogingen").state == "1"

    await harness.tick(timedelta(hours=1, minutes=1))
    assert hass.states.get("sensor.gastpagina_foute_pogingen").state == "0"
    await harness.move_to(CHECK_OUT + timedelta(minutes=1))
    assert hass.states.get("binary_sensor.gastpagina_boeking_actief").state == "off"
    assert hass.states.get("sensor.gastpagina_actieve_sessies").state == "0"


async def test_diagnostics_hide_secrets(harness: Harness) -> None:
    await harness.token()
    diag = await async_get_config_entry_diagnostics(harness.hass, harness.entry)
    text = json.dumps(diag)
    assert diag["sessions"] == 1
    assert diag["bookings_valid_now"] == 1
    for secret in (CODE, "zeemonster2026", TITLE, STATION_URL):
        assert secret not in text


async def test_global_lock(harness: Harness) -> None:
    runtime = harness.hass.data[DOMAIN]
    for n in range(50):
        await runtime.async_login("FOUTE-CODE-0000", f"10.0.{n // 250}.{n % 250}")
    status, body = await harness.login()
    assert status == 429
    assert body["retry_after"] > 0
    assert f"{DOMAIN}_global_lock" in harness.hass.data["persistent_notification"]


# --- config flow -------------------------------------------------------------------


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CALENDAR: "calendar.gasten",
            CONF_HOURS_BEFORE: 24,
            CONF_HOURS_AFTER: 0,
            CONF_MIN_CODE_LENGTH: 10,
            CONF_MAX_ATTEMPTS: 5,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CALENDAR] == "calendar.gasten"


async def test_options_flow(harness: Harness) -> None:
    hass = harness.hass
    result = await hass.config_entries.options.async_init(harness.entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CALENDAR: "calendar.gasten",
            CONF_HOURS_BEFORE: 1,
            CONF_HOURS_AFTER: 0,
            CONF_MIN_CODE_LENGTH: 10,
            CONF_MAX_ATTEMPTS: 5,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await harness.move_to(CHECK_IN - timedelta(hours=2))
    assert (await harness.login())[0] == 401


async def test_single_instance(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_CALENDAR: "calendar.gasten"}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT


async def test_code_in_any_case_from_calendar(harness: Harness) -> None:
    harness.calendar.events.clear()
    harness.calendar.add(code="zee monster 2026")
    await harness.tick(timedelta(seconds=61))
    assert (await harness.login(CODE))[0] == 200


def test_oostende_file_parses_cleanly() -> None:
    from pathlib import Path

    from homeassistant.util.yaml import load_yaml

    from custom_components.gastpagina.devices import parse_config

    path = Path(__file__).parent.parent / "config" / CONFIG_FILE
    config = parse_config(load_yaml(path))
    assert config.problems == []
    assert len(config.rooms) == 7
    assert len(config.devices) == 22  # 14 lights, 4 covers, 3 thermostats, 1 radio
    assert len(config.stations) == 6
    assert config.devices["badkamer.plafond"].domain == "switch"
    assert not config.devices["badkamer.plafond"].dimmable


async def test_new_booking_works_at_once(harness: Harness) -> None:
    """A miss rereads a calendar older than 10 s, so a fresh booking needs no wait."""
    harness.calendar.add(uid="nieuw", summary="Net geboekt", code="NIEUWE-GAST-777")
    assert (await harness.login("NIEUWE-GAST-777"))[0] == 401  # cache is seconds old
    harness.freezer.tick(timedelta(seconds=11))
    assert (await harness.login("NIEUWE-GAST-777"))[0] == 200
