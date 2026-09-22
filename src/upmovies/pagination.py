"""Keyset cursors, for the surfaces that page over a list that grows while it is read.

One codec rather than one per surface. The admin resolution queue (D-25) minted the first one
and the entity `/events` endpoints (EF-18, NEU-1440) need the same thing over the same key
shape — a `timestamptz` and a UUID — so it lives here, at the package root, where a public read
path can import it without pulling `link.resolve` (and, behind it, the scorer) along.

The token is opaque on purpose: the ordering key is the *surface's* business, and a client that
learns to construct one will keep constructing it after the key changes.
"""

import base64
import binascii
from datetime import datetime
from uuid import UUID


class InvalidCursor(ValueError):
    """The `cursor` query parameter was not one this module minted."""


def encode_cursor(sort_key: datetime, row_id: UUID) -> str:
    """Pack a row's `(timestamp, id)` sort key into the token the next request sends back."""
    raw = f"{sort_key.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Unpack a cursor, or raise `InvalidCursor`.

    Every malformed shape lands on the same exception, because from the caller's side they
    are one mistake: a token this module did not mint. The caller turns that into a 400.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        timestamp, _, row_id = raw.partition("|")
        sort_key = datetime.fromisoformat(timestamp)
        if sort_key.tzinfo is None:
            # The keys this pages over are `timestamptz`; a naive value would compare at
            # whatever instant the driver assumed and quietly page from the wrong place.
            # `encode_cursor` never mints one, so this is a forgery, which is the same mistake
            # as the rest.
            raise ValueError("naive timestamp")
        return sort_key, UUID(row_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor(cursor) from exc
