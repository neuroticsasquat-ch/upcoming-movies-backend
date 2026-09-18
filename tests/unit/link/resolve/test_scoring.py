"""The pure half of person resolution: name matching, the feature weights, and routing.

Every decision this module makes is arithmetic over values a caller already read, so the
whole table is provable here without a database or a network. What only a database can
prove — the cache, the merge into `features`, the counters — is in
`tests/integration/link/resolve/test_pipeline.py`.
"""

from datetime import UTC, date, datetime

import pytest

from upmovies.catalog.person_dates import DECEASED, IMPLAUSIBLE_AGE, years_before
from upmovies.link.cluster import _VALID_TYPES
from upmovies.link.resolve.candidates import Candidate, ChangeFact, CreditFact
from upmovies.link.resolve.scoring import (
    ACCEPT_FLOOR,
    ACCEPT_MARGIN,
    ATTACHMENT_EVENT_TYPES,
    MIN_AGE_YEARS,
    MIN_CREW_AGE_YEARS,
    POSTHUMOUS_YEARS,
    W_AGE,
    W_CHANGE_STREAM,
    W_CREDITED,
    W_DEPARTMENT,
    W_FILMOGRAPHY,
    W_NAME,
    Mention,
    Path,
    Thresholds,
    age_plausibility,
    cap_confidence,
    department_agreement,
    name_match,
    rank_candidates,
    resolve_mention,
)

CHANGED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
STORY_DATE = date(2026, 9, 15)


def candidate(person_id: int, name: str, **overrides) -> Candidate:
    fields: dict = {
        "person_id": person_id,
        "name": name,
        "original_name": None,
        "known_for_department": None,
        "popularity": None,
        "from_search": True,
        "credited": False,
        "in_change_stream": False,
    }
    fields.update(overrides)
    return Candidate(**fields)


def mention(name: str = "Chris Evans", **overrides) -> Mention:
    return Mention(name_as_written=name, **overrides)


def crew_credit(**overrides) -> CreditFact:
    fields: dict = {
        "credit_type": "crew",
        "department": "Directing",
        "job": "Director",
        "credit_order": None,
    }
    fields.update(overrides)
    return CreditFact(**fields)


def cast_credit(**overrides) -> CreditFact:
    fields: dict = {
        "credit_type": "cast",
        "department": "Acting",
        "job": None,
        "credit_order": 1,
    }
    fields.update(overrides)
    return CreditFact(**fields)


# --- name matching -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "stored", "expected"),
    [
        ("Chris Evans", "Chris Evans", "exact"),
        ("chris  evans", "Chris Evans", "normalized"),
        ("Chloë Sevigny", "Chloë Sevigny", "exact"),
        ("Kenneth Branagh Jr.", "Kenneth Branagh", "suffix"),
        ("Sammy Davis Jr", "Sammy Davis Jr.", "suffix"),
        ("J.K. Simmons", "Jonathan Kimble Simmons", "initials"),
        ("J. Simmons", "Jonathan Simmons", "initials"),
        ("Chris Evans", "Chris Pine", "none"),
        ("Chris Evans", "Christopher Evans", "none"),
        ("John Smith", "James Smith", "none"),
        ("Cher", "Cher", "exact"),
        ("Cher", "Chad", "none"),
    ],
)
def test_name_match_quality(written: str, stored: str, expected: str):
    assert name_match(written, candidate(1, stored)) == expected


def test_original_name_is_matched_as_well_as_the_display_name():
    """A trade quoting a foreign production's press release writes the native-script form,
    which is the spelling TMDB keeps in `original_name`."""
    hit = candidate(1, "Song Kang-ho", original_name="송강호")
    assert name_match("송강호", hit) == "exact"
    assert name_match("Song Kang-ho", hit) == "exact"


def test_a_name_that_does_not_match_scores_nothing_however_corroborated():
    """The name is the claim; everything else is corroboration. A film's director is
    credited, in the change stream and in the right department, and none of it makes them the
    person a story about their film named."""
    director = candidate(
        1,
        "Someone Else",
        credited=True,
        in_change_stream=True,
        credits=(crew_credit(),),
    )
    [scored] = rank_candidates([director], mention=mention(department="Directing"))
    assert scored.score == 0.0


# --- features ------------------------------------------------------------------------


def test_corroboration_raises_the_score_above_a_bare_name_match():
    bare = candidate(1, "Chris Evans")
    anchored = candidate(2, "Chris Evans", credited=True, credits=(cast_credit(),))
    [best, worst] = rank_candidates([bare, anchored], mention=mention(department="Acting"))
    assert best.person_id == 2
    assert worst.score == pytest.approx(W_NAME)
    assert best.score > worst.score


def test_a_contradicted_department_costs_a_candidate_more_than_silence_does():
    """A contradiction is evidence, not merely an absence of it: the story says the film's
    composer and this candidate's only tie to it is an acting credit. It does not overturn
    the credit — being on the film is a harder fact than a department a model inferred — but
    it has to cost something, or the feature would only ever be able to agree."""
    actor = candidate(1, "Ludwig Goransson", credited=True, credits=(cast_credit(),))
    contradicted = rank_candidates(
        [actor], mention=mention("Ludwig Goransson", department="Sound")
    )[0]
    silent = rank_candidates([actor], mention=mention("Ludwig Goransson"))[0]
    agreeing = rank_candidates([actor], mention=mention("Ludwig Goransson", department="Acting"))[0]
    assert contradicted.score < silent.score < agreeing.score
    assert contradicted.features["department_agreement"] == -1.0


def test_an_unextracted_department_earns_no_bonus_rather_than_half_of_one():
    plain = candidate(1, "Chris Evans")
    [scored] = rank_candidates([plain], mention=mention())
    assert scored.features["department_agreement"] == 0.0
    assert scored.score == pytest.approx(W_NAME)


def test_role_matches_a_job_on_this_film():
    director = candidate(1, "Greta Gerwig", credits=(crew_credit(),))
    assert department_agreement(mention("Greta Gerwig", role="director"), director) == 1.0


def test_a_character_name_as_role_leaves_the_department_clause_to_speak():
    """For a performer the extracted role is a character name, which matches no job."""
    actor = candidate(1, "Robert Pattinson", credits=(cast_credit(),))
    assert department_agreement(mention("Robert Pattinson", role="Batman"), actor) == 0.0
    with_department = mention("Robert Pattinson", role="Batman", department="Acting")
    assert department_agreement(with_department, actor) == 1.0


def test_known_for_department_can_agree_but_never_contradict():
    """A working actor who directs one film is still `known_for_department='Acting'`, so it
    is far too weak a fact to disbelieve a story on."""
    uncredited = candidate(1, "Greta Gerwig", known_for_department="Acting")
    assert department_agreement(mention("Greta Gerwig", department="Acting"), uncredited) == 1.0
    assert department_agreement(mention("Greta Gerwig", department="Directing"), uncredited) == 0.0


def test_filmography_overlap_with_a_title_the_article_named():
    overlapping = candidate(1, "Chris Evans", filmography_tmdb_ids=(55, 66))
    apart = candidate(2, "Chris Evans", filmography_tmdb_ids=(77,))
    ranked = rank_candidates(
        [apart, overlapping], mention=mention(), mentioned_tmdb_ids=frozenset({66})
    )
    assert ranked[0].person_id == 1
    assert ranked[0].features["filmography_overlap"] == [66]
    assert ranked[1].features["filmography_overlap"] == []


def test_the_change_stream_corroborates_a_departing_person():
    leaving = candidate(
        1,
        "Chris Evans",
        in_change_stream=True,
        changes=(
            ChangeFact(credit_type="cast", job=None, change="removed", changed_at=CHANGED_AT),
        ),
    )
    plain = candidate(2, "Chris Evans")
    ranked = rank_candidates([plain, leaving], mention=mention())
    assert ranked[0].person_id == 1


# --- popularity is never primary -----------------------------------------------------


def test_popularity_orders_a_tie_without_entering_the_score():
    """D-21's tiebreak prior. It decides who heads the shortlist and nothing else — both
    candidates still score identically, so the mention still routes to `tiebreak`."""
    obscure = candidate(1, "Chris Evans", popularity=0.4)
    famous = candidate(2, "Chris Evans", popularity=98.0)
    decision = resolve_mention(
        [obscure, famous], mention=mention(), link_confidence=0.9, search_empty=False
    )
    assert [s.person_id for s in decision.ranked] == [2, 1]
    assert decision.ranked[0].score == decision.ranked[1].score
    assert decision.path is Path.TIEBREAK
    assert decision.person_id is None


def test_popularity_cannot_carry_a_candidate_over_the_floor():
    nobody = candidate(1, "Someone Entirely Different", popularity=500.0)
    decision = resolve_mention([nobody], mention=mention(), link_confidence=0.9)
    assert decision.path is Path.UNLINKED


# --- routing -------------------------------------------------------------------------


def test_a_lone_name_match_accepts():
    decision = resolve_mention(
        [candidate(1, "Chris Evans")], mention=mention(), link_confidence=0.9
    )
    assert (decision.path, decision.person_id) == (Path.ACCEPTED, 1)


def test_the_credited_namesake_beats_the_one_who_is_not():
    """The margin, not the floor, is what makes the wrong Chris Evans impossible — and what
    separates them when one of them is actually on the film."""
    on_the_film = candidate(1, "Chris Evans", credited=True, credits=(cast_credit(),))
    namesake = candidate(2, "Chris Evans", popularity=99.0)
    decision = resolve_mention([namesake, on_the_film], mention=mention(), link_confidence=0.95)
    assert (decision.path, decision.person_id) == (Path.ACCEPTED, 1)


def test_two_equally_corroborated_namesakes_route_to_tiebreak():
    first = candidate(1, "Chris Evans", credited=True, credits=(cast_credit(),))
    second = candidate(2, "Chris Evans", credited=True, credits=(cast_credit(),))
    decision = resolve_mention([first, second], mention=mention(), link_confidence=0.95)
    assert decision.path is Path.TIEBREAK
    assert decision.person_id is None
    assert decision.features["margin"] == 0.0
    assert [s.person_id for s in decision.ranked] == [1, 2]


def test_nothing_above_the_floor_is_unlinked():
    decision = resolve_mention(
        [candidate(1, "Someone Else")], mention=mention(), link_confidence=0.9
    )
    assert (decision.path, decision.person_id) == (Path.UNLINKED, None)


def test_no_candidates_at_all_is_unlinked():
    decision = resolve_mention([], mention=mention(), link_confidence=0.9)
    assert decision.path is Path.UNLINKED
    assert decision.ranked == []
    assert decision.features["best_score"] == 0.0


def test_an_initials_match_alone_cannot_clear_the_floor():
    """An initials match covers every Smith with a J, so it accepts only once something else
    about the candidate corroborates it."""
    alone = resolve_mention(
        [candidate(1, "Jonathan Kimble Simmons")],
        mention=mention("J.K. Simmons"),
        link_confidence=0.9,
    )
    assert alone.path is Path.UNLINKED

    corroborated = resolve_mention(
        [
            candidate(
                1,
                "Jonathan Kimble Simmons",
                credited=True,
                in_change_stream=True,
                credits=(cast_credit(),),
            )
        ],
        mention=mention("J.K. Simmons", department="Acting"),
        link_confidence=0.9,
    )
    assert corroborated.path is Path.ACCEPTED


def test_thresholds_are_settings_not_constants():
    """A band the operator widens routes what used to accept to `tiebreak` instead."""
    strong = candidate(1, "Chris Evans", credited=True, credits=(cast_credit(),))
    weak = candidate(2, "Chris Evans")
    wide = Thresholds(accept_floor=ACCEPT_FLOOR, accept_margin=0.9)
    assert resolve_mention([strong, weak], mention=mention(), link_confidence=0.9).path is (
        Path.ACCEPTED
    )
    decision = resolve_mention(
        [strong, weak], mention=mention(), link_confidence=0.9, thresholds=wide
    )
    assert decision.path is Path.TIEBREAK


# --- not_in_tmdb (INV-8) -------------------------------------------------------------


def test_an_empty_search_on_an_attachment_beat_is_not_in_tmdb():
    decision = resolve_mention(
        [],
        mention=mention("Nobody Knownyet", event_type="casting"),
        link_confidence=0.9,
        search_empty=True,
    )
    assert (decision.path, decision.person_id) == (Path.NOT_IN_TMDB, None)


def test_an_empty_search_with_no_attachment_claim_is_merely_unlinked():
    """An executive quoted in passing, or a name the trade misspelled. Their absence from
    TMDB says nothing worth recording."""
    decision = resolve_mention(
        [],
        mention=mention("Some Exec", event_type="other"),
        link_confidence=0.9,
        search_empty=True,
    )
    assert decision.path is Path.UNLINKED


def test_an_extracted_role_is_a_debut_claim_whatever_the_beat():
    """D-21 names the role, so it carries the claim on its own — a story can say what
    somebody does on the film while the beat it names them in is a trailer drop."""
    for event_type in (None, "trailer"):
        decision = resolve_mention(
            [],
            mention=mention("Nobody Knownyet", role="director", event_type=event_type),
            link_confidence=0.9,
            search_empty=True,
        )
        assert decision.path is Path.NOT_IN_TMDB


def test_an_attachment_beat_is_a_debut_claim_when_no_role_was_extracted():
    """The addition to what the role clause catches: a casting story that names somebody
    without saying which part they have."""
    decision = resolve_mention(
        [],
        mention=mention("Nobody Knownyet", event_type="casting"),
        link_confidence=0.9,
        search_empty=True,
    )
    assert decision.path is Path.NOT_IN_TMDB


def test_a_beat_the_extraction_prompt_cannot_emit_is_not_in_the_attachment_set():
    """`crew_attached` is a `news.Event` type but not one the cluster instructions offer, so
    listing it here would be a route that silently never fires."""
    assert "crew_attached" not in ATTACHMENT_EVENT_TYPES
    assert ATTACHMENT_EVENT_TYPES <= _VALID_TYPES


def test_a_search_that_found_people_is_never_not_in_tmdb():
    """Candidates exist and none of them matched — that is "we could not tell", which is a
    different answer from "TMDB does not hold this person"."""
    decision = resolve_mention(
        [candidate(1, "Someone Else")],
        mention=mention("Chris Evans", event_type="casting"),
        link_confidence=0.9,
        search_empty=False,
    )
    assert decision.path is Path.UNLINKED


# --- INV-6 ---------------------------------------------------------------------------


def test_confidence_is_capped_by_the_story_link():
    """D-23: we cannot be surer who a story named than we are that it is about this film."""
    decision = resolve_mention(
        [candidate(1, "Chris Evans", credited=True, credits=(cast_credit(),))],
        mention=mention(department="Acting"),
        link_confidence=0.7,
    )
    assert decision.path is Path.ACCEPTED
    assert decision.confidence == 0.7
    assert decision.features["best_score"] > 0.7


def test_a_score_below_the_link_confidence_is_left_alone():
    decision = resolve_mention(
        [candidate(1, "Chris Evans")], mention=mention(), link_confidence=0.99
    )
    assert decision.confidence == pytest.approx(W_NAME)


def test_a_story_with_no_link_confidence_is_capped_by_nothing():
    assert cap_confidence(0.8, None) == 0.8
    assert cap_confidence(0.8, 0.5) == 0.5


def test_an_unlinked_mention_still_records_what_its_best_candidate_scored():
    """The queue a human works at `/admin/resolution` is worth ranking."""
    decision = resolve_mention(
        [candidate(1, "Jonathan Kimble Simmons")],
        mention=mention("J.K. Simmons"),
        link_confidence=0.9,
    )
    assert decision.path is Path.UNLINKED
    assert decision.confidence > 0.0
    assert decision.features["candidate_count"] == 1


# --- age/alive plausibility (D-21) ----------------------------------------------------


def test_the_weights_still_sum_to_one_with_the_age_term_in_them():
    """A perfect match corroborated every way scores exactly 1.0, which is what keeps the
    score from needing a clamp from above — and the age term had to be paid for out of the
    same 1.0 rather than added on top of it."""
    assert (
        W_NAME + W_CREDITED + W_CHANGE_STREAM + W_AGE + W_DEPARTMENT + W_FILMOGRAPHY
    ) == pytest.approx(1.0)


def test_the_floor_is_still_calibrated_against_the_name_alone():
    """`ACCEPT_FLOOR` sits just under a normalized name match with nothing else behind it, so
    rebalancing the corroborating features must not have moved what a lone name can do."""
    assert ACCEPT_FLOOR < W_NAME * 0.95
    lone = resolve_mention([candidate(1, "Chris  Evans")], mention=mention(), link_confidence=0.9)
    assert lone.path is Path.ACCEPTED


def test_a_long_dead_candidate_falls_below_the_floor():
    """The posthumous namesake: TMDB knows one Chris Evans by that name and he died in 1998,
    so a trade naming him this week is not naming him."""
    dead = candidate(1, "Chris Evans", birthday=date(1920, 1, 1), deathday=date(1998, 3, 4))
    decision = resolve_mention(
        [dead], mention=mention(), link_confidence=0.9, story_date=STORY_DATE
    )
    assert decision.path is Path.UNLINKED
    assert decision.ranked[0].score == pytest.approx(W_NAME - W_AGE)
    features = decision.ranked[0].features
    assert (features["age_plausibility"], features["age_reason"]) == (-1.0, DECEASED)
    assert (features["birthday"], features["deathday"]) == ("1920-01-01", "1998-03-04")


def test_a_candidate_implausibly_young_for_the_extracted_role_falls_below_the_floor():
    """The other half of the feature: TMDB's name search happily returns the nine-year-old
    who shares a director's name."""
    child = candidate(1, "Chris Evans", birthday=date(2017, 5, 1))
    directing = mention(role="Director", department="Directing")
    decision = resolve_mention(
        [child], mention=directing, link_confidence=0.9, story_date=STORY_DATE
    )
    assert decision.path is Path.UNLINKED
    assert decision.ranked[0].features["age_reason"] == IMPLAUSIBLE_AGE


def test_the_age_bar_is_the_one_the_extracted_role_implies():
    """Infants really are cast, so the bar that catches a nine-year-old director cannot be
    the bar applied to a performer — the role and the department are what tell them apart."""
    child = candidate(1, "Chris Evans", birthday=date(2017, 5, 1))
    assert MIN_AGE_YEARS < MIN_CREW_AGE_YEARS
    performer = mention(role="Young Steve", department="Acting")
    assert age_plausibility(performer, child, story_date=STORY_DATE).value == 1.0
    assert age_plausibility(mention(role="Director"), child, story_date=STORY_DATE).value == -1.0
    assert age_plausibility(mention(department="Sound"), child, story_date=STORY_DATE).value == -1.0


def test_an_infant_is_implausible_for_any_role_at_all():
    newborn = candidate(1, "Chris Evans", birthday=date(2026, 1, 1))
    acting = mention(role="Baby Steve", department="Acting")
    assert age_plausibility(acting, newborn, story_date=STORY_DATE).value == -1.0


def test_a_living_plausible_candidate_still_accepts():
    alive = candidate(1, "Chris Evans", birthday=date(1981, 6, 13))
    decision = resolve_mention(
        [alive], mention=mention(), link_confidence=0.99, story_date=STORY_DATE
    )
    assert decision.path is Path.ACCEPTED
    assert decision.ranked[0].score == pytest.approx(W_NAME + W_AGE)
    assert decision.ranked[0].features["age_plausibility"] == 1.0
    assert decision.ranked[0].features["age_reason"] is None


def test_a_candidate_with_no_dates_earns_neither_a_bonus_nor_a_penalty():
    """Absence of evidence is not evidence, as with the other five features — and most people
    TMDB holds have no birthday at all."""
    undated = candidate(1, "Chris Evans")
    [scored] = rank_candidates([undated], mention=mention(), story_date=STORY_DATE)
    assert scored.features["age_plausibility"] == 0.0
    assert scored.score == pytest.approx(W_NAME)


def test_a_death_inside_the_posthumous_window_neither_penalizes_nor_corroborates():
    """A film completed before the death, archive footage, a posthumous release — all
    ordinary, and none of them a reason to call the candidate plausible either."""
    recent = years_before(STORY_DATE, POSTHUMOUS_YEARS - 1)
    departed = candidate(1, "Chris Evans", birthday=date(1950, 1, 1), deathday=recent)
    [scored] = rank_candidates([departed], mention=mention(), story_date=STORY_DATE)
    assert scored.features["age_plausibility"] == 0.0
    assert scored.score == pytest.approx(W_NAME)


def test_a_story_with_no_date_leaves_the_feature_silent():
    """The story date is one of the feature's two inputs; without it there is no age to judge
    and no death to be after."""
    dead = candidate(1, "Chris Evans", deathday=date(1998, 3, 4))
    decision = resolve_mention([dead], mention=mention(), link_confidence=0.9, story_date=None)
    assert decision.path is Path.ACCEPTED
    assert decision.ranked[0].features["age_plausibility"] == 0.0
    assert decision.features["story_date"] is None


def test_the_dead_namesake_loses_to_the_living_one_deterministically():
    """The case the feature exists for. Two people TMDB knows by one name, both credited on
    the film, scoring identically on every other feature — before this they could only route
    to `tiebreak` and reach a model."""
    dead = candidate(
        1, "Chris Evans", credited=True, credits=(cast_credit(),), deathday=date(1998, 3, 4)
    )
    alive = candidate(
        2, "Chris Evans", credited=True, credits=(cast_credit(),), birthday=date(1981, 6, 13)
    )
    decision = resolve_mention(
        [dead, alive],
        mention=mention(department="Acting"),
        link_confidence=0.99,
        story_date=STORY_DATE,
    )
    assert (decision.path, decision.person_id) == (Path.ACCEPTED, 2)
    # Both sides of a ±1 feature, which is what makes the spread wide enough to clear the
    # margin where a penalty alone would still have left the two inside it.
    assert decision.features["margin"] >= ACCEPT_MARGIN
    assert decision.features["story_date"] == "2026-09-15"


def test_a_date_contradiction_never_overturns_a_credit_on_the_film():
    """Being on the film is a harder fact than a date TMDB holds for a namesake — the same
    reason a contradicted department does not overturn one. A posthumous credit inside a
    couple of years is ordinary, and the sweep's own holds say so (D-8)."""
    dead_but_credited = candidate(
        1,
        "Chris Evans",
        credited=True,
        in_change_stream=True,
        credits=(cast_credit(),),
        deathday=date(1998, 3, 4),
    )
    decision = resolve_mention(
        [dead_but_credited],
        mention=mention(department="Acting"),
        link_confidence=0.99,
        story_date=STORY_DATE,
    )
    assert decision.path is Path.ACCEPTED


def test_a_death_after_the_story_ran_is_not_a_death_this_mention_cares_about():
    """An archived story from before they died. Reading the dates against today would turn
    every one of those into a contradiction."""
    later = candidate(1, "Chris Evans", birthday=date(1962, 1, 1), deathday=date(2020, 6, 1))
    old_story = date(2015, 4, 1)
    assert age_plausibility(mention(), later, story_date=old_story).value == 1.0
    assert age_plausibility(mention(), later, story_date=STORY_DATE).value == -1.0


# --- the routing properties the weights exist to hold ---------------------------------
#
# Every one of these was true before the age feature was added and is *not* implied by the
# weights summing to 1.0 — a rebalance that spends the wrong 0.05 breaks one of them silently,
# which is exactly what happened once (see the weights' own comment). They are pinned here so
# the next rebalance has to answer them.


def test_a_date_contradiction_pulls_a_bare_name_match_under_the_floor():
    """The floor is what this contradiction has to be able to reach, and `W_AGE` is sized for
    it: a candidate whose only claim is the name, and who was dead when the story ran, is not
    this person.

    It is the *only* contradiction that reaches a candidate with nothing else going for it.
    A department can only contradict a candidate whose departments are known, which means
    credits on this film, which means `W_CREDITED` is already in their score — so that
    weight is bounded by the separation property below instead of by the floor.
    """
    assert W_NAME - W_AGE < ACCEPT_FLOOR
    dead = candidate(1, "Chris Evans", deathday=date(1998, 3, 4))
    decision = resolve_mention(
        [dead], mention=mention(), link_confidence=0.9, story_date=STORY_DATE
    )
    assert decision.path is Path.UNLINKED


@pytest.mark.parametrize(
    ("weight", "spread", "separates", "agreeing", "rival"),
    [
        (W_CREDITED, 1, True, {"credited": True, "credits": (cast_credit(),)}, {}),
        (W_AGE, 2, True, {"birthday": date(1981, 6, 13)}, {"deathday": date(1998, 3, 4)}),
        (W_CHANGE_STREAM, 1, False, {"in_change_stream": True}, {}),
        (
            W_FILMOGRAPHY,
            1,
            False,
            {"filmography_tmdb_ids": (66,)},
            {"filmography_tmdb_ids": (77,)},
        ),
    ],
)
def test_which_features_can_separate_two_namesakes_on_their_own(
    weight: float, spread: int, separates: bool, agreeing, rival
):
    """The table a rebalance has to preserve, because it is what keeps the band at D-22's
    ≤10% of mentions — and it is not implied by the weights summing to 1.0.

    A contradiction-capable feature spreads a namesake pair by `2 × W` (it corroborates one
    and penalizes the other); a plain bonus spreads them by `W`. `credited` and the two ±1
    features clear `ACCEPT_MARGIN`; the change stream and filmography overlap never did,
    before this feature or after it, and a pair that differs only there still reaches the
    model. Shaving a weight from the first group into the second is the silent regression
    this pins.
    """
    assert (spread * weight >= ACCEPT_MARGIN) is separates
    decision = resolve_mention(
        [candidate(1, "Chris Evans", **agreeing), candidate(2, "Chris Evans", **rival)],
        mention=mention(),
        link_confidence=0.99,
        mentioned_tmdb_ids=frozenset({66}),
        story_date=STORY_DATE,
    )
    if separates:
        assert (decision.path, decision.person_id) == (Path.ACCEPTED, 1)
    else:
        assert (decision.path, decision.person_id) == (Path.TIEBREAK, None)


def test_a_department_that_agrees_with_one_namesake_and_contradicts_the_other_decides():
    """The case a halved `W_DEPARTMENT` broke: the spread is `2 × W_DEPARTMENT`, and it has to
    clear the margin or the story telling us which department this person works in stops
    being able to pick between two people who share a name."""
    composer = candidate(
        1,
        "Ludwig Goransson",
        credited=True,
        credits=(crew_credit(job="Original Music Composer", department="Sound"),),
    )
    actor = candidate(2, "Ludwig Goransson", credited=True, credits=(cast_credit(),))
    decision = resolve_mention(
        [composer, actor],
        mention=mention("Ludwig Goransson", department="Sound"),
        link_confidence=0.99,
    )
    assert (decision.path, decision.person_id) == (Path.ACCEPTED, 1)


def test_a_birthday_on_one_namesake_and_none_on_the_other_decides_nothing():
    """`W_AGE` is under the margin on purpose. TMDB holding a birthday for one of two people
    by the same name says something about TMDB's coverage, not about which of them a trade
    just named — so it orders the shortlist and stops there."""
    assert W_AGE < ACCEPT_MARGIN
    documented = candidate(1, "Chris Evans", birthday=date(1981, 6, 13))
    undocumented = candidate(2, "Chris Evans")
    decision = resolve_mention(
        [documented, undocumented],
        mention=mention(),
        link_confidence=0.99,
        story_date=STORY_DATE,
    )
    assert decision.path is Path.TIEBREAK
    assert decision.person_id is None
