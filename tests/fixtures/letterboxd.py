"""Builders for Letterboxd export payloads used by the import tests."""

import io
import zipfile
from typing import Any

WATCHLIST_HEADER = "Date,Name,Year,Letterboxd URI"
RATINGS_HEADER = "Date,Name,Year,Letterboxd URI,Rating"


def watchlist_csv(rows: list[tuple[str, Any]]) -> bytes:
    """`watchlist.csv` for `(name, year)` pairs. A `year` of `""` is Letterboxd's own spelling
    of "no year on record", which is why it is passed through rather than formatted."""
    lines = [WATCHLIST_HEADER]
    lines += [
        f"2026-01-0{i + 1},{name},{year},https://boxd.it/{i}" for i, (name, year) in enumerate(rows)
    ]
    return ("\n".join(lines) + "\n").encode()


def ratings_csv(rows: list[tuple[str, Any, Any]]) -> bytes:
    """`ratings.csv` for `(name, year, rating)` triples.

    Nothing imports these any more (EF-20). It survives as a fixture because refusing a
    ratings file rather than reading it as a watchlist is now behaviour with a test of its
    own: the two headers differ by one column, so a file this did not recognise would import
    somebody's whole viewing history as films they mean to see."""
    lines = [RATINGS_HEADER]
    lines += [
        f"2026-01-0{i + 1},{name},{year},https://boxd.it/{i},{rating}"
        for i, (name, year, rating) in enumerate(rows)
    ]
    return ("\n".join(lines) + "\n").encode()


def export_zip(members: dict[str, bytes]) -> bytes:
    """A zip holding exactly `members`, keyed by the name each takes inside it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()
