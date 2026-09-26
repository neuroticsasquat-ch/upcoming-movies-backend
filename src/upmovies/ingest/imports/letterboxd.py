"""Reading a Letterboxd export: the zip, or the watchlist CSV out of it, into rows.

Pure — no DB, no network, no clock — so the whole of "is this a usable file?" is decided
before a job row exists and answered to the uploader as a 422 they can act on, rather than
surfacing minutes later as a failed job they have to poll to discover (spec §1).

**Only `watchlist.csv` is read** (EF-20). The ratings path is deleted: a follow is binary now
(EF-1), so a four-star rating in 2019 would buy a follow that delivers every credit change of
somebody the user once enjoyed, which is not what rating a film says. The export also carries
diary entries, reviews, lists, likes and comments; those were always out of scope, and a member
of the zip we do not name is not an error — a Letterboxd export that grew a file is not a
broken one.

`ratings.csv` is the one unnamed member this still has to *recognise*, because it is not inert:
its header is a superset of the watchlist's, so an uploader who sends it on its own would
otherwise have their entire rated history read as a watchlist. It is refused with a reason
instead, which is the same bargain the rest of this module makes — the user learns what to
upload while they are still looking at the file.

`watchlist.csv` carries no TMDB id — the `Letterboxd URI` column is a link to *their* page, and
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

NAME_COLUMN = "Name"
YEAR_COLUMN = "Year"
RATING_COLUMN = "Rating"
"""Not a column this reads — the column that tells a bare `ratings.csv` apart from a bare
`watchlist.csv`, which is otherwise only knowable from a filename the user's browser chose."""

MAX_ROWS_PER_FILE = 5_000
"""Spec §4. At the client's 40 req / 10 s this is ~20 minutes of work for one file, which the
job status makes visible; past it the user is better served by being told to split the export
than by a job that runs for an hour."""

MAX_MEMBER_BYTES = 32 * 1024 * 1024
"""What one member of the zip may expand to. The upload itself is capped at 5 MB by the route,
which bounds nothing about what is *inside* a zip: 5 MB of well-compressed CSV is gigabytes
expanded. Checked against the member's declared size before it is read, so a bomb is refused
rather than decompressed and then refused."""


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
class LetterboxdExport:
    """What one upload turned out to contain: the rows of `watchlist.csv`, and nothing else.

    A file with no watchlist in it at all is refused by `parse_upload`. A watchlist that is
    merely *empty* is not — somebody whose watchlist has nothing on it has uploaded a valid
    export, and the job that reads it succeeds having done nothing, which is the truth."""

    watchlist: tuple[WatchlistRow, ...] = ()

    @property
    def row_count(self) -> int:
        """Every row the runner will step through — the denominator of the progress bar the UI
        polls (NEU-1358)."""
        return len(self.watchlist)


def parse_upload(data: bytes) -> LetterboxdExport:
    """Read an upload into rows, or raise `InvalidImportFile` saying why it could not be.

    A zip is read by member name, a bare CSV by its header row — the asymmetry is the spec's
    (§1) and it is the right way round. Inside the export the names are Letterboxd's own and
    are the only thing distinguishing `watchlist.csv` from `diary.csv`, whose header is a
    superset of it; a file uploaded on its own has whatever name the user's browser gave it,
    which is routinely `watchlist (1).csv`."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        return _parse_zip(data)
    return _parse_single_csv(data)


def _parse_zip(data: bytes) -> LetterboxdExport:
    watchlist: bytes | None = None
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            # Matched on the basename: an export is sometimes re-zipped with its contents in a
            # folder, and the member is then `letterboxd-user-2026-09-15/watchlist.csv`.
            name = info.filename.rsplit("/", 1)[-1].lower()
            if name == WATCHLIST_MEMBER and watchlist is None:
                if info.file_size > MAX_MEMBER_BYTES:
                    raise InvalidImportFile(f"{name} in the zip is too large to read")
                watchlist = archive.read(info)

    if watchlist is None:
        raise InvalidImportFile(
            "the zip contains no watchlist.csv — upload a Letterboxd export, or watchlist.csv "
            "on its own. Ratings are no longer imported."
        )
    return LetterboxdExport(watchlist=_read_watchlist(watchlist))


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
        # `ratings.csv` and `diary.csv` both land here, and both would otherwise read cleanly
        # as a watchlist — same `Name` and `Year` columns — and import somebody's whole
        # viewing history as films they mean to see.
        raise InvalidImportFile(
            "that file is a list of films you have already watched — ratings are no longer "
            "imported. Upload watchlist.csv, or the export zip Letterboxd sent you."
        )
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
