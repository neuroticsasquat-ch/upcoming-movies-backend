import json
from datetime import date
from uuid import uuid4

import pytest

from upmovies.link.linker import (
    StoryCandidates,
    apply_retrieval_link_decisions,
    build_retrieval_link_request,
    link_retrieval_story_batch,
    reject_zero_candidate_stories,
)
from upmovies.link.retrieval.index import indexed_film
from upmovies.link.retrieval.select import CandidateSet, ScoredCandidate
from upmovies.llm.client import CallLog
from upmovies.news.models import Story


class FakeClient:
    def __init__(self, response: str):
        self._response = response
        self.requests: list[dict] = []

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        from upmovies.llm.client import CallResult

        self.requests.append({"model": model, "system": system, "messages": messages})
        return calls.record(CallResult(text=self._response))


def _story(title="A headline", summary=""):
    return Story(
        id=uuid4(), source="X", url=f"https://e/{uuid4()}", title=title, raw={"summary": summary}
    )


def test_instructions_warn_against_interview_enthusiasm_headlines():
    from upmovies.link.linker import _RETRIEVAL_INSTRUCTIONS

    lowered = _RETRIEVAL_INSTRUCTIONS.lower()
    assert "teases" in lowered
    assert "reacts to" in lowered
    assert "no new production fact" in lowered
    assert "wishlist" in lowered
    assert "do not currently hold" in lowered


def test_instructions_flag_recirculated_old_news():
    from upmovies.link.linker import _RETRIEVAL_INSTRUCTIONS

    text = _RETRIEVAL_INSTRUCTIONS.lower()
    assert "recirculat" in text or "re-report" in text or "already-known" in text
    assert "publication date does not make it new" in text or "fresh publication date" in text


def test_instructions_flag_release_calendar_listicles():
    """NEU-451: weekly/monthly release-calendar listicles (a multi-film list where the
    tracked film is one entry among many) are not-news:roundup even when they state a
    release date — a calendar restating a scheduled date is not an announcement."""
    from upmovies.link.linker import _RETRIEVAL_INSTRUCTIONS

    text = _RETRIEVAL_INSTRUCTIONS.lower()
    assert "listicle" in text
    assert "one entry among many" in text
    assert "release-date announcement" in text


def test_instructions_cover_sibling_spinoff_trap():
    from upmovies.link.linker import _RETRIEVAL_INSTRUCTIONS

    lowered = _RETRIEVAL_INSTRUCTIONS.lower()
    # A distinct, identified sibling film (spin-off/sequel/prequel) that is not
    # itself among the story's candidates must be named as a no-match trap, distinct from the
    # existing "the next Batman" generic-reference rule.
    assert "spin-off" in lowered
    assert "not itself tracked" in lowered


def test_instructions_cover_original_to_sequel_trap():
    from upmovies.link.linker import _RETRIEVAL_INSTRUCTIONS

    lowered = _RETRIEVAL_INSTRUCTIONS.lower()
    # The sibling trap must run BOTH directions: a story about the original/earlier film
    # is not its tracked sequel merely because they share a title stem.
    assert "title stem" in lowered
    assert "both directions" in lowered


def test_instructions_cover_medium_mismatch_trap():
    from upmovies.link.linker import _RETRIEVAL_INSTRUCTIONS

    lowered = _RETRIEVAL_INSTRUCTIONS.lower()
    # NEU-483 #5: a story about a same-franchise TV series/game is not "about" the
    # tracked film merely because it shares characters — that's a different production.
    assert "different medium" in lowered
    assert "tv series" in lowered
    assert "video game" in lowered


# --- the retrieval request builder (NEU-998) ---------------------------------------


def _candidates(*titles, limit=10):
    films = [indexed_film(film_id=uuid4(), title=t, year=2026) for t in titles]
    return CandidateSet(
        scored=tuple(ScoredCandidate(film=f, score=1.0) for f in films), limit=limit
    )


def _batch(*pairs):
    return [StoryCandidates(story=story, candidates=candidates) for story, candidates in pairs]


def test_retrieval_request_sends_no_roster_and_no_cached_block():
    """The point of the rewrite: the prefix is instructions only. Below Haiku 4.5's
    4096-token cache floor, so marking it cached would buy nothing and misreport why
    `cache_creation_input_tokens` is zero (spec §4.2)."""
    system, _ = build_retrieval_link_request(
        _batch((_story(), _candidates("Runner"))), date(2026, 6, 25)
    )
    assert len(system) == 1
    assert "cache_control" not in system[0]
    assert "ROSTER" not in system[0]["text"]


def test_retrieval_request_carries_each_story_its_own_numbered_candidates():
    first, second = _story(title="Runner news"), _story(title="Sunup news")
    _, messages = build_retrieval_link_request(
        _batch((first, _candidates("Runner", "Runner Up")), (second, _candidates("Sunup"))),
        date(2026, 6, 25),
    )
    payload = json.loads(messages[0]["content"])
    assert payload["as_of_date"] == "2026-06-25"
    one, two = payload["stories"]
    assert one["id"] == str(first.id) and one["title"] == "Runner news"
    assert [c["n"] for c in one["candidates"]] == [1, 2]
    assert [c["title"] for c in one["candidates"]] == ["Runner", "Runner Up"]
    # Numbering restarts per story — index 1 is a different film in each list.
    assert [c["n"] for c in two["candidates"]] == [1]
    assert two["candidates"][0]["title"] == "Sunup"


def test_retrieval_request_shows_only_the_capped_candidates():
    story = _story()
    _, messages = build_retrieval_link_request(
        _batch((story, _candidates("A", "B", "C", limit=2))), date(2026, 6, 25)
    )
    candidates = json.loads(messages[0]["content"])["stories"][0]["candidates"]
    assert [c["title"] for c in candidates] == ["A", "B"]


def test_retrieval_request_carries_the_story_dek():
    story = _story(title="Runner news", summary="A dek about Runner.")
    _, messages = build_retrieval_link_request(
        _batch((story, _candidates("Runner"))), date(2026, 6, 25)
    )
    assert json.loads(messages[0]["content"])["stories"][0]["summary"] == "A dek about Runner."


def test_retrieval_request_rejects_a_zero_candidate_story():
    """A zero-candidate story never reaches the model at all (ADR-0009) — an empty list
    in the prompt would leave the reply nothing valid to index into."""
    story = _story()
    with pytest.raises(ValueError):
        build_retrieval_link_request(
            _batch((story, CandidateSet(scored=(), limit=10))), date(2026, 6, 25)
        )


def test_retrieval_instructions_ask_for_a_per_story_index():
    from upmovies.link.linker import _RETRIEVAL_INSTRUCTIONS

    lowered = _RETRIEVAL_INSTRUCTIONS.lower()
    assert "candidate" in lowered
    assert "roster" not in lowered
    # The reply must index into *that story's* list, and saying so is what makes an
    # out-of-list index rejectable rather than a coercion (NEU-999).
    assert "that story's candidate list" in lowered


def test_retrieval_instructions_retain_the_franchise_traps_in_full():
    """The ticket is explicit: a smaller candidate set makes these paragraphs more
    necessary, not less. Rendered from `_CLASSIFICATION_RULES`, which outlived the roster
    prompt it was shared with (NEU-1004) — the text is unchanged."""
    from upmovies.link.linker import _RETRIEVAL_INSTRUCTIONS

    lowered = _RETRIEVAL_INSTRUCTIONS.lower()
    for phrase in (
        "franchise-generic casting/announcement traps",
        "spin-off",
        "not itself tracked",
        "both directions",
        "title stem",
        "different medium",
        "video game",
        "wishlist",
        "listicle",
        "one entry among many",
    ):
        assert phrase in lowered, phrase
    assert "the story is not about any tracked film. most stories are no-match" in lowered
    assert _RETRIEVAL_INSTRUCTIONS.count("franchise trap runs in BOTH directions") == 1


def test_retrieval_request_does_not_escape_non_latin_titles():
    """Escaped, one CJK character costs six — and `original_title` is rendered precisely
    when it differs from the title, which is exactly the non-Latin case (spec §4.3)."""
    story = _story()
    film = indexed_film(film_id=uuid4(), title="Godzilla Minus One", original_title="ゴジラ-1.0")
    candidates = CandidateSet(scored=(ScoredCandidate(film=film, score=1.0),), limit=10)
    _, messages = build_retrieval_link_request(_batch((story, candidates)), date(2026, 6, 25))
    assert "ゴジラ" in messages[0]["content"]
    assert "\\u" not in messages[0]["content"]


# --- applying retrieval decisions (NEU-999) -----------------------------------------


def _reply(*decisions) -> str:
    return json.dumps(list(decisions))


def _decision(story, film, *, confidence=0.95, reason="about", **extra):
    return {"id": str(story.id), "film": film, "confidence": confidence, "reason": reason, **extra}


class TestRetrievalIndexIsLocalToTheStory:
    def test_each_story_resolves_index_1_to_its_own_first_candidate(self):
        first, second = _story(title="Runner news"), _story(title="Sunup news")
        runner, sunup = _candidates("Runner"), _candidates("Sunup")
        batch = _batch((first, runner), (second, sunup))

        result = apply_retrieval_link_decisions(
            raw=_reply(_decision(first, 1), _decision(second, 1)), batch=batch, floor=0.7
        )

        assert result.linked == 2
        # The whole difference from the deleted global roster index: the same number
        # names a different film in each story's list.
        assert first.film_id == runner.film_ids[0]
        assert second.film_id == sunup.film_ids[0]
        assert first.film_id != second.film_id

    def test_an_index_past_the_storys_list_is_rejected_not_coerced(self):
        story = _story()
        batch = _batch((story, _candidates("Runner", "Runner Up")))

        result = apply_retrieval_link_decisions(
            raw=_reply(_decision(story, 3)), batch=batch, floor=0.7
        )

        assert (result.linked, result.rejected) == (0, 1)
        assert story.link_status == "rejected"
        assert story.film_id is None
        assert story.link_confidence is None
        # Its own note: a systematic numbering regression in the one lossy stage must stay
        # queryable rather than hide inside the `no-match` the model never actually reached.
        assert story.link_note == "out-of-list"

    def test_an_index_valid_only_in_another_storys_list_is_rejected(self):
        """The rejection the global-roster scheme could not express (spec §4.2): index 2 is
        a real film — in the *other* story's list."""
        wide, narrow = _story(title="Runner news"), _story(title="Sunup news")
        batch = _batch((wide, _candidates("Runner", "Runner Up")), (narrow, _candidates("Sunup")))

        apply_retrieval_link_decisions(
            raw=_reply(_decision(wide, 2), _decision(narrow, 2)), batch=batch, floor=0.7
        )

        assert wide.link_status == "linked"
        assert narrow.link_status == "rejected"
        assert narrow.link_note == "out-of-list"

    @pytest.mark.parametrize("film", [0, -1, "1", 1.5, True])
    def test_an_index_that_names_no_candidate_is_rejected(self, film):
        # Numbering starts at 1, so 0 names nothing; a string or a float is not an index at
        # all. `True` is an `int` in Python and would otherwise resolve to candidate 1.
        story = _story()
        batch = _batch((story, _candidates("Runner", "Runner Up")))

        apply_retrieval_link_decisions(raw=_reply(_decision(story, film)), batch=batch, floor=0.7)

        assert story.link_status == "rejected"
        assert story.link_note == "out-of-list"

    def test_a_null_film_is_a_no_match_not_an_out_of_list_reply(self):
        # The model naming no film is the answer the prompt asks for most often; only a
        # number that names nothing is a defective reply.
        story = _story()
        batch = _batch((story, _candidates("Runner")))

        apply_retrieval_link_decisions(
            raw=_reply(_decision(story, None, confidence=0.0, reason="no-match")),
            batch=batch,
            floor=0.7,
        )

        assert story.link_note == "no-match"


class TestDecisionRules:
    def test_below_floor_is_rejected(self):
        story = _story()
        batch = _batch((story, _candidates("Runner")))

        apply_retrieval_link_decisions(
            raw=_reply(_decision(story, 1, confidence=0.4)), batch=batch, floor=0.7
        )

        assert story.link_status == "rejected"
        assert story.link_note == "below-floor"

    def test_not_news_downstream_recirculation_is_rejected(self):
        story = _story(title="Kim Kardashian's son Psalm makes acting debut in Angry Birds Movie 3")
        batch = _batch((story, _candidates("The Angry Birds Movie 3")))

        result = apply_retrieval_link_decisions(
            raw=_reply(
                _decision(story, 1, confidence=0.0, reason="not-news", category="downstream")
            ),
            batch=batch,
            floor=0.7,
        )

        assert result.linked == 0
        assert story.link_status == "rejected"
        assert story.link_note == "not-news:downstream"

    def test_not_news_keeps_its_category(self):
        story = _story()
        batch = _batch((story, _candidates("Runner")))

        apply_retrieval_link_decisions(
            raw=_reply(_decision(story, 1, reason="not-news", category="reaction")),
            batch=batch,
            floor=0.7,
        )

        assert story.link_note == "not-news:reaction"

    def test_mention_is_rejected(self):
        story = _story()
        batch = _batch((story, _candidates("Runner")))

        apply_retrieval_link_decisions(
            raw=_reply(_decision(story, 1, reason="mention")), batch=batch, floor=0.7
        )

        assert story.link_note == "mention"

    def test_a_story_the_model_skipped_is_rejected_no_decision(self):
        story = _story()
        batch = _batch((story, _candidates("Runner")))

        result = apply_retrieval_link_decisions(raw=_reply(), batch=batch, floor=0.7)

        assert result.rejected == 1
        assert story.link_note == "no-decision"


class TestZeroCandidateRejection:
    def test_it_stamps_its_own_note_and_counts_the_stories(self):
        stories = [_story(), _story()]

        assert reject_zero_candidate_stories(stories) == 2

        for story in stories:
            assert story.link_status == "rejected"
            # Deliberately not folded into `no-match` (ADR-0009): stories lost to a
            # retrieval bug must stay queryable and re-runnable within the recency window.
            assert story.link_note == "no-candidates"
            assert story.film_id is None
            assert story.link_confidence is None
            assert story.linked_at is not None

    def test_nothing_to_reject_is_a_no_op(self):
        assert reject_zero_candidate_stories([]) == 0


class TestLinkRetrievalStoryBatch:
    async def test_it_calls_the_model_once_and_applies_the_reply(self):
        story = _story()
        batch = _batch((story, _candidates("Runner")))
        client = FakeClient(_reply(_decision(story, 1)))

        result = await link_retrieval_story_batch(
            client=client,
            model="m",
            batch=batch,
            floor=0.7,
            run_date=date(2026, 6, 25),
            calls=CallLog(),
        )

        assert result.linked == 1
        assert len(client.requests) == 1
        assert "cache_control" not in client.requests[0]["system"][0]

    async def test_an_empty_batch_makes_no_call(self):
        client = FakeClient("[]")

        result = await link_retrieval_story_batch(
            client=client,
            model="m",
            batch=[],
            floor=0.7,
            run_date=date(2026, 6, 25),
            calls=CallLog(),
        )

        assert (result.linked, result.rejected) == (0, 0)
        assert client.requests == []

    async def test_an_unusable_reply_is_recorded_as_a_parse_failure_and_raised(self):
        # `link` raises on unusable output rather than swallowing it, and the parse
        # outcome is recorded either way.
        story = _story()
        calls = CallLog()
        client = FakeClient("not json at all")

        with pytest.raises(json.JSONDecodeError):
            await link_retrieval_story_batch(
                client=client,
                model="m",
                batch=_batch((story, _candidates("Runner"))),
                floor=0.7,
                run_date=date(2026, 6, 25),
                calls=calls,
            )

        assert calls.results[0].parse_ok is False
