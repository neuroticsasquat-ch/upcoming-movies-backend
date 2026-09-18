"""The pure half of person resolution: name matching, the feature weights, and routing.

Every decision this module makes is arithmetic over values a caller already read, so the
whole table is provable here without a database or a network. What only a database can
prove — the cache, the merge into `features`, the counters — is in
`tests/integration/link/resolve/test_pipeline.py`.
"""

from datetime import UTC, datetime

import pytest

from upmovies.link.cluster import _VALID_TYPES
from upmovies.link.resolve.candidates import Candidate, ChangeFact, CreditFact
from upmovies.link.resolve.scoring import (
    ACCEPT_FLOOR,
    ATTACHMENT_EVENT_TYPES,
    W_NAME,
    Mention,
    Path,
    Thresholds,
    cap_confidence,
    department_agreement,
    name_match,
    rank_candidates,
    resolve_mention,
)

CHANGED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


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
