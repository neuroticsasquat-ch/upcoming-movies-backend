"""The displayable-release diff: what `_rebuild_release_dates` throws away (NEU-1121)."""

from datetime import UTC, date, datetime

from upmovies.catalog.release_grade import displayable_regions, is_displayable_release
from upmovies.ingest.tmdb.release_date_history import (
    RELEASE_DATE_MOVED,
    RELEASE_DATE_SET,
    DisplayableRelease,
    diff_release_dates,
)


def rel(region: str = "US", rtype: int = 3, day: int = 1) -> DisplayableRelease:
    return DisplayableRelease(
        iso_3166_1=region, release_type=rtype, release_date=date(2027, 6, day)
    )


class TestDisplayableCut:
    def test_us_theatrical_is_displayable(self):
        assert is_displayable_release(iso_3166_1="US", release_type=3, origin_country=["GB"])

    def test_origin_country_theatrical_is_displayable(self):
        assert is_displayable_release(iso_3166_1="GB", release_type=2, origin_country=["GB"])

    def test_every_origin_country_counts_not_just_the_first(self):
        # The drift NEU-1121 fixes: the page used origin_country[0], the visibility
        # predicate used all of them.
        assert is_displayable_release(iso_3166_1="FR", release_type=3, origin_country=["GB", "FR"])

    def test_foreign_theatrical_is_not_displayable(self):
        # Cliffhanger: origin US, only release row is German. The page shows nothing.
        assert not is_displayable_release(iso_3166_1="DE", release_type=3, origin_country=["US"])

    def test_non_theatrical_types_are_not_displayable(self):
        for non_theatrical in (1, 4, 5, 6):  # premiere, digital, physical, TV
            assert not is_displayable_release(
                iso_3166_1="US", release_type=non_theatrical, origin_country=["US"]
            )

    def test_us_is_always_in_the_region_set(self):
        assert "US" in displayable_regions(None)
        assert "US" in displayable_regions(["JP"])


class TestFirstObservationIsABaseline:
    def test_previous_none_never_yields_changes(self):
        # The rule the module exists to guarantee (spec §5.3). A newly admitted film's
        # whole slate is recorded silently.
        assert diff_release_dates(previous=None, current=[rel(), rel("GB", 2, 5)]) == []

    def test_previous_empty_set_is_not_a_baseline(self):
        # Observed-and-held-nothing is a real state; a date arriving then is a real event.
        changes = diff_release_dates(previous=[], current=[rel()])
        assert [c.change for c in changes] == [RELEASE_DATE_SET]


class TestDiff:
    def test_new_date_where_none_existed_is_set(self):
        changes = diff_release_dates(
            previous=[rel("US", 3, 1)], current=[rel("US", 3, 1), rel("GB", 2, 4)]
        )
        assert len(changes) == 1
        assert changes[0].change == RELEASE_DATE_SET
        assert changes[0].release.iso_3166_1 == "GB"
        assert changes[0].previous_date is None

    def test_moved_date_carries_both_sides(self):
        changes = diff_release_dates(previous=[rel("US", 3, 1)], current=[rel("US", 3, 20)])
        assert len(changes) == 1
        assert changes[0].change == RELEASE_DATE_MOVED
        assert changes[0].previous_date == date(2027, 6, 1)
        assert changes[0].release.release_date == date(2027, 6, 20)

    def test_unchanged_date_yields_nothing(self):
        assert diff_release_dates(previous=[rel()], current=[rel()]) == []

    def test_withdrawn_date_yields_nothing(self):
        # Matches classify_field_change: "there would be no date to render".
        assert diff_release_dates(previous=[rel()], current=[]) == []

    def test_limited_and_wide_are_tracked_independently(self):
        # Same region, same film — two subjects, because the page lists them separately.
        changes = diff_release_dates(
            previous=[rel("US", 2, 1), rel("US", 3, 10)],
            current=[rel("US", 2, 5), rel("US", 3, 10)],
        )
        assert len(changes) == 1
        assert changes[0].release.release_type == 2

    def test_changes_are_ordered_stably(self):
        changes = diff_release_dates(
            previous=[rel("US", 3, 1), rel("GB", 2, 1)],
            current=[rel("US", 3, 9), rel("GB", 2, 9)],
        )
        assert [(c.release.iso_3166_1, c.release.release_type) for c in changes] == [
            ("GB", 2),
            ("US", 3),
        ]


class TestFromRows:
    def test_naive_and_aware_datetimes_both_reduce_to_a_date(self):
        # film_release_date.release_date is timestamptz; the diff compares dates.
        from upmovies.ingest.tmdb.release_date_history import displayable_from_rows

        rows = [("US", 3, datetime(2027, 6, 1, tzinfo=UTC))]
        assert displayable_from_rows(rows, origin_country=["US"]) == [
            DisplayableRelease("US", 3, date(2027, 6, 1))
        ]

    def test_undisplayable_rows_are_dropped(self):
        from upmovies.ingest.tmdb.release_date_history import displayable_from_rows

        rows = [
            ("DE", 3, datetime(2027, 6, 1, tzinfo=UTC)),
            ("US", 4, datetime(2027, 6, 1, tzinfo=UTC)),
        ]
        assert displayable_from_rows(rows, origin_country=["US"]) == []
