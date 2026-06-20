import json
from uuid import uuid4

from upmovies.link.linker import link_story_batch
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
