"""The labeled validation set for measuring linking/clustering accuracy. Self-contained:
each item embeds the story text plus its label, keyed to films by TMDB id (stable across
databases) rather than local uuids."""

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator


class ValidationItem(BaseModel):
    url: str
    source: str
    title: str
    summary: str = ""
    relation: Literal["about", "mention", "none"]
    expected_film_tmdb_id: int | None = None
    event_type: str | None = None
    event_group: str | None = None  # free-text group label for cluster scoring (about items)
    is_production_news: bool | None = None  # about-only: False = excluded (not production news)
    exclusion_category: (
        Literal["reaction", "roundup", "streaming-move", "interview-quote", "downstream", "other"]
        | None
    ) = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "ValidationItem":
        if self.relation == "about" and self.expected_film_tmdb_id is None:
            raise ValueError("an 'about' item must set expected_film_tmdb_id")
        if self.relation != "about" and self.expected_film_tmdb_id is not None:
            raise ValueError("only 'about' items may set expected_film_tmdb_id")
        if self.relation != "about" and (
            self.is_production_news is not None or self.exclusion_category is not None
        ):
            raise ValueError("production-news fields may be set only on 'about' items")
        if self.exclusion_category is not None and self.is_production_news is not False:
            raise ValueError("exclusion_category requires is_production_news=False")
        return self


class ValidationSet(BaseModel):
    """The labeled items plus the date the catalog should be read as of.

    `as_of_date` exists because the set is a *dated* observation, not a timeless one. Its
    stories are news from the weeks around labeling, and the films they name are upcoming
    only until they release — roughly a fifth of the active catalog turns over per month.
    Scored against today's catalog, the fixture's own subjects fall out of scope and read as
    recall failures of the path under test; scored as of the labeling date, it stays a stable
    oracle. The date lives with the data so a caller cannot supply the wrong one.

    Deliberately *not* list-like: mimicking a sequence on a pydantic model shadows
    `BaseModel.__iter__` (which yields field pairs, and is what `dict(model)` consumes) and
    makes an empty set falsy. Readers that only want the labels say `.items`.
    """

    as_of_date: date | None = None
    items: list[ValidationItem]


def load_validation_set(path: str | Path) -> ValidationSet:
    """Load a validation set in either shape: the `{as_of_date, items}` envelope, or a bare
    list of items (pre-pin fixtures, which carry no date and fall back to wall clock)."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        return ValidationSet(items=[ValidationItem.model_validate(row) for row in data])
    return ValidationSet.model_validate(data)
