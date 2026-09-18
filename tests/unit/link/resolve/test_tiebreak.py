"""The `resolve` stage's closed-set question and the answers it will accept (D-22, NEU-1364).

Two seams, and both are about the same failure. The **request** must show a model enough to
tell two people apart who share a name by construction — if the options render down to the
same row twice, the stage cannot do better than a coin flip however good the model is. The
**reply** must be classified rather than trusted: a number outside the options list identifies
nobody, and coercing it to the nearest option is how a numbering bug becomes a confident wrong
person. Neither needs a database or a network.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from upmovies.link.resolve.candidates import Candidate, ChangeFact, CreditFact
from upmovies.link.resolve.scoring import Mention
from upmovies.link.resolve.tiebreak import (
    TiebreakQuestion,
    ask_tiebreak,
    build_tiebreak_request,
    parse_tiebreak_reply,
)
from upmovies.llm import CallLog, CallResult


class FakeClient:
    def __init__(self, response: str):
        self._response = response
        self.prompts: list = []

    async def complete_call(self, *, model, prompt, calls):
        self.prompts.append(prompt)
        return calls.record(CallResult(text=self._response))


def _candidate(person_id: int, name: str = "Chris Evans", **overrides) -> Candidate:
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


def _question(**overrides) -> TiebreakQuestion:
    fields: dict = {
        "film_title": "The Housemaid's Secret",
        "film_year": 2027,
        "story_title": "Chris Evans joins The Housemaid's Secret",
        "story_text": "The composer is scoring the thriller for Lionsgate.",
        "mention": Mention(
            name_as_written="Chris Evans",
            role="composer",
            department="Sound",
            event_type="casting",
        ),
        "evidence_span": "Chris Evans will score the film",
    }
    fields.update(overrides)
    return TiebreakQuestion(**fields)


def _payload(prompt) -> dict:
    return json.loads(prompt.user)


GOLDEN = Path(__file__).parents[3] / "fixtures" / "link" / "resolve_tiebreak_prompt.txt"


# --- the instructions: the rules that make the set closed ------------------------


def test_the_instructions_forbid_an_answer_outside_the_options():
    from upmovies.link.resolve.tiebreak import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    assert "closed set" in lowered
    assert "never invent one" in lowered
    assert "identifies nobody" in lowered


def test_the_instructions_ask_for_none_rather_than_a_guess():
    """The band is by construction the case arithmetic could not decide, so the model
    declining is a correct and expected answer — not a failure to be pushed past."""
    from upmovies.link.resolve.tiebreak import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    assert "prefer null to a guess" in lowered
    assert "coin flip" in lowered


def test_the_instructions_say_a_shared_name_is_not_evidence():
    """Every option matched the name — that is why they are options — so a model that treats
    the match as corroboration is reading the shortlist as a ranking."""
    from upmovies.link.resolve.tiebreak import _INSTRUCTIONS

    assert "Being offered an option is not evidence" in _INSTRUCTIONS
    assert "A shared name is never enough" in _INSTRUCTIONS


# --- the request -----------------------------------------------------------------


def test_the_prefix_is_the_instructions_and_carries_nothing_per_mention():
    """`Prompt.stable_prefix` is a promise that the content does not vary call to call. A
    mention's film or name leaking into it would break that for every provider at once."""
    prompt = build_tiebreak_request(_question(), [_candidate(1), _candidate(2)])

    from upmovies.link.resolve.tiebreak import _INSTRUCTIONS

    assert prompt.stable_prefix == _INSTRUCTIONS
    assert "Chris Evans" not in prompt.stable_prefix
    assert "Housemaid" not in prompt.stable_prefix


def test_the_prefix_is_identical_across_two_different_mentions():
    first = build_tiebreak_request(_question(), [_candidate(1)])
    second = build_tiebreak_request(
        _question(film_title="Another Film", story_title="Someone else entirely"),
        [_candidate(2, name="Someone Else")],
    )
    assert first.stable_prefix == second.stable_prefix


def test_options_are_numbered_from_one_in_the_order_given():
    prompt = build_tiebreak_request(_question(), [_candidate(700), _candidate(701)])
    assert [o["n"] for o in _payload(prompt)["options"]] == [1, 2]


def test_an_option_carries_the_known_for_titles_that_separate_two_namesakes():
    """The whole point of the stage: the options share a name, so what they are known for is
    the evidence. Without it the two rows are the same row twice."""
    prompt = build_tiebreak_request(
        _question(),
        [
            _candidate(700, known_for_titles=("Avengers: Endgame", "Knives Out")),
            _candidate(701, known_for_titles=("Tenet", "Oppenheimer")),
        ],
    )
    options = _payload(prompt)["options"]
    assert options[0]["known_for"] == ["Avengers: Endgame", "Knives Out"]
    assert options[1]["known_for"] == ["Tenet", "Oppenheimer"]


def test_an_option_carries_its_credit_on_this_film():
    """The sharpest fact available about a candidate the film's own credits produced, and the
    one the search hit cannot carry."""
    prompt = build_tiebreak_request(
        _question(),
        [
            _candidate(
                700,
                credited=True,
                credits=(
                    CreditFact(
                        credit_type="crew",
                        department="Sound",
                        job="Original Music Composer",
                        credit_order=None,
                    ),
                ),
            ),
            _candidate(701),
        ],
    )
    option = _payload(prompt)["options"][0]
    assert option["credited_on_this_film"] == [
        {"job": "Original Music Composer", "department": "Sound"}
    ]


def test_a_cast_credit_with_no_job_is_rendered_as_a_performer():
    """TMDB leaves `job` off cast entries, and `{"job": null}` reads as a credit nobody knows
    anything about rather than as the ordinary case it is."""
    prompt = build_tiebreak_request(
        _question(),
        [
            _candidate(
                700,
                credited=True,
                credits=(
                    CreditFact(credit_type="cast", department=None, job=None, credit_order=2),
                ),
            )
        ],
    )
    assert _payload(prompt)["options"][0]["credited_on_this_film"] == [{"job": "actor"}]


def test_an_option_carries_a_recent_credit_change_on_this_film():
    prompt = build_tiebreak_request(
        _question(),
        [
            _candidate(
                700,
                in_change_stream=True,
                changes=(
                    ChangeFact(
                        credit_type="crew",
                        job="Director",
                        change="added",
                        changed_at=datetime(2026, 9, 10, tzinfo=UTC),
                    ),
                ),
            )
        ],
    )
    assert _payload(prompt)["options"][0]["recent_change_on_this_film"] == [
        {"job": "Director", "change": "added", "changed_at": "2026-09-10"}
    ]


def test_an_option_omits_the_facts_its_source_never_carried():
    """A null-padded option reads as evidence of absence — "known for nothing" — when it only
    means the source that would carry the fact is not the one that produced this candidate."""
    option = _payload(build_tiebreak_request(_question(), [_candidate(700)]))["options"][0]
    assert option == {"n": 1, "name": "Chris Evans"}


def test_no_option_carries_its_score_or_its_rank():
    """The band exists *because* the scores were indistinguishable. Showing them would either
    say nothing or invite the model to ratify an order popularity happened to set (D-21)."""
    prompt = build_tiebreak_request(
        _question(), [_candidate(700, popularity=90.0), _candidate(701, popularity=1.0)]
    )
    rendered = json.dumps(_payload(prompt)["options"])
    assert "score" not in rendered
    assert "popularity" not in rendered


def test_the_request_carries_the_film_the_story_and_the_mention():
    prompt = build_tiebreak_request(_question(), [_candidate(700)])
    payload = _payload(prompt)
    assert payload["film"] == {"title": "The Housemaid's Secret", "year": 2027}
    assert payload["story"]["title"] == "Chris Evans joins The Housemaid's Secret"
    assert payload["story"]["summary"].startswith("The composer is scoring")
    assert payload["mention"]["name_as_written"] == "Chris Evans"
    assert payload["mention"]["role"] == "composer"
    assert payload["mention"]["evidence_span"] == "Chris Evans will score the film"


def test_the_request_does_not_escape_non_latin_names():
    """The names most in need of disambiguation are the ones carrying native script, and
    escaping costs six characters apiece to hand the model `\\uXXXX` runs to compare."""
    prompt = build_tiebreak_request(
        _question(), [_candidate(700, name="Ryusuke Hamaguchi", original_name="濱口竜介")]
    )
    assert "濱口竜介" in prompt.user
    assert "\\u" not in prompt.user


def test_an_empty_shortlist_is_a_wiring_bug_not_a_call():
    """A mention with no candidates never routes to the band, so asking a closed-set question
    with an empty answer space would spend a call to be told "none" by construction."""
    with pytest.raises(ValueError, match="at least one option"):
        build_tiebreak_request(_question(), [])


def test_the_request_does_not_require_a_prefill():
    """`resolve` is a stage an operator may point at an OpenAI-compatible provider, where a
    trailing assistant turn is history rather than a continuation."""
    assert build_tiebreak_request(_question(), [_candidate(1)]).prefill_required is False


# --- the golden prompt -----------------------------------------------------------


def test_the_rendered_prompt_matches_the_golden_file():
    """The whole request, pinned byte for byte against `tests/fixtures/link/`.

    The substring assertions above say the load-bearing clauses are *present*; this says
    nothing changed that nobody meant to change. A prompt is measured behaviour — every clause
    in the instruction block is there because a failure mode put it there — so an edit should
    have to be made deliberately, against a diff a reviewer can read, rather than arriving as a
    silent rewording. Update the fixture in the same commit as the prompt, and say in the PR
    what moved.
    """
    prompt = build_tiebreak_request(
        _question(),
        [
            _candidate(
                700,
                known_for_titles=("Avengers: Endgame", "Knives Out"),
                known_for_department="Acting",
            ),
            _candidate(
                701,
                known_for_titles=("Tenet",),
                known_for_department="Sound",
                credited=True,
                credits=(
                    CreditFact(
                        credit_type="crew",
                        department="Sound",
                        job="Original Music Composer",
                        credit_order=None,
                    ),
                ),
            ),
        ],
    )
    rendered = f"{prompt.stable_prefix}\n\n--- user ---\n{prompt.user}\n"
    assert rendered == GOLDEN.read_text()


# --- the reply -------------------------------------------------------------------


def test_an_option_number_is_read_as_that_option():
    reply = parse_tiebreak_reply('{"option": 2, "reason": "scored Tenet"}', option_count=3)
    assert (reply.option, reply.reason, reply.usable) == (2, "scored Tenet", True)


def test_a_null_option_is_an_explicit_none():
    reply = parse_tiebreak_reply('{"option": null, "reason": "neither"}', option_count=3)
    assert (reply.option, reply.answered_none, reply.usable) == (None, True, True)


@pytest.mark.parametrize("spelling", ['"none"', '"None"', '"null"', '"nobody"'])
def test_none_is_accepted_in_the_spellings_models_actually_use(spelling):
    """Reading a correct decline as unparseable would push it onto the human queue, which is
    the opposite of what the queue is for."""
    reply = parse_tiebreak_reply(f'{{"option": {spelling}}}', option_count=3)
    assert reply.answered_none is True


def test_an_empty_option_is_a_lost_answer_and_not_a_decline():
    """A reply that lost its answer must not be read as a considered one: `unlinked` stamps
    `resolved_at` and is never asked again, so coercing this would turn a defective reply into
    a permanent decision — what the closed set exists to forbid."""
    reply = parse_tiebreak_reply('{"option": ""}', option_count=3)
    assert (reply.answered_none, reply.unparseable, reply.usable) == (False, True, False)


def test_a_numeric_string_is_the_same_answer_as_a_number():
    """A spelling difference between providers, not a different answer."""
    assert parse_tiebreak_reply('{"option": "2"}', option_count=3).option == 2


def test_an_answer_the_options_do_not_offer_is_rejected_not_coerced():
    """The link stage's out-of-list rule (`linker.py`), for the same reason: the nearest
    plausible option is how a numbering bug becomes a confident wrong person."""
    reply = parse_tiebreak_reply('{"option": 7}', option_count=3)
    assert (reply.option, reply.out_of_list, reply.usable) == (None, True, False)


def test_zero_is_out_of_list_rather_than_a_decline():
    """Options are numbered from 1, so 0 indexes nothing — and reading it as "none" would
    quietly accept an off-by-one in the numbering as a decision."""
    assert parse_tiebreak_reply('{"option": 0}', option_count=3).out_of_list is True


def test_prose_around_the_object_is_tolerated():
    raw = 'Sure, here you go:\n```json\n{"option": 1, "reason": "credited"}\n```'
    assert parse_tiebreak_reply(raw, option_count=2).option == 1


@pytest.mark.parametrize(
    "raw", ["", "I cannot tell", '{"answer": 1}', '{"option": "the first one"}', "[1]"]
)
def test_an_unusable_reply_is_unparseable_rather_than_an_answer(raw):
    reply = parse_tiebreak_reply(raw, option_count=2)
    assert (reply.usable, reply.unparseable) == (False, True)


def test_a_long_reason_is_bounded_on_the_way_in():
    """It is stored on the mention for D-25's reviewer; a model returning a paragraph does not
    get to grow the row."""
    reply = parse_tiebreak_reply(json.dumps({"option": 1, "reason": "x" * 500}), option_count=1)
    assert reply.reason is not None and len(reply.reason) == 240


# --- the call --------------------------------------------------------------------


async def test_the_call_records_a_usable_answer_as_a_parse_success():
    client = FakeClient('{"option": 1, "reason": "the composer"}')
    calls = CallLog()

    reply = await ask_tiebreak(
        client=client,
        model="claude-sonnet-4-6",
        question=_question(),
        options=[_candidate(700), _candidate(701)],
        calls=calls,
    )

    assert reply.option == 1
    assert calls.results[-1].parse_ok is True


async def test_an_unusable_answer_is_recorded_as_a_parse_failure_and_returned():
    """Returned, not raised: the mention has a deterministic decision already and it stands.
    Raising would discard the scoring work over a reply that did arrive."""
    client = FakeClient("no idea, sorry")
    calls = CallLog()

    reply = await ask_tiebreak(
        client=client,
        model="claude-sonnet-4-6",
        question=_question(),
        options=[_candidate(700)],
        calls=calls,
    )

    assert reply.usable is False
    assert calls.results[-1].parse_ok is False
