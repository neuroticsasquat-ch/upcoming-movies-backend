"""Reading a Letterboxd export: the zip, or one of the two CSVs out of it, into rows.

Pure — no DB, no network, no clock — so the whole of "is this a usable file?" is decided
before a job row exists and answered to the uploader as a 422 they can act on, rather than
surfacing minutes later as a failed job they have to poll to discover (spec §1).

Only `watchlist.csv` and `ratings.csv` are read. The export also carries diary entries,
reviews, lists, likes and comments; the spec puts all of them out of scope, and a member of
the zip we do not name is not an error — a Letterboxd export that grew a file is not a broken
one.

Neither CSV carries a TMDB id — the `Letterboxd URI` column is a link to *their* page, and
following it would mean scraping — so every row here is a title and a year that
`ingest.tmdb.resolution` has to place. That is why the rows come out of this module untouched:
whatever normalization matching needs belongs to the matcher, next to the hits it compares
them against."""

import csv
import io
import zipfile
from dataclasses import dataclass

from upmovies.app.errors import DomainError

WATCHLIST_MEMBER = "watchlist.csv"
RATINGS_MEMBER = "ratings.csv"

NAME_COLUMN = "Name"
YEAR_COLUMN = "Year"
RATING_COLUMN = "Rating"

MAX_ROWS_PER_FILE = 5_000
"""Spec §4. At the client's 40 req / 10 s this is ~20 minutes of work for one file, which the
job status makes visible; past it the user is better served by being told to split the export
than by a job that runs for an hour."""

MAX_MEMBER_BYTES = 32 * 1024 * 1024
"""What one member of the zip may expand to. The upload itself is capped at 5 MB by the route,
which bounds nothing about what is *inside* a zip: 5 MB of well-compressed CSV is gigabytes
expanded. Checked against the member's declared size before it is read, so a bomb is refused
rather than decompressed and then refused."""

PROMOTED_RATING = 4.0
"""The rating at or above which a film contributes its people (spec §3). Letterboxd rates in
halves from 0.5 to 5, so this is "four stars or better"."""


class InvalidImportFile(DomainError):
    """The upload is not a Letterboxd export this can read, and says why.

    The message is shown to the uploader — it is the whole value of validating synchronously —
    so it names what was wrong with *their* file and never an internal detail."""


@dataclass(frozen=True)
class WatchlistRow:
    """One `watchlist.csv` row: a film the user means to see."""

    name: str
    year: int | None


@dataclass(frozen=True)
class RatingRow:
    """One `ratings.csv` row. `rating` is None when the column was blank or unreadable, which
    sorts with the low ratings: neither is promoted, and neither costs a request."""

    name: str
    year: int | None
    rating: float | None

    @property
    def is_promoted(self) -> bool:
        return self.rating is not None and self.rating >= PROMOTED_RATING


@dataclass(frozen=True)
class LetterboxdExport:
    """What one upload turned out to contain. Either list may be empty — a user who uploads
    `watchlist.csv` alone has no ratings — but not both, which `parse_upload` refuses."""

    watchlist: tuple[WatchlistRow, ...] = ()
    ratings: tuple[RatingRow, ...] = ()

    @property
    def row_count(self) -> int:
        """Every row the runner will step through, promoted or not.

        Counts the ratings this import will skip without a request as well, because this is
        the denominator of the progress bar the UI polls (NEU-1358): a job whose `rows_total`
        excluded them would appear to stall at the watchlist and then leap to done."""
        return len(self.watchlist) + len(self.ratings)


def parse_upload(data: bytes) -> LetterboxdExport:
    """Read an upload into rows, or raise `InvalidImportFile` saying why it could not be.

    A zip is read by member name, a bare CSV by its header row — the asymmetry is the spec's
    (§1) and it is the right way round. Inside the export the names are Letterboxd's own and
    are the only thing distinguishing `ratings.csv` from `diary.csv`, whose headers are
    supersets of it; a file uploaded on its own has whatever name the user's browser gave it,
    which is routinely `watchlist (1).csv`."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        return _parse_zip(data)
    return _parse_single_csv(data)


def _parse_zip(data: bytes) -> LetterboxdExport:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = {}
        for info in archive.infolist():
            # Matched on the basename: an export is sometimes re-zipped with its contents in a
            # folder, and the member is then `letterboxd-user-2026-09-15/watchlist.csv`.
            name = info.filename.rsplit("/", 1)[-1].lower()
            if name in (WATCHLIST_MEMBER, RATINGS_MEMBER) and name not in members:
                if info.file_size > MAX_MEMBER_BYTES:
                    raise InvalidImportFile(f"{name} in the zip is too large to read")
                members[name] = archive.read(info)

    if not members:
        raise InvalidImportFile(
            "the zip contains neither watchlist.csv nor ratings.csv — upload a Letterboxd "
            "export, or one of those two files on its own"
        )
    watchlist = _read_watchlist(members[WATCHLIST_MEMBER]) if WATCHLIST_MEMBER in members else ()
    ratings = _read_ratings(members[RATINGS_MEMBER]) if RATINGS_MEMBER in members else ()
    return LetterboxdExport(watchlist=watchlist, ratings=ratings)


def _parse_single_csv(data: bytes) -> LetterboxdExport:
    header = _reader(data).fieldnames
    if header is None:
        raise InvalidImportFile("the file is empty")
    columns = {(c or "").strip() for c in header}
    if NAME_COLUMN not in columns or YEAR_COLUMN not in columns:
        raise InvalidImportFile(
            "the file does not look like a Letterboxd export: expected a header row with "
            f"{NAME_COLUMN!r} and {YEAR_COLUMN!r} columns"
        )
    if RATING_COLUMN in columns:
        return LetterboxdExport(ratings=_read_ratings(data))
    return LetterboxdExport(watchlist=_read_watchlist(data))


def _reader(data: bytes) -> csv.DictReader:
    # `utf-8-sig` because Letterboxd's exports carry a BOM, which would otherwise ride along on
    # the first header name and make `Name` unrecognisable. `replace` on the rest: a single bad
    # byte somewhere in a thousand-row library should cost that title its diacritic, not the
    # whole import.
    text = data.decode("utf-8-sig", errors="replace")
    return csv.DictReader(io.StringIO(text, newline=""))


def _read_watchlist(data: bytes) -> tuple[WatchlistRow, ...]:
    rows = [
        WatchlistRow(name=name, year=_year(raw))
        for raw in _rows(data, WATCHLIST_MEMBER)
        if (name := _name(raw)) is not None
    ]
    return tuple(rows)


def _read_ratings(data: bytes) -> tuple[RatingRow, ...]:
    rows = [
        RatingRow(name=name, year=_year(raw), rating=_rating(raw))
        for raw in _rows(data, RATINGS_MEMBER)
        if (name := _name(raw)) is not None
    ]
    return tuple(rows)


def _rows(data: bytes, what: str) -> list[dict[str, str | None]]:
    rows = list(_reader(data))
    if len(rows) > MAX_ROWS_PER_FILE:
        raise InvalidImportFile(
            f"{what} has {len(rows)} rows, more than the {MAX_ROWS_PER_FILE} this can import "
            "in one go"
        )
    return rows


def _name(row: dict[str, str | None]) -> str | None:
    """The film's title, or None for a row that has none — a trailing blank line, or an export
    truncated mid-write. Dropped rather than refused: one unreadable line should not cost the
    user the other thousand."""
    name = (row.get(NAME_COLUMN) or "").strip()
    return name or None


def _year(row: dict[str, str | None]) -> int | None:
    """The release year Letterboxd holds, or None when it holds none.

    A row without one is kept and goes on to be reported unmatched, because both matching
    rules in `ingest.tmdb.resolution` are year-equality rules and neither can be satisfied
    without one. It costs no request to find that out, which is why the row is not dropped
    here: the user is told the title could not be placed, which is true and actionable,
    rather than having it silently vanish between the file and the report."""
    try:
        return int((row.get(YEAR_COLUMN) or "").strip())
    except ValueError:
        return None


def _rating(row: dict[str, str | None]) -> float | None:
    try:
        return float((row.get(RATING_COLUMN) or "").strip())
    except ValueError:
        return None
