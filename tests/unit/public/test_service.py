from upmovies.news.models import Story
from upmovies.public.service import outlet_label


def _story(*, outlet: str | None, source: str) -> Story:
    return Story(source=source, url="https://x", title="t", outlet=outlet)


def test_outlet_label_prefers_resolved_outlet():
    assert outlet_label(_story(outlet="Deadline", source="Google News: per-film")) == "Deadline"


def test_outlet_label_falls_back_to_source_when_outlet_none():
    assert outlet_label(_story(outlet=None, source="Deadline")) == "Deadline"
