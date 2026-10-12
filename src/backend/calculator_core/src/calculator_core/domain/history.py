"""History ordering and the opaque, verifiable pagination cursor.

Order: `occurred_at` descending, ties broken by `calculation_id` descending. The cursor
holds the key of the last row of a page, so the next page is "every row strictly after it
in that order" (keyset pagination: no repeats, no skips, stable while rows are added).

A cursor is `<payload>.<tag>` (both base64url). The tag is an HMAC-SHA256 of the payload
under a key only this system knows, so a value the system did not issue is rejected.
"""

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from calculator_core.domain.calculation import Calculation
from calculator_core.domain.errors import InvalidCursorError

MIN_KEY_BYTES = 16


@dataclass(frozen=True)
class HistoryCursor:
    occurred_at: datetime
    calculation_id: UUID


def sort_key(calculation: Calculation) -> tuple[datetime, UUID]:
    """Key of the history order; sort with `reverse=True` (most recent first)."""
    return (calculation.occurred_at, calculation.id)


def cursor_for(calculation: Calculation) -> HistoryCursor:
    return HistoryCursor(calculation.occurred_at, calculation.id)


def is_after_cursor(calculation: Calculation, cursor: HistoryCursor) -> bool:
    """True when the row comes strictly later than the cursor in the history order."""
    return sort_key(calculation) < (cursor.occurred_at, cursor.calculation_id)


class CursorCodec:
    """Issues cursors and verifies that a received value is one this system issued."""

    def __init__(self, key: bytes) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"The cursor key must have at least {MIN_KEY_BYTES} bytes.")
        self._key = key

    def encode(self, cursor: HistoryCursor) -> str:
        payload = {
            "t": cursor.occurred_at.astimezone(UTC).isoformat(),
            "i": str(cursor.calculation_id),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return f"{_b64(raw)}.{_b64(self._tag(raw))}"

    def decode(self, value: str) -> HistoryCursor:
        """Decode a cursor produced by `encode`; anything else is an input error."""
        try:
            encoded_payload, encoded_tag = value.split(".")
            raw = _unb64(encoded_payload)
            tag = _unb64(encoded_tag)
        except binascii.Error, ValueError:
            raise InvalidCursorError() from None
        if not hmac.compare_digest(tag, self._tag(raw)):
            raise InvalidCursorError()
        return _parse_payload(raw)

    def _tag(self, raw: bytes) -> bytes:
        return hmac.new(self._key, raw, hashlib.sha256).digest()


def _parse_payload(raw: bytes) -> HistoryCursor:
    try:
        payload = json.loads(raw)
        occurred_at = datetime.fromisoformat(payload["t"])
        calculation_id = UUID(payload["i"])
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        raise InvalidCursorError() from error
    if occurred_at.tzinfo is None:
        raise InvalidCursorError()
    return HistoryCursor(occurred_at, calculation_id)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)
