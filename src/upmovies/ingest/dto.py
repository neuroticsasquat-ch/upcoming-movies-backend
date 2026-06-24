from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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
