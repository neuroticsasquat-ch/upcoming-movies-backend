from upmovies.catalog.release_grade import RELEASE_TYPE_BUCKETS
from upmovies.public.release import (
    RELEASE_BUCKET_LABELS,
    RELEASE_BUCKETS,
    bucket_for_tmdb_type,
    release_label_for_tmdb_type,
)


def test_bucket_for_tmdb_type_limited():
    assert bucket_for_tmdb_type(2) == "limited"


def test_bucket_for_tmdb_type_wide():
    assert bucket_for_tmdb_type(3) == "wide"


def test_bucket_for_tmdb_type_digital():
    assert bucket_for_tmdb_type(4) == "digital"


def test_bucket_for_tmdb_type_physical():
    assert bucket_for_tmdb_type(5) == "physical"


def test_bucket_for_tmdb_type_premiere_not_surfaced():
    assert bucket_for_tmdb_type(1) is None


def test_bucket_for_tmdb_type_tv_not_surfaced():
    assert bucket_for_tmdb_type(6) is None


def test_bucket_for_tmdb_type_unknown_not_surfaced():
    assert bucket_for_tmdb_type(99) is None


def test_release_label_for_surfaced_types():
    assert release_label_for_tmdb_type(2) == "Limited"
    assert release_label_for_tmdb_type(3) == "Wide"
    assert release_label_for_tmdb_type(4) == "Digital"
    assert release_label_for_tmdb_type(5) == "Physical"


def test_release_label_for_unsurfaced_types_is_none():
    assert release_label_for_tmdb_type(1) is None
    assert release_label_for_tmdb_type(6) is None
    assert release_label_for_tmdb_type(99) is None


def test_release_buckets_constant():
    assert RELEASE_BUCKETS == ("limited", "wide", "digital", "physical")


def test_release_bucket_labels():
    assert RELEASE_BUCKET_LABELS == {
        "limited": "Limited",
        "wide": "Wide",
        "digital": "Digital",
        "physical": "Physical",
    }


def test_every_bucket_has_a_label():
    # The two halves of the same vocabulary: a type admitted by `release_grade` with no label
    # here would list on the film page as nothing at all.
    assert set(RELEASE_TYPE_BUCKETS.values()) == set(RELEASE_BUCKET_LABELS)


def test_tmdb_type_to_bucket_keys():
    assert tuple(RELEASE_TYPE_BUCKETS) == (2, 3, 4, 5)
