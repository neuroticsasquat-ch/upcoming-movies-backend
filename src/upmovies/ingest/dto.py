from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field


class RetrievalHealthOut(BaseModel):
    """One run's candidate-retrieval health — the soft tier of the two-tier guard (ADR-0010).

    The stored counts are the state and the rates are views onto them, so a rate can never
    disagree with the numbers it was taken from. Every rate is `None` on an empty
    denominator rather than `0.0`: a zero zero-candidate rate reads as "retrieval is
    healthy", which is precisely the opposite of what a run that retrieved nothing says.

    `roster_pick_recall` is **not truth**, and a bare percentage must not be read as one.
    It measures retrieval against the roster's picks, and the roster makes false positives
    — so retrieval declining to surface one scores as a loss here while being a win. The
    cutover gate hand-adjudicates every miss for exactly that reason."""

    stories_retrieved: int
    """The denominator: every story retrieval ran over, zero-candidate ones included."""
    zero_candidate_stories: int
    saturated_stories: int
    mean_candidates: float | None
    roster_picks: int
    """Stories the roster linked, so retrieval had a pick to be measured against — a
    separate, much smaller denominator than `stories_retrieved`."""
    roster_picks_retrieved: int

    @computed_field
    @property
    def zero_candidate_rate(self) -> float | None:
        """The drift signal: stories rejected with no model ever seeing them (ADR-0009)."""
        return self._rate(self.zero_candidate_stories, self.stories_retrieved)

    @computed_field
    @property
    def saturation_rate(self) -> float | None:
        """How often the cap discarded a film that had cleared the threshold."""
        return self._rate(self.saturated_stories, self.stories_retrieved)

    @computed_field
    @property
    def roster_pick_recall(self) -> float | None:
        """Share of the roster's picks retrieval would have offered. See the class docstring
        before reading this as a score."""
        return self._rate(self.roster_picks_retrieved, self.roster_picks)

    @staticmethod
    def _rate(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None


class RetrievalHealthPointOut(RetrievalHealthOut):
    """One run's health stamped with the run it came from — a point in the trend series.

    The trend is the point of the surface, not the snapshot: the failure it guards is slow
    drift, where a single run's number says nothing and the shape over a fortnight says
    everything."""

    run_id: UUID
    run_status: str
    started_at: datetime


class RunLLMUsageOut(BaseModel):
    """Per-stage LLM token usage + estimated dollar cost for one run, surfaced to the admin UI."""

    model_config = ConfigDict(from_attributes=True)

    stage: str
    model: str
    batched: bool
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_usd: float


class RunOut(BaseModel):
    """Read model for an ingest run, surfaced to the admin UI."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    items_processed: int
    items_failed: int
    last_progress_at: datetime | None
    detail: str | None
    error: str | None
    llm_usage: list[RunLLMUsageOut] = []
    retrieval_health: RetrievalHealthOut | None = None
    """None for every run that did not run candidate retrieval — which is every run of every
    other kind, and any `link` run from before shadow was switched on.

    Assembled by the reader, not extracted from the run: half of it is an aggregate over
    `ingest.link_retrieval_probe`. The ORM side is deliberately named `retrieval_health_row`
    so this field cannot pick that relationship up by name."""
