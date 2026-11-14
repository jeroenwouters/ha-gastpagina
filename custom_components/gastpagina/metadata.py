"""`sleutel: waarde` lines in a calendar event's description.

The bookings calendar is the hub's only store. Each event carries its metadata as
lines like `bron: airbnb` or `status: optie`; the owner's own text stays above them
and the `code:` line stays at the bottom.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Final

from .bookings import code_value

SOURCE: Final = "bron"
EXTERNAL: Final = "extern"
STATUS: Final = "status"
KIND: Final = "soort"
RESERVATION: Final = "reservatie"
PHONE4: Final = "tel4"
GUESTS: Final = "personen"
EMAIL: Final = "email"
PHONE: Final = "telefoon"
REQUESTED: Final = "aangevraagd"
BLOCKS: Final = "blokkeert"

KEYS: Final = (
    SOURCE,
    EXTERNAL,
    STATUS,
    KIND,
    RESERVATION,
    PHONE4,
    GUESTS,
    EMAIL,
    PHONE,
    REQUESTED,
    BLOCKS,
)
# Personal data, removed some weeks after the stay.
PERSONAL: Final = (PHONE4, EMAIL, PHONE, RESERVATION)

STATUS_OPTION: Final = "optie"
STATUS_CONFIRMED: Final = "bevestigd"
KIND_BLOCK: Final = "blokkade"

_KEYS = "|".join(KEYS)
_LINE = re.compile(rf"^[ \t]*({_KEYS})[ \t]*:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_CODE = re.compile(r"^[ \t]*code[ \t]*:.*$", re.IGNORECASE | re.MULTILINE)


def read(description: str | None) -> dict[str, str]:
    """The known metadata of an event; the first line of each key counts."""
    found: dict[str, str] = {}
    for match in _LINE.finditer(description or ""):
        found.setdefault(match.group(1).casefold(), match.group(2))
    return found


def write(description: str | None, updates: Mapping[str, str | None]) -> str:
    """Set (or with None remove) metadata lines; the owner's text and the code stay.

    New lines go after the existing metadata, above the `code:` line.
    """
    lines = (description or "").split("\n")
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        match = _LINE.fullmatch(line)
        if match is None:
            kept.append(line)
            continue
        key = match.group(1).casefold()
        if key not in updates:
            kept.append(line)
        elif key not in seen and updates[key] is not None:
            kept.append(f"{key}: {_clean(updates[key])}")
        seen.add(key)
    new = [f"{k}: {_clean(v)}" for k, v in updates.items() if k not in seen and v is not None]
    if new:
        code_at = next((i for i, line in enumerate(kept) if _CODE.fullmatch(line)), None)
        if code_at is None:
            while kept and not kept[-1].strip():
                kept.pop()
            kept.extend(new)
        else:
            kept[code_at:code_at] = new
    text = "\n".join(kept).strip("\n")
    return text


def _clean(value: str | None) -> str:
    """One line, no surprises."""
    return " ".join((value or "").split())[:200]


def is_option(meta: Mapping[str, str]) -> bool:
    """A website request that the owner has not confirmed yet."""
    return meta.get(STATUS, "").casefold() == STATUS_OPTION


def blocks(meta: Mapping[str, str]) -> bool:
    """Whether the event takes the dates off the market (default: yes)."""
    return meta.get(BLOCKS, "").casefold() not in ("nee", "no", "false")


def has_code(description: str | None) -> bool:
    """Whether a `code:` line is present (any value)."""
    return code_value(description) is not None
