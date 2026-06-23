"""The labeled validation set for measuring linking/clustering accuracy. Self-contained:
each item embeds the story text plus its label, keyed to films by TMDB id (stable across
databases) rather than local uuids."""

import json
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


def load_validation_set(path: str | Path) -> list[ValidationItem]:
    data = json.loads(Path(path).read_text())
    return [ValidationItem.model_validate(row) for row in data]
