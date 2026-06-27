from upmovies.public.release import (
    _TMDB_TYPE_TO_BUCKET,
    RELEASE_BUCKET_LABELS,
    RELEASE_BUCKETS,
    bucket_for_tmdb_type,
    release_label_for_tmdb_type,
)


def test_bucket_for_tmdb_type_limited():
    assert bucket_for_tmdb_type(2) == "limited"


def test_bucket_for_tmdb_type_wide():
    assert bucket_for_tmdb_type(3) == "wide"


def test_bucket_for_tmdb_type_premiere_not_surfaced():
    assert bucket_for_tmdb_type(1) is None


def test_bucket_for_tmdb_type_digital_not_surfaced():
    assert bucket_for_tmdb_type(4) is None


def test_bucket_for_tmdb_type_physical_not_surfaced():
    assert bucket_for_tmdb_type(5) is None


def test_bucket_for_tmdb_type_tv_not_surfaced():
    assert bucket_for_tmdb_type(6) is None


def test_bucket_for_tmdb_type_unknown_not_surfaced():
    assert bucket_for_tmdb_type(99) is None


def test_release_label_for_surfaced_types():
    assert release_label_for_tmdb_type(2) == "Limited"
    assert release_label_for_tmdb_type(3) == "Wide"


def test_release_label_for_unsurfaced_types_is_none():
    assert release_label_for_tmdb_type(1) is None
    assert release_label_for_tmdb_type(4) is None
    assert release_label_for_tmdb_type(99) is None


def test_release_buckets_constant():
    assert RELEASE_BUCKETS == ("limited", "wide")


def test_release_bucket_labels():
    assert RELEASE_BUCKET_LABELS == {"limited": "Limited", "wide": "Wide"}


def test_tmdb_type_to_bucket_keys():
    assert tuple(_TMDB_TYPE_TO_BUCKET) == (2, 3)
