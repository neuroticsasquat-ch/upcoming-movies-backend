from upmovies.public.arc import derive_arc_stage, most_significant_event_type, ordered_event_types


def test_status_baselines():
    assert derive_arc_stage("Rumored") == "announced"
    assert derive_arc_stage("Planned") == "announced"
    assert derive_arc_stage("In Production") == "shooting"
    assert derive_arc_stage("Post Production") == "wrapped"
    assert derive_arc_stage("Released") == "released"


def test_unknown_or_none_status_floors_to_announced():
    assert derive_arc_stage(None) == "announced"
    assert derive_arc_stage("Whatever") == "announced"


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


def test_most_significant_first_look_ranks_below_arc_types():
    # first_look ranks like other (-1): loses to any arc-stage type, wins when alone.
    assert most_significant_event_type(["casting", "first_look"]) == "casting"
    assert most_significant_event_type(["first_look"]) == "first_look"


def test_crew_attached_ranks_with_announced():
    """Unregistered it would fall to the -1 fallback and rank beneath every arc type — a
    director attaching would lose to anything it shared a day with (NEU-1083)."""
    assert most_significant_event_type(["crew_attached", "other"]) == "crew_attached"
    assert most_significant_event_type(["crew_attached", "casting"]) == "casting"
    assert most_significant_event_type(["crew_attached"]) == "crew_attached"


def test_crew_attached_does_not_move_the_arc_stage():
    """`derive_arc_stage` stays status-only (NEU-452): a catalog event is not a status, any
    more than a news event is."""
    assert derive_arc_stage("Planned") == "announced"
    assert derive_arc_stage("In Production") == "shooting"


def test_ordered_event_types_most_significant_first():
    assert ordered_event_types(["casting", "trailer", "announced"]) == [
        "trailer",
        "casting",
        "announced",
    ]


def test_ordered_event_types_dedupes():
    assert ordered_event_types(["casting", "casting", "trailer"]) == ["trailer", "casting"]


def test_ordered_event_types_breaks_rank_ties_alphabetically():
    """`first_look` and `other` share the -1 fallback rank, so rank alone leaves their order
    up to whatever Postgres' array_agg happened to return. The alphabetical tie-break keeps
    the rendered label row stable between requests."""
    assert ordered_event_types(["other", "first_look"]) == ["first_look", "other"]
    assert ordered_event_types(["first_look", "other"]) == ["first_look", "other"]


def test_ordered_event_types_leads_with_the_most_significant_type():
    for types in (["announced", "casting", "trailer"], ["other"], ["crew_attached", "casting"]):
        assert ordered_event_types(types)[0] == most_significant_event_type(types)


def test_ordered_event_types_empty():
    assert ordered_event_types([]) == []
