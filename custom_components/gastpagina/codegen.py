"""Readable guest codes like ZEE-4821-MONSTER, from `secrets` only."""

from __future__ import annotations

import secrets

from .bookings import normalize_code
from .words_nl import WORDS

_MAX_TRIES = 100


def _candidate(prefix: str) -> str:
    first = secrets.choice(WORDS)
    second = secrets.choice(WORDS)
    while second == first:
        second = secrets.choice(WORDS)
    parts = [first, f"{secrets.randbelow(10_000):04d}", second]
    if prefix:
        parts.insert(0, prefix.upper())
    return "-".join(parts)


def generate(*, taken: set[str], min_length: int, prefix: str = "") -> str:
    """A new code whose normalized form is not in `taken` and is long enough.

    Two words of 300 and four digits: about 29.7 bits.
    """
    for _ in range(_MAX_TRIES):
        code = _candidate(prefix)
        while len(normalize_code(code)) < min_length:
            code += "-" + secrets.choice(WORDS)
        if normalize_code(code) not in taken:
            return code
    raise RuntimeError("no unused code found")
