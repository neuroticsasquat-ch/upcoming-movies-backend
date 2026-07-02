import json
from datetime import date
from uuid import uuid4

from upmovies.link.linker import (
    apply_link_decisions,
    build_batch_request,
    build_link_request,
    link_story_batch,
)
from upmovies.link.roster import Roster, RosterEntry
from upmovies.news.models import Story


class FakeClient:
    def __init__(self, response: str):
        self._response = response
        self.calls: list[dict] = []

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        from upmovies.llm.client import Usage

        self.calls.append({"model": model, "system": system, "messages": messages})
        return self._response, Usage()


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
    result, _usage = await link_story_batch(
        client=client,
        model="m",
        roster=_roster(film_id),
        stories=[story],
        floor=floor,
        run_date=date(2026, 6, 25),
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
    system = client.calls[0]["system"]
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


def test_build_batch_request_carries_custom_id_and_cached_block():
    req = build_batch_request(
        custom_id="3",
        model="link-m",
        roster=_roster(uuid4()),
        stories=[_story()],
        run_date=date(2026, 6, 25),
    )
    assert req.custom_id == "3"
    assert req.model == "link-m"
    assert req.max_tokens == 2048
    assert req.system[0]["cache_control"] == {"type": "ephemeral"}
    assert "entity-linking classifier" in req.system[0]["text"]


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
