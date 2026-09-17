"""Reading a Letterboxd export (D-15). Pure parsing — no DB, no network."""

import pytest

from tests.fixtures.letterboxd import export_zip, ratings_csv, watchlist_csv
from upmovies.ingest.imports.letterboxd import (
    MAX_ROWS_PER_FILE,
    InvalidImportFile,
    parse_upload,
)


def test_reads_both_files_out_of_an_export_zip():
    data = export_zip(
        {
            "watchlist.csv": watchlist_csv([("Dune", 2021), ("Arrival", 2016)]),
            "ratings.csv": ratings_csv([("Heat", 1995, 4.5)]),
            # Everything else in a real export. Out of scope, and not an error.
            "diary.csv": b"Date,Name,Year,Letterboxd URI,Rating,Rewatch\n",
            "profile.csv": b"Date Joined,Username\n",
        }
    )
    export = parse_upload(data)
    assert [(r.name, r.year) for r in export.watchlist] == [("Dune", 2021), ("Arrival", 2016)]
    assert [(r.name, r.year, r.rating) for r in export.ratings] == [("Heat", 1995, 4.5)]
    assert export.row_count == 3


def test_reads_a_zip_whose_members_sit_in_a_folder():
    # An export re-zipped by a file manager, which is what a user who unzipped it to look
    # inside and then zipped it back produces.
    data = export_zip({"letterboxd-tom-2026-09-15/watchlist.csv": watchlist_csv([("Dune", 2021)])})
    assert [r.name for r in parse_upload(data).watchlist] == ["Dune"]


def test_a_bare_csv_is_identified_by_its_header_not_its_name():
    # The `Rating` column is the only difference, and the file arrives with whatever name the
    # user's browser gave it.
    assert parse_upload(watchlist_csv([("Dune", 2021)])).ratings == ()
    assert parse_upload(ratings_csv([("Dune", 2021, 3.0)])).watchlist == ()
    assert len(parse_upload(ratings_csv([("Dune", 2021, 3.0)])).ratings) == 1


def test_a_utf8_bom_does_not_hide_the_header():
    # Letterboxd's exports carry one; decoded as plain utf-8 it rides along on `Name`.
    data = b"\xef\xbb\xbf" + watchlist_csv([("Amélie", 2001)])
    assert [r.name for r in parse_upload(data).watchlist] == ["Amélie"]


def test_a_blank_year_is_kept_as_none_rather_than_dropped():
    # The row goes on to be reported unmatched: no rule can place it, and the user is better
    # told that than left to wonder where the title went.
    export = parse_upload(watchlist_csv([("Untitled Project", "")]))
    assert [(r.name, r.year) for r in export.watchlist] == [("Untitled Project", None)]


@pytest.mark.parametrize("rating", ["", "not-a-number"])
def test_an_unreadable_rating_sorts_with_the_low_ones(rating):
    # Watched but unrated. Not promoted, and deliberately not an error.
    (row,) = parse_upload(ratings_csv([("Heat", 1995, rating)])).ratings
    assert row.rating is None
    assert row.is_promoted is False


@pytest.mark.parametrize(
    ("rating", "promoted"), [(3.5, False), (4.0, True), (4.5, True), (5.0, True)]
)
def test_the_promotion_cut_is_four_stars_inclusive(rating, promoted):
    (row,) = parse_upload(ratings_csv([("Heat", 1995, rating)])).ratings
    assert row.is_promoted is promoted


def test_a_row_with_no_name_is_dropped():
    # A trailing blank line, or an export truncated mid-write. One unreadable line must not
    # cost the user the other thousand.
    data = watchlist_csv([("Dune", 2021), ("", 2016)])
    assert [r.name for r in parse_upload(data).watchlist] == ["Dune"]


def test_row_count_includes_the_ratings_the_import_will_skip():
    # It is the denominator of the progress bar the UI polls, so a job whose `rows_total`
    # excluded them would appear to stall and then leap to done.
    export = parse_upload(ratings_csv([("Heat", 1995, 1.0), ("Dune", 2021, 5.0)]))
    assert export.row_count == 2


def test_a_file_that_is_not_an_export_is_refused_with_a_reason():
    with pytest.raises(InvalidImportFile) as excinfo:
        parse_upload(b"one,two,three\n1,2,3\n")
    assert "Name" in str(excinfo.value)


def test_an_empty_file_is_refused():
    with pytest.raises(InvalidImportFile):
        parse_upload(b"")


def test_a_zip_with_neither_file_is_refused():
    with pytest.raises(InvalidImportFile) as excinfo:
        parse_upload(export_zip({"diary.csv": b"Date,Name\n"}))
    assert "watchlist.csv" in str(excinfo.value)


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
