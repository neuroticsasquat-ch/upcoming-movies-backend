from datetime import UTC, datetime
from uuid import UUID

from upmovies.news.models import Story
from upmovies.public.sources import SOURCE_CAP, cap_sources, outlet_label, source_url


def _dt(day: int) -> datetime:
    return datetime(2025, 3, day, tzinfo=UTC)


def _story(
    *,
    source: str = "Deadline",
    outlet: str | None = None,
    published_at: datetime | None = None,
    id: UUID | None = None,
    url: str | None = None,
    title: str = "t",
) -> Story:
    return Story(
        id=id,
        source=source,
        outlet=outlet,
        url=url or f"https://x/{source}/{published_at}",
        title=title,
        published_at=published_at,
    )


def test_source_cap_default_is_three():
    assert SOURCE_CAP == 3


def test_outlet_label_prefers_resolved_outlet():
    assert outlet_label(_story(outlet="Deadline", source="Google News: per-film")) == "Deadline"


def test_outlet_label_falls_back_to_source_when_outlet_none():
    assert outlet_label(_story(outlet=None, source="Deadline")) == "Deadline"


def test_cap_sources_returns_distinct_outlets_newest_first():
    stories = [
        _story(source="Variety", published_at=_dt(2)),
        _story(source="Deadline", published_at=_dt(5)),
    ]
    assert [outlet_label(s) for s in cap_sources(stories)] == ["Deadline", "Variety"]


def test_cap_sources_trims_to_three_keeping_most_recent_outlets():
    stories = [
        _story(source="Deadline", published_at=_dt(5)),
        _story(source="Variety", published_at=_dt(4)),
        _story(source="The Hollywood Reporter", published_at=_dt(3)),
        _story(source="Collider", published_at=_dt(2)),
        _story(source="Empire", published_at=_dt(1)),
    ]
    result = cap_sources(stories)
    assert [outlet_label(s) for s in result] == ["Deadline", "Variety", "The Hollywood Reporter"]


def test_cap_sources_dedupes_outlet_keeping_most_recent():
    stories = [
        _story(source="Deadline", url="https://d/old", published_at=_dt(1)),
        _story(source="Deadline", url="https://d/new", published_at=_dt(5)),
        _story(source="Variety", published_at=_dt(3)),
    ]
    result = cap_sources(stories)
    assert [outlet_label(s) for s in result] == ["Deadline", "Variety"]
    assert result[0].url == "https://d/new"  # kept the most-recent Deadline


def test_cap_sources_dedupe_key_uses_outlet_label_fallback():
    # one row resolves via outlet, the other via source — same publisher, dedupe to one
    stories = [
        _story(source="Deadline", outlet=None, published_at=_dt(1)),
        _story(source="Google News: per-film", outlet="Deadline", published_at=_dt(5)),
    ]
    result = cap_sources(stories)
    assert [outlet_label(s) for s in result] == ["Deadline"]
    assert result[0].published_at == _dt(5)


def test_cap_sources_null_published_at_sorts_last():
    stories = [
        _story(source="NoDate", published_at=None),
        _story(source="Deadline", published_at=_dt(2)),
        _story(source="Variety", published_at=_dt(1)),
    ]
    assert [outlet_label(s) for s in cap_sources(stories)] == ["Deadline", "Variety", "NoDate"]


def test_cap_sources_id_tiebreak_is_deterministic():
    id_a = UUID("00000000-0000-0000-0000-00000000000a")
    id_b = UUID("00000000-0000-0000-0000-00000000000b")
    same = _dt(3)
    s_a = _story(source="Alpha", id=id_a, published_at=same)
    s_b = _story(source="Bravo", id=id_b, published_at=same)
    # same published_at → id ascending, regardless of input order
    assert [s.id for s in cap_sources([s_b, s_a])] == [id_a, id_b]
    assert [s.id for s in cap_sources([s_a, s_b])] == [id_a, id_b]


def test_cap_sources_normalizes_outlet_for_dedupe():
    stories = [
        _story(source="The Hollywood Reporter", url="https://thr/new", published_at=_dt(5)),
        _story(source="hollywood   reporter", url="https://thr/old", published_at=_dt(1)),
        _story(source="Variety", published_at=_dt(3)),
    ]
    result = cap_sources(stories)
    # casefold + "the " strip + whitespace-collapse make these one outlet;
    # the verbatim label of the most-recent kept story is displayed.
    assert [outlet_label(s) for s in result] == ["The Hollywood Reporter", "Variety"]


def test_source_url_prefers_resolved():
    s = Story(
        source="Google News: per-film",
        url="https://news.google.com/rss/articles/X",
        title="t",
        resolved_url="https://variety.com/real",
    )
    assert source_url(s) == "https://variety.com/real"


def test_source_url_falls_back_to_google_when_unresolved():
    s = Story(
        source="Google News: per-film", url="https://news.google.com/rss/articles/X", title="t"
    )
    assert source_url(s) == "https://news.google.com/rss/articles/X"
