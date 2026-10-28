"""Constants for the gastpagina integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "gastpagina"

CONF_CALENDAR: Final = "calendar_entity"
CONF_HOURS_BEFORE: Final = "hours_before"
CONF_HOURS_AFTER: Final = "hours_after"
CONF_MIN_CODE_LENGTH: Final = "min_code_length"
CONF_MAX_ATTEMPTS: Final = "max_attempts"

DEFAULT_CALENDAR: Final = "calendar.gasten"
DEFAULT_HOURS_BEFORE: Final = 24
DEFAULT_HOURS_AFTER: Final = 0
DEFAULT_MIN_CODE_LENGTH: Final = 10
DEFAULT_MAX_ATTEMPTS: Final = 5

CONFIG_FILE: Final = "gastpagina.yaml"

# Login throttling
ATTEMPT_WINDOW: Final = timedelta(minutes=15)
GLOBAL_FAILURE_LIMIT: Final = 50
GLOBAL_FAILURE_WINDOW: Final = timedelta(hours=1)
GLOBAL_LOCK: Final = timedelta(minutes=15)

# Calendar cache: refreshed every minute; not trusted at all after five.
CALENDAR_REFRESH: Final = timedelta(seconds=60)
CALENDAR_STALE: Final = timedelta(minutes=5)
CALENDAR_MARGIN: Final = timedelta(minutes=5)
# A wrong code rereads a cache older than this, so a booking entered moments ago works.
CALENDAR_RECHECK: Final = timedelta(seconds=10)
# How far ahead bookings are read, so clashing codes are reported early.
CALENDAR_LOOKAHEAD: Final = timedelta(days=7)

MAX_SESSIONS_PER_BOOKING: Final = 10
STREAM_KEEPALIVE: Final = 25.0  # seconds
TICKET_TTL: Final = timedelta(seconds=30)

# Never "Authorization": HA's own auth middleware reads that header.
TOKEN_HEADER: Final = "X-Gast-Token"  # noqa: S105 (a header name, not a secret)

EVENT_LOGIN: Final = "gastpagina_login"
EVENT_ACTION: Final = "gastpagina_action"

SIGNAL_UPDATE: Final = f"{DOMAIN}_update"
SIGNAL_RECHECK: Final = f"{DOMAIN}_recheck"
SIGNAL_CONFIG: Final = f"{DOMAIN}_config"

STORAGE_KEY: Final = f"{DOMAIN}.sessions"
STORAGE_VERSION: Final = 1

SERVICE_RELOAD: Final = "reload"
SERVICE_REVOKE_SESSIONS: Final = "revoke_sessions"
ATTR_BOOKING: Final = "booking"

SCENE_DEVICE: Final = "scene"
