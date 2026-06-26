from upmovies.public.release import (
    _TMDB_TYPE_TO_BUCKET,
    RELEASE_BUCKETS,
    bucket_for_tmdb_type,
    release_type_label,
)


def test_release_type_label_all_six_types():
    assert release_type_label(1) == "Premiere"
    assert release_type_label(2) == "Theatrical (limited)"
    assert release_type_label(3) == "Theatrical"
    assert release_type_label(4) == "Digital"
    assert release_type_label(5) == "Physical"
    assert release_type_label(6) == "TV"


def test_release_type_label_unknown_fallback():
    result = release_type_label(99)
    assert result  # non-empty
    assert "99" in result
    assert result == "Unknown (99)"


def test_bucket_for_tmdb_type_premiere():
    assert bucket_for_tmdb_type(1) == "premiere"


def test_bucket_for_tmdb_type_limited():
    assert bucket_for_tmdb_type(2) == "limited"


def test_bucket_for_tmdb_type_wide():
    assert bucket_for_tmdb_type(3) == "wide"


def test_bucket_for_tmdb_type_digital_not_surfaced():
    assert bucket_for_tmdb_type(4) is None


def test_bucket_for_tmdb_type_physical_not_surfaced():
    assert bucket_for_tmdb_type(5) is None


def test_bucket_for_tmdb_type_tv_not_surfaced():
    assert bucket_for_tmdb_type(6) is None


def test_bucket_for_tmdb_type_unknown_not_surfaced():
    assert bucket_for_tmdb_type(99) is None


def test_release_buckets_constant():
    assert RELEASE_BUCKETS == ("premiere", "limited", "wide")


def test_tmdb_type_to_bucket_keys():
    assert tuple(_TMDB_TYPE_TO_BUCKET) == (1, 2, 3)
