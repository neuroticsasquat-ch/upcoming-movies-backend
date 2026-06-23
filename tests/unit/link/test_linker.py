import json
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

    async def complete(self, *, model, system, messages, max_tokens=4096) -> str:
        self.calls.append({"model": model, "system": system, "messages": messages})
        return self._response


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
        client=client, model="m", roster=_roster(film_id), stories=[story], floor=floor
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
    system, messages = build_link_request(_roster(uuid4()), [_story(title="Runner news")])
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Runner" in system[0]["text"]
    payload = json.loads(messages[0]["content"])
    assert payload[0]["title"] == "Runner news"


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
        custom_id="3", model="link-m", roster=_roster(uuid4()), stories=[_story()]
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
