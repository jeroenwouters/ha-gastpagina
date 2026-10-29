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
CONF_AUTO_CODES: Final = "auto_codes"
CONF_CODE_PREFIX: Final = "code_prefix"
CONF_NOTIFY_NEW_CODE: Final = "notify_new_code"
CONF_LOOKAHEAD_DAYS: Final = "lookahead_days"

DEFAULT_CALENDAR: Final = "calendar.gasten"
DEFAULT_HOURS_BEFORE: Final = 24
DEFAULT_HOURS_AFTER: Final = 0
DEFAULT_MIN_CODE_LENGTH: Final = 10
DEFAULT_MAX_ATTEMPTS: Final = 5
DEFAULT_AUTO_CODES: Final = True
DEFAULT_CODE_PREFIX: Final = ""
DEFAULT_NOTIFY_NEW_CODE: Final = True
DEFAULT_LOOKAHEAD_DAYS: Final = 90

# Failed writes of a generated code before the owner gets a repair issue.
CODE_WRITE_ATTEMPTS: Final = 3

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
# Carries the code itself: the only place outside the calendar where it appears.
EVENT_CODE_CREATED: Final = "gastpagina_code_created"

SIGNAL_UPDATE: Final = f"{DOMAIN}_update"
SIGNAL_RECHECK: Final = f"{DOMAIN}_recheck"
SIGNAL_CONFIG: Final = f"{DOMAIN}_config"

STORAGE_KEY: Final = f"{DOMAIN}.sessions"
OWNER_STORAGE_KEY: Final = f"{DOMAIN}.owners"

ROLE_GUEST: Final = "guest"
ROLE_OWNER: Final = "owner"
# Owner sessions last this long after their last use.
OWNER_SESSION_TTL: Final = timedelta(days=30)
OWNER_LAST_USED_EVERY: Final = timedelta(hours=1)
STORAGE_VERSION: Final = 1

SERVICE_RELOAD: Final = "reload"
SERVICE_REVOKE_SESSIONS: Final = "revoke_sessions"
SERVICE_GENERATE_CODE: Final = "generate_code"
SERVICE_FILL_CODES: Final = "fill_codes"
SERVICE_ADD_OWNER_CODE: Final = "add_owner_code"
SERVICE_REMOVE_OWNER_CODE: Final = "remove_owner_code"
SERVICE_LIST_OWNER_CODES: Final = "list_owner_codes"
ATTR_BOOKING: Final = "booking"
ATTR_NAME: Final = "name"
ATTR_CODE: Final = "code"
ATTR_OWNER: Final = "owner"

SCENE_DEVICE: Final = "scene"
