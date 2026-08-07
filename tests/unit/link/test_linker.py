import json
from datetime import date
from uuid import uuid4

import pytest

from upmovies.link.linker import (
    StoryCandidates,
    apply_link_decisions,
    build_link_request,
    build_retrieval_link_request,
    link_story_batch,
)
from upmovies.link.retrieval.index import indexed_film
from upmovies.link.retrieval.select import CandidateSet, ScoredCandidate
from upmovies.link.roster import Roster, RosterEntry
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


def _roster(film_id):
    entry = RosterEntry(
        film_id=film_id, title="Runner", original_title=None, year=2026, overview=None, genres=[]
    )
    return Roster(entries=[entry], text='#1 "Runner" (2026)')


def _story(title="A headline", summary=""):
    return Story(
        id=uuid4(), source="X", url=f"https://e/{uuid4()}", title=title, raw={"summary": summary}
    )


async def _run(response, *, floor=0.7):
    film_id = uuid4()
    story = _story()
    client = FakeClient(response(str(story.id)))
    result = await link_story_batch(
        client=client,
        model="m",
        roster=_roster(film_id),
        stories=[story],
        floor=floor,
        run_date=date(2026, 6, 25),
        calls=CallLog(),
    )
    return story, film_id, client, result


async def test_about_high_confidence_links():
    story, film_id, _, result = await _run(
        lambda sid: json.dumps([{"id": sid, "film": 1, "confidence": 0.95, "reason": "about"}])
    )
    assert result.linked == 1 and result.rejected == 0
    assert story.link_status == "linked"
    assert story.film_id == film_id
    assert story.link_confidence == 0.95
    assert story.linked_at is not None
    assert story.link_note is None


async def test_mention_is_rejected():
    story, _, _, result = await _run(
        lambda sid: json.dumps([{"id": sid, "film": 1, "confidence": 0.9, "reason": "mention"}])
    )
    assert result.rejected == 1
    assert story.link_status == "rejected"
    assert story.film_id is None
    assert story.link_confidence is None
    assert story.link_note == "mention"


async def test_below_floor_is_rejected():
    story, _, _, _ = await _run(
        lambda sid: json.dumps([{"id": sid, "film": 1, "confidence": 0.4, "reason": "about"}])
    )
    assert story.link_status == "rejected"
    assert story.link_note == "below-floor"


async def test_no_match_is_rejected():
    story, _, _, _ = await _run(
        lambda sid: json.dumps([{"id": sid, "film": None, "confidence": 0.0, "reason": "no-match"}])
    )
    assert story.link_status == "rejected"
    assert story.link_note == "no-match"


async def test_roster_is_sent_as_cached_system_block():
    _, _, client, _ = await _run(
        lambda sid: json.dumps([{"id": sid, "film": None, "confidence": 0.0, "reason": "no-match"}])
    )
    system = client.requests[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Runner" in system[0]["text"]


async def test_response_wrapped_in_prose_is_still_parsed():
    story, film_id, _, result = await _run(
        lambda sid: (
            "Here you go:\n```json\n"
            + json.dumps([{"id": sid, "film": 1, "confidence": 0.9, "reason": "about"}])
            + "\n```"
        )
    )
    assert result.linked == 1
    assert story.link_status == "linked"


async def test_omitted_story_is_rejected_no_decision():
    story, _, _, result = await _run(lambda sid: json.dumps([]))  # model returned nothing
    assert result.rejected == 1
    assert story.link_status == "rejected"
    assert story.link_note == "no-decision"


def test_build_link_request_uses_cached_roster_and_story_payload():
    roster = _roster(uuid4())
    stories = [_story(title="Runner news")]
    system, messages = build_link_request(roster, stories, date(2026, 6, 25))
    # cached roster system block unchanged apart from the constant's new sentence
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    payload = json.loads(messages[0]["content"])
    assert payload["as_of_date"] == "2026-06-25"
    assert isinstance(payload["stories"], list)
    assert payload["stories"][0]["title"] == "Runner news"


def test_apply_link_decisions_links_about_high_confidence():
    film_id = uuid4()
    story = _story()
    raw = json.dumps([{"id": str(story.id), "film": 1, "confidence": 0.95, "reason": "about"}])
    result = apply_link_decisions(raw=raw, stories=[story], roster=_roster(film_id), floor=0.7)
    assert result.linked == 1
    assert story.link_status == "linked"
    assert story.film_id == film_id


async def test_not_news_with_category_is_rejected():
    story, _, _, result = await _run(
        lambda sid: json.dumps(
            [
                {
                    "id": sid,
                    "film": 1,
                    "confidence": 0.9,
                    "reason": "not-news",
                    "category": "reaction",
                }
            ]
        )
    )
    assert result.rejected == 1 and result.linked == 0
    assert story.link_status == "rejected"
    assert story.film_id is None
    assert story.link_confidence is None
    assert story.link_note == "not-news:reaction"


async def test_not_news_without_category_is_rejected():
    story, _, _, _ = await _run(
        lambda sid: json.dumps([{"id": sid, "film": 1, "confidence": 0.9, "reason": "not-news"}])
    )
    assert story.link_status == "rejected"
    assert story.link_note == "not-news"


async def test_not_news_unknown_category_falls_back_to_bare_note():
    story, _, _, _ = await _run(
        lambda sid: json.dumps(
            [{"id": sid, "film": 1, "confidence": 0.9, "reason": "not-news", "category": "bogus"}]
        )
    )
    assert story.link_note == "not-news"


def test_instructions_warn_against_interview_enthusiasm_headlines():
    from upmovies.link.linker import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    assert "teases" in lowered
    assert "reacts to" in lowered
    assert "no new production fact" in lowered
    assert "wishlist" in lowered
    assert "do not currently hold" in lowered


async def test_not_news_downstream_recirculation_is_rejected():
    film_id = uuid4()
    story = _story(title="Kim Kardashian's son Psalm makes acting debut in Angry Birds Movie 3")
    raw = (
        '[{"id": "' + str(story.id) + '", "film": 1, "confidence": 0.0, '
        '"reason": "not-news", "category": "downstream"}]'
    )
    result = apply_link_decisions(raw=raw, stories=[story], roster=_roster(film_id), floor=0.7)
    assert result.linked == 0
    assert story.link_status == "rejected"
    assert story.link_note == "not-news:downstream"


def test_instructions_flag_recirculated_old_news():
    from upmovies.link.linker import _INSTRUCTIONS

    text = _INSTRUCTIONS.lower()
    assert "recirculat" in text or "re-report" in text or "already-known" in text
    assert "publication date does not make it new" in text or "fresh publication date" in text


def test_instructions_flag_release_calendar_listicles():
    """NEU-451: weekly/monthly release-calendar listicles (a multi-film list where the
    tracked film is one entry among many) are not-news:roundup even when they state a
    release date — a calendar restating a scheduled date is not an announcement."""
    from upmovies.link.linker import _INSTRUCTIONS

    text = _INSTRUCTIONS.lower()
    assert "listicle" in text
    assert "one entry among many" in text
    assert "release-date announcement" in text


def test_instructions_cover_sibling_spinoff_trap():
    from upmovies.link.linker import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    # A distinct, identified sibling film (spin-off/sequel/prequel) that is not
    # itself in the roster must be named as a no-match trap, distinct from the
    # existing "the next Batman" generic-reference rule.
    assert "spin-off" in lowered
    assert "not itself tracked" in lowered


def test_instructions_cover_original_to_sequel_trap():
    from upmovies.link.linker import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    # The sibling trap must run BOTH directions: a story about the original/earlier film
    # is not its tracked sequel merely because they share a title stem.
    assert "title stem" in lowered
    assert "both directions" in lowered


def test_instructions_cover_medium_mismatch_trap():
    from upmovies.link.linker import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
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
    necessary, not less. Both prompts render from one shared block so they cannot drift."""
    from upmovies.link.linker import _INSTRUCTIONS, _RETRIEVAL_INSTRUCTIONS

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
    # And the definitions block is the same one, word for word: both prompts render from
    # `_CLASSIFICATION_RULES`, so this is the shared text, not a second copy of it.
    assert "the story is not about any tracked film. most stories are no-match" in lowered
    assert _INSTRUCTIONS.count("franchise trap runs in BOTH directions") == 1


def test_retrieval_request_does_not_escape_non_latin_titles():
    """Escaped, one CJK character costs six — and `original_title` is rendered precisely
    when it differs from the title, which is exactly the non-Latin case (spec §4.3)."""
    story = _story()
    film = indexed_film(film_id=uuid4(), title="Godzilla Minus One", original_title="ゴジラ-1.0")
    candidates = CandidateSet(scored=(ScoredCandidate(film=film, score=1.0),), limit=10)
    _, messages = build_retrieval_link_request(_batch((story, candidates)), date(2026, 6, 25))
    assert "ゴジラ" in messages[0]["content"]
    assert "\\u" not in messages[0]["content"]
