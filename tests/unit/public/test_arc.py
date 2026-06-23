from upmovies.public.arc import derive_arc_stage


def test_status_baselines_with_no_events():
    assert derive_arc_stage("Rumored", []) == "announced"
    assert derive_arc_stage("Planned", []) == "announced"
    assert derive_arc_stage("In Production", []) == "shooting"
    assert derive_arc_stage("Post Production", []) == "wrapped"
    assert derive_arc_stage("Released", []) == "released"


def test_unknown_or_none_status_floors_to_announced():
    assert derive_arc_stage(None, []) == "announced"
    assert derive_arc_stage("Whatever", []) == "announced"


def test_event_types_advance_stage():
    assert derive_arc_stage("Planned", ["casting"]) == "cast"
    assert derive_arc_stage("Planned", ["release_date"]) == "dated"
    assert derive_arc_stage("Planned", ["trailer"]) == "trailer"


def test_max_combine_takes_the_furthest_stage():
    # In Production baseline (shooting) is ahead of an "announced" event.
    assert derive_arc_stage("In Production", ["announced"]) == "shooting"
    # A trailer event pulls a Planned film all the way to trailer.
    assert derive_arc_stage("Planned", ["announced", "casting", "trailer"]) == "trailer"


def test_other_event_type_contributes_nothing():
    assert derive_arc_stage("Planned", ["other"]) == "announced"
    assert derive_arc_stage("In Production", ["other"]) == "shooting"
