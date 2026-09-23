"""The organisation closed-set question (EF-12): what the model is shown, and what it is
deliberately not shown. The reply parser is the person side's and is pinned in
`test_tiebreak.py`."""

import json

import pytest

from upmovies.link.resolve.org_candidates import COLLECTION, COMPANY, OrgCandidate
from upmovies.link.resolve.org_scoring import OrgMention
from upmovies.link.resolve.tiebreak import OrgTiebreakQuestion, build_org_tiebreak_request


def question(**overrides) -> OrgTiebreakQuestion:
    fields: dict = {
        "film_title": "The Housemaid's Secret",
        "film_year": 2027,
        "story_title": "Blumhouse boards the thriller",
        "story_text": "The studio will finance and produce.",
        "mention": OrgMention(
            name_as_written="Blumhouse", kind=COMPANY, event_type="company_attached"
        ),
        "evidence_span": "Blumhouse is boarding the thriller",
    }
    fields.update(overrides)
    return OrgTiebreakQuestion(**fields)


def candidate(entity_id: int, name: str, **overrides) -> OrgCandidate:
    fields: dict = {"entity_id": entity_id, "kind": COMPANY, "name": name}
    fields.update(overrides)
    return OrgCandidate(**fields)


def payload(prompt) -> dict:
    return json.loads(prompt.user)


def test_the_instructions_are_the_stable_prefix():
    """ADR-0006: fixed text first, per-mention content in `user`."""
    prompt = build_org_tiebreak_request(question(), [candidate(1, "Blumhouse")])
    assert "identifying ONE organisation" in prompt.stable_prefix
    assert "Blumhouse boards the thriller" not in prompt.stable_prefix


def test_one_prompt_serves_both_organisation_kinds():
    """A studio and a franchise are separated by the same evidence, so one stable prefix
    covers them and both share whatever cache it earns."""
    studio = build_org_tiebreak_request(question(), [candidate(1, "Blumhouse")])
    franchise = build_org_tiebreak_request(
        question(mention=OrgMention(name_as_written="John Wick", kind=COLLECTION)),
        [candidate(1, "John Wick Collection", kind=COLLECTION)],
    )
    assert studio.stable_prefix == franchise.stable_prefix


def test_the_mention_carries_its_kind():
    prompt = build_org_tiebreak_request(question(), [candidate(1, "Blumhouse")])
    assert payload(prompt)["mention"]["kind"] == COMPANY


def test_options_are_numbered_from_one_in_ranked_order():
    prompt = build_org_tiebreak_request(
        question(), [candidate(7, "Blumhouse"), candidate(3, "Blumhouse Television")]
    )
    assert [(o["n"], o["name"]) for o in payload(prompt)["options"]] == [
        (1, "Blumhouse"),
        (2, "Blumhouse Television"),
    ]


def test_an_option_on_the_film_says_so():
    prompt = build_org_tiebreak_request(
        question(), [candidate(1, "Blumhouse", attached=True), candidate(2, "Blumhouse")]
    )
    first, second = payload(prompt)["options"]
    assert first["already_on_this_film"] is True
    assert "already_on_this_film" not in second


def test_the_popularity_prior_is_never_shown():
    """The band exists because the scores could not separate the options; showing the prior
    that ordered them would invite the model to ratify exactly that ordering (D-21)."""
    prompt = build_org_tiebreak_request(question(), [candidate(1, "Blumhouse", catalog_reach=99)])
    assert "catalog_reach" not in prompt.user
    assert "99" not in prompt.user


def test_the_scores_are_never_shown():
    prompt = build_org_tiebreak_request(question(), [candidate(1, "Blumhouse")])
    assert "score" not in payload(prompt)["options"][0]


def test_a_collection_option_is_named_as_a_franchise():
    prompt = build_org_tiebreak_request(
        question(mention=OrgMention(name_as_written="John Wick", kind=COLLECTION)),
        [candidate(1, "John Wick Collection", kind=COLLECTION)],
    )
    assert payload(prompt)["options"][0]["kind"] == "franchise"


def test_an_option_repeating_its_own_name_omits_the_original():
    prompt = build_org_tiebreak_request(
        question(), [candidate(1, "Blumhouse", original_name="Blumhouse")]
    )
    assert "original_name" not in payload(prompt)["options"][0]


def test_a_distinct_original_name_is_shown():
    prompt = build_org_tiebreak_request(
        question(), [candidate(1, "Le Samouraï Collection", original_name="Le Samouraï")]
    )
    assert payload(prompt)["options"][0]["original_name"] == "Le Samouraï"


def test_non_latin_names_are_not_escaped():
    """The names most in need of disambiguation carry non-Latin script."""
    prompt = build_org_tiebreak_request(question(), [candidate(1, "東宝")])
    assert "東宝" in prompt.user


def test_an_empty_shortlist_is_a_wiring_bug():
    with pytest.raises(ValueError):
        build_org_tiebreak_request(question(), [])


def test_no_prefill_is_required():
    """`resolve` is a stage an operator may point at an OpenAI-compatible provider."""
    assert build_org_tiebreak_request(question(), [candidate(1, "X")]).prefill_required is False
