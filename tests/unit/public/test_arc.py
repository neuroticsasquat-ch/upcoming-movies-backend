from upmovies.public.arc import derive_arc_stage, most_significant_event_type


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


def test_most_significant_picks_highest_ranked_type():
    assert most_significant_event_type(["announced", "casting", "trailer"]) == "trailer"
    assert most_significant_event_type(["casting", "release_date"]) == "release_date"
    assert most_significant_event_type(["production_start", "production_wrap"]) == "production_wrap"


def test_most_significant_is_order_independent():
    assert most_significant_event_type(["trailer", "announced"]) == "trailer"
    assert most_significant_event_type(["announced", "trailer"]) == "trailer"


def test_most_significant_single_type():
    assert most_significant_event_type(["casting"]) == "casting"


def test_most_significant_other_ranks_below_announced():
    assert most_significant_event_type(["announced", "other"]) == "announced"


def test_most_significant_other_only():
    assert most_significant_event_type(["other"]) == "other"
    assert most_significant_event_type(["other", "other"]) == "other"
