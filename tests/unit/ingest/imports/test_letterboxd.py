"""Reading a Letterboxd export (D-15). Pure parsing — no DB, no network.

Since EF-20 the only member this reads is `watchlist.csv`. The ratings tests here are no
longer about promotion cuts; they are about a `ratings.csv` being *refused* rather than read
as a watchlist, which is what it would otherwise be — the headers differ by one column."""

import pytest

from tests.fixtures.letterboxd import export_zip, ratings_csv, watchlist_csv
from upmovies.ingest.imports.letterboxd import (
    MAX_ROWS_PER_FILE,
    InvalidImportFile,
    parse_upload,
)


def test_reads_the_watchlist_out_of_an_export_zip():
    data = export_zip(
        {
            "watchlist.csv": watchlist_csv([("Dune", 2021), ("Arrival", 2016)]),
            # Everything else in a real export, `ratings.csv` now among it. Out of scope, and
            # not an error — a zip that carries more than the watchlist is still an export.
            "ratings.csv": ratings_csv([("Heat", 1995, 4.5)]),
            "diary.csv": b"Date,Name,Year,Letterboxd URI,Rating,Rewatch\n",
            "profile.csv": b"Date Joined,Username\n",
        }
    )
    export = parse_upload(data)
    assert [(r.name, r.year) for r in export.watchlist] == [("Dune", 2021), ("Arrival", 2016)]
    assert export.row_count == 2


def test_the_ratings_in_a_zip_contribute_nothing():
    """EF-20: `ratings.csv` beside a watchlist is read past, not read.

    The zip is the common case — it is what Letterboxd's "Export your data" hands you — so the
    ratings must be inert here rather than refused, or every real export would be a 422."""
    with_ratings = export_zip(
        {
            "watchlist.csv": watchlist_csv([("Dune", 2021)]),
            "ratings.csv": ratings_csv([(f"Rated {i}", 1990 + i, 5.0) for i in range(50)]),
        }
    )
    without = export_zip({"watchlist.csv": watchlist_csv([("Dune", 2021)])})
    assert parse_upload(with_ratings) == parse_upload(without)


def test_reads_a_zip_whose_members_sit_in_a_folder():
    # An export re-zipped by a file manager, which is what a user who unzipped it to look
    # inside and then zipped it back produces.
    data = export_zip({"letterboxd-tom-2026-09-15/watchlist.csv": watchlist_csv([("Dune", 2021)])})
    assert [r.name for r in parse_upload(data).watchlist] == ["Dune"]


def test_a_bare_ratings_csv_is_refused_rather_than_read_as_a_watchlist():
    """The one thing that makes `RATING_COLUMN` still worth knowing about (EF-20).

    `ratings.csv` carries the same `Name` and `Year` columns a watchlist does, so a parser that
    simply stopped looking at the `Rating` column would import somebody's entire viewing
    history as films they mean to see — and every one of them outside the alert window."""
    with pytest.raises(InvalidImportFile) as excinfo:
        parse_upload(ratings_csv([("Heat", 1995, 4.5)]))
    assert "already watched" in str(excinfo.value)


def test_a_bare_watchlist_csv_is_identified_by_its_header_not_its_name():
    # The file arrives with whatever name the user's browser gave it, routinely
    # `watchlist (1).csv`.
    assert [r.name for r in parse_upload(watchlist_csv([("Dune", 2021)])).watchlist] == ["Dune"]


def test_a_utf8_bom_does_not_hide_the_header():
    # Letterboxd's exports carry one; decoded as plain utf-8 it rides along on `Name`.
    data = b"\xef\xbb\xbf" + watchlist_csv([("Amélie", 2001)])
    assert [r.name for r in parse_upload(data).watchlist] == ["Amélie"]


def test_a_blank_year_is_kept_as_none_rather_than_dropped():
    # The row goes on to be reported unmatched: no rule can place it, and the user is better
    # told that than left to wonder where the title went.
    export = parse_upload(watchlist_csv([("Untitled Project", "")]))
    assert [(r.name, r.year) for r in export.watchlist] == [("Untitled Project", None)]


def test_a_row_with_no_name_is_dropped():
    # A trailing blank line, or an export truncated mid-write. One unreadable line must not
    # cost the user the other thousand.
    data = watchlist_csv([("Dune", 2021), ("", 2016)])
    assert [r.name for r in parse_upload(data).watchlist] == ["Dune"]


def test_row_count_is_the_watchlist_alone():
    # It is the denominator of the progress bar the UI polls, and there is nothing else for
    # the runner to step through any more.
    export = parse_upload(watchlist_csv([("Heat", 1995), ("Dune", 2021)]))
    assert export.row_count == 2


def test_a_file_that_is_not_an_export_is_refused_with_a_reason():
    with pytest.raises(InvalidImportFile) as excinfo:
        parse_upload(b"one,two,three\n1,2,3\n")
    assert "Name" in str(excinfo.value)


def test_an_empty_file_is_refused():
    with pytest.raises(InvalidImportFile):
        parse_upload(b"")


def test_a_zip_with_no_watchlist_is_refused():
    with pytest.raises(InvalidImportFile) as excinfo:
        parse_upload(export_zip({"diary.csv": b"Date,Name\n"}))
    assert "watchlist.csv" in str(excinfo.value)


def test_a_zip_holding_only_ratings_is_refused():
    # A user who exported and then kept only the file they thought mattered. The message has
    # to say why, because the file is not corrupt — it is simply not imported any more.
    with pytest.raises(InvalidImportFile) as excinfo:
        parse_upload(export_zip({"ratings.csv": ratings_csv([("Heat", 1995, 4.5)])}))
    assert "watchlist.csv" in str(excinfo.value)
    assert "Ratings" in str(excinfo.value)


def test_a_file_past_the_row_cap_is_refused():
    rows = [(f"Film {i}", 2020) for i in range(MAX_ROWS_PER_FILE + 1)]
    with pytest.raises(InvalidImportFile) as excinfo:
        parse_upload(watchlist_csv(rows))
    assert str(MAX_ROWS_PER_FILE) in str(excinfo.value)


def test_a_file_at_the_row_cap_is_accepted():
    rows = [(f"Film {i}", 2020) for i in range(MAX_ROWS_PER_FILE)]
    assert len(parse_upload(watchlist_csv(rows)).watchlist) == MAX_ROWS_PER_FILE


def test_a_zip_bomb_is_refused_before_it_is_decompressed():
    # The 5 MB the route caps the *upload* at says nothing about what is inside a zip.
    data = export_zip({"watchlist.csv": b"Date,Name,Year\n" + b"x" * (33 * 1024 * 1024)})
    assert len(data) < 1024 * 1024  # it compresses to nothing, which is the whole problem
    with pytest.raises(InvalidImportFile) as excinfo:
        parse_upload(data)
    assert "too large" in str(excinfo.value)
