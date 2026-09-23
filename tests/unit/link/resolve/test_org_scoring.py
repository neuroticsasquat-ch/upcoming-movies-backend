"""The organisation decision table (EF-12): the name gate, the one corroborating feature, the
popularity prior that orders but never scores, and the four routes."""

from upmovies.link.resolve.org_candidates import COLLECTION, COMPANY, OrgCandidate
from upmovies.link.resolve.org_scoring import (
    ORG_NAME_MATCH_NONE,
    W_ORG_ATTACHED,
    W_ORG_NAME,
    OrgMention,
    org_name_match,
    rank_org_candidates,
    resolve_org_mention,
    score_org_candidate,
)
from upmovies.link.resolve.scoring import ACCEPT_FLOOR, Path, Thresholds


def company(entity_id: int, name: str, **overrides) -> OrgCandidate:
    fields: dict = {"entity_id": entity_id, "kind": COMPANY, "name": name, "from_search": True}
    fields.update(overrides)
    return OrgCandidate(**fields)


def collection(entity_id: int, name: str, **overrides) -> OrgCandidate:
    fields: dict = {"entity_id": entity_id, "kind": COLLECTION, "name": name, "from_search": True}
    fields.update(overrides)
    return OrgCandidate(**fields)


def mention(name: str, kind: str = COMPANY, **overrides) -> OrgMention:
    return OrgMention(name_as_written=name, kind=kind, **overrides)


# --- the name gate -------------------------------------------------------------------------


def test_an_identical_spelling_is_exact():
    assert org_name_match("Blumhouse", company(1, "Blumhouse")) == "exact"


def test_case_and_whitespace_differences_are_normalized():
    assert org_name_match("blumhouse   productions", company(1, "Blumhouse Productions")) == (
        "normalized"
    )


def test_a_tmdb_collection_suffix_is_dropped_rather_than_penalised():
    """TMDB names every franchise "<X> Collection" and no trade story writes that word."""
    assert org_name_match("John Wick", collection(1, "John Wick Collection")) == "reduced"


def test_punctuation_differences_reduce_alike():
    assert org_name_match("Warner Bros", company(1, "Warner Bros.")) == "reduced"


def test_a_company_legal_form_is_dropped():
    assert org_name_match("Legendary Entertainment", company(1, "Legendary Entertainment Inc"))


def test_a_trading_word_is_never_dropped():
    """ "Warner Bros. Pictures" and "Warner Bros. Television" are two real TMDB companies."""
    assert org_name_match("Warner Bros. Pictures", company(1, "Warner Bros. Television")) == (
        ORG_NAME_MATCH_NONE
    )


def test_an_abbreviation_is_not_a_match():
    """No initials tier: "WB" naming Warner Bros. is exactly the coercion that produces a
    confident wrong studio, with nothing to contradict it."""
    assert org_name_match("WB", company(1, "Warner Bros.")) == ORG_NAME_MATCH_NONE


def test_a_collection_original_name_is_tried_too():
    assert (
        org_name_match(
            "Le Samouraï", collection(1, "Le Samouraï Collection", original_name="Le Samouraï")
        )
        == "exact"
    )


def test_a_one_word_name_is_never_reduced_to_nothing():
    """A trailing form word is only dropped when something is left of the name."""
    assert org_name_match("Collection", collection(1, "Marvel Collection")) == ORG_NAME_MATCH_NONE


# --- the score -----------------------------------------------------------------------------


def test_a_name_that_does_not_match_scores_zero_however_attached():
    scored = score_org_candidate(
        company(1, "Universal Pictures", attached=True), mention=mention("Blumhouse")
    )
    assert scored.score == 0.0


def test_being_on_the_film_is_the_one_corroborating_feature():
    named = mention("Blumhouse")
    off = score_org_candidate(company(1, "Blumhouse"), mention=named)
    on = score_org_candidate(company(2, "Blumhouse", attached=True), mention=named)
    assert round(on.score - off.score, 4) == W_ORG_ATTACHED


def test_an_exact_unattached_match_scores_the_name_weight():
    scored = score_org_candidate(company(1, "Blumhouse"), mention=mention("Blumhouse"))
    assert scored.score == W_ORG_NAME


def test_the_weakest_accepting_match_still_clears_the_floor():
    """`ACCEPT_FLOOR` is calibrated against the name weight alone, for both arms."""
    scored = score_org_candidate(
        company(1, "Blumhouse Productions"), mention=mention("blumhouse productions")
    )
    assert scored.score >= ACCEPT_FLOOR


def test_the_features_behind_the_number_are_kept():
    scored = score_org_candidate(
        company(1, "Blumhouse", attached=True, catalog_reach=9), mention=mention("Blumhouse")
    )
    assert scored.features == {
        "name_match": "exact",
        "name_quality": 1.0,
        "attached": True,
        "from_search": True,
        "catalog_reach": 9,
    }


# --- the ordering --------------------------------------------------------------------------


def test_the_popularity_prior_orders_two_equal_scores():
    ranked = rank_org_candidates(
        [company(1, "Blumhouse", catalog_reach=2), company(2, "Blumhouse", catalog_reach=40)],
        mention=mention("Blumhouse"),
    )
    assert [s.entity_id for s in ranked] == [2, 1]


def test_the_popularity_prior_never_enters_the_score():
    low = score_org_candidate(
        company(1, "Blumhouse", catalog_reach=0), mention=mention("Blumhouse")
    )
    high = score_org_candidate(
        company(2, "Blumhouse", catalog_reach=500), mention=mention("Blumhouse")
    )
    assert low.score == high.score


def test_the_entity_id_breaks_a_total_tie():
    ranked = rank_org_candidates(
        [company(9, "Blumhouse"), company(3, "Blumhouse")], mention=mention("Blumhouse")
    )
    assert [s.entity_id for s in ranked] == [3, 9]


# --- the routes ----------------------------------------------------------------------------


def test_a_clear_winner_is_accepted():
    decision = resolve_org_mention(
        [company(1, "Blumhouse", attached=True), company(2, "Universal")],
        mention=mention("Blumhouse"),
        link_confidence=None,
    )
    assert (decision.path, decision.entity_id) == (Path.ACCEPTED, 1)


def test_a_lone_plausible_candidate_is_measured_against_nothing():
    decision = resolve_org_mention(
        [company(1, "Blumhouse"), company(2, "Universal Pictures")],
        mention=mention("Blumhouse"),
        link_confidence=None,
    )
    assert (decision.path, decision.entity_id) == (Path.ACCEPTED, 1)


def test_two_indistinguishable_namesakes_go_to_the_band():
    decision = resolve_org_mention(
        [company(1, "Blumhouse"), company(2, "Blumhouse")],
        mention=mention("Blumhouse"),
        link_confidence=None,
    )
    assert (decision.path, decision.entity_id) == (Path.TIEBREAK, None)


def test_being_on_the_film_separates_two_namesakes():
    decision = resolve_org_mention(
        [company(1, "Blumhouse"), company(2, "Blumhouse", attached=True)],
        mention=mention("Blumhouse"),
        link_confidence=None,
    )
    assert (decision.path, decision.entity_id) == (Path.ACCEPTED, 2)


def test_nothing_matching_is_unlinked_when_the_search_answered():
    decision = resolve_org_mention(
        [company(1, "Universal Pictures")],
        mention=mention("Blumhouse"),
        link_confidence=None,
        search_empty=False,
    )
    assert (decision.path, decision.entity_id) == (Path.UNLINKED, None)


def test_an_empty_search_is_not_in_tmdb():
    """Unlike a person's name, an organisation's name is what the endpoint matches on, so an
    empty answer means TMDB holds no such studio (INV-8)."""
    decision = resolve_org_mention(
        [], mention=mention("Nonesuch Pictures"), link_confidence=None, search_empty=True
    )
    assert (decision.path, decision.entity_id) == (Path.NOT_IN_TMDB, None)


def test_no_candidates_and_a_non_empty_search_is_unlinked():
    decision = resolve_org_mention(
        [], mention=mention("Blumhouse"), link_confidence=None, search_empty=False
    )
    assert decision.path is Path.UNLINKED


# --- INV-6 and the thresholds ---------------------------------------------------------------


def test_confidence_is_capped_at_the_story_s_link():
    decision = resolve_org_mention(
        [company(1, "Blumhouse", attached=True)],
        mention=mention("Blumhouse"),
        link_confidence=0.3,
    )
    assert decision.confidence == 0.3


def test_a_story_with_no_link_confidence_is_capped_by_nothing():
    decision = resolve_org_mention(
        [company(1, "Blumhouse")], mention=mention("Blumhouse"), link_confidence=None
    )
    assert decision.confidence == W_ORG_NAME


def test_the_thresholds_are_injectable():
    decision = resolve_org_mention(
        [company(1, "Blumhouse")],
        mention=mention("Blumhouse"),
        link_confidence=None,
        thresholds=Thresholds(accept_floor=0.99, accept_margin=0.12),
    )
    assert decision.path is Path.UNLINKED


def test_the_decision_records_the_kind_and_the_arithmetic():
    decision = resolve_org_mention(
        [company(1, "Blumhouse")], mention=mention("Blumhouse"), link_confidence=None
    )
    assert decision.features["kind"] == COMPANY
    assert decision.features["best_score"] == W_ORG_NAME
    assert decision.features["runner_up_score"] == 0.0
    assert decision.features["candidate_count"] == 1
    assert decision.features["cache_hit"] is False


def test_the_whole_ranked_shortlist_is_carried():
    """The near-misses are what `/admin/resolution` renders, and what the band shows a model."""
    decision = resolve_org_mention(
        [company(1, "Blumhouse"), company(2, "Blumhouse"), company(3, "Universal")],
        mention=mention("Blumhouse"),
        link_confidence=None,
    )
    assert [s.entity_id for s in decision.ranked] == [1, 2, 3]
