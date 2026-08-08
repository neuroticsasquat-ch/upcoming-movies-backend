"""Loaders for the retrieval recall oracle's two fixtures.

`validation_set.json` supplies the labeled stories, `retrieval_catalog.json` the films
they are labeled against. Both are read by the recall gate
(`tests/integration/link/retrieval/test_recall_oracle.py`) and by the drift check that
keeps the two in step (`tests/unit/link/retrieval/test_recall_oracle_fixture.py`), which
is why the loading lives here rather than in either of them."""

import json
from pathlib import Path
from typing import NamedTuple

from upmovies.link.validation import ValidationItem, load_validation_set

FIXTURE_DIR = Path(__file__).resolve().parent / "link"
VALIDATION_SET = FIXTURE_DIR / "validation_set.json"
RETRIEVAL_CATALOG = FIXTURE_DIR / "retrieval_catalog.json"


class CatalogFilm(NamedTuple):
    """One fixture film — only the fields retrieval scores on."""

    tmdb_id: int
    title: str
    original_title: str | None
    alternative_titles: tuple[str, ...]


def about_items() -> list[ValidationItem]:
    """The labeled `about` rows — the only ones carrying a film to be retrieved."""
    return [item for item in load_validation_set(VALIDATION_SET).items if item.relation == "about"]


def labeled_tmdb_id(item: ValidationItem) -> int:
    """The TMDB id an `about` row is labeled with.

    The field is optional on `ValidationItem` because `mention` and `none` rows carry no
    film; its validator already rules that out for `about`, which is all this reads."""
    assert item.expected_film_tmdb_id is not None
    return item.expected_film_tmdb_id


def retrieval_catalog() -> list[CatalogFilm]:
    """The fixture catalog the recall gate scores against."""
    return [
        CatalogFilm(
            tmdb_id=entry["tmdb_id"],
            title=entry["title"],
            original_title=entry["original_title"],
            alternative_titles=tuple(entry["alternative_titles"]),
        )
        for entry in json.loads(RETRIEVAL_CATALOG.read_text())
    ]
