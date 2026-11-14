"""Just enough iCalendar for the channel feeds.

Airbnb and Booking.com publish one VEVENT per booking or blocked period, on whole
days. Parsing and writing that subset ourselves keeps the integration free of
extra requirements.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re

_ESCAPED = re.compile(r"\\([\\;,nN])")


class ICalError(ValueError):
    """The text is not a calendar."""


@dataclass(frozen=True, slots=True)
class FeedEvent:
    """One VEVENT, reduced to whole days (end exclusive)."""

    uid: str
    summary: str
    description: str
    start: date
    end: date


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return _ESCAPED.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _date(value: str) -> date:
    value = value.strip()
    if len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").date()
    # Date-time (with or without Z or TZID): the day is what counts here.
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").date()


def parse(text: str) -> list[FeedEvent]:
    """Events of a feed; raises ICalError when it is no calendar at all."""
    lines = _unfold(text)
    if not any(line.strip().upper() == "BEGIN:VCALENDAR" for line in lines):
        raise ICalError("no VCALENDAR")
    events: list[FeedEvent] = []
    current: dict[str, str] | None = None
    for line in lines:
        name, _, value = line.partition(":")
        key = name.split(";", 1)[0].strip().upper()
        if key == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {}
        elif key == "END" and value.strip().upper() == "VEVENT":
            if current is not None and (event := _event(current)) is not None:
                events.append(event)
            current = None
        elif current is not None and key and key not in current:
            current[key] = value
    return events


def _event(fields: dict[str, str]) -> FeedEvent | None:
    if fields.get("STATUS", "").strip().upper() == "CANCELLED":
        return None
    try:
        start = _date(fields["DTSTART"])
        end = _date(fields["DTEND"]) if "DTEND" in fields else start + timedelta(days=1)
    except (KeyError, ValueError):
        return None
    if end <= start:
        return None
    uid = _unescape(fields.get("UID", "")).strip()
    if not uid:
        uid = f"{start.isoformat()}/{end.isoformat()}"
    return FeedEvent(
        uid=uid[:200],
        summary=_unescape(fields.get("SUMMARY", "")).strip(),
        description=_unescape(fields.get("DESCRIPTION", "")),
        start=start,
        end=end,
    )


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Lines of at most 75 octets, continued with a space."""
    out: list[str] = []
    current = ""
    for char in line:
        if len((current + char).encode()) > 75:
            out.append(current)
            current = " " + char
        else:
            current += char
    out.append(current)
    return "\r\n".join(out)


@dataclass(frozen=True, slots=True)
class Block:
    """A busy period for an outgoing feed."""

    uid: str
    start: date
    end: date


def render(blocks: Iterable[Block], *, name: str, stamp: datetime) -> str:
    """A calendar of busy whole days, without any guest data."""
    stamp_text = stamp.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//gastpagina//kanalenhub//NL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
    ]
    for block in blocks:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_escape(block.uid)}",
            f"DTSTAMP:{stamp_text}",
            f"DTSTART;VALUE=DATE:{block.start:%Y%m%d}",
            f"DTEND;VALUE=DATE:{block.end:%Y%m%d}",
            "SUMMARY:Bezet",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
