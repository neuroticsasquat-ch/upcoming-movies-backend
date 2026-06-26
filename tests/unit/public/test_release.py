from upmovies.public.release import release_type_label


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
