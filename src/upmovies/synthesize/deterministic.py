"""Deterministic summaries for catalog-sourced events (ADR-0014, spec §5.4).

`EventOut.summary` is a required `str` and every read path inner-joins `EventSummary`, so an
event with no summary row is invisible on the feed, the film page and the sitemap alike. A
catalog-sourced event — one born from a TMDB field or credit change rather than from a trade
story — has nothing to summarize with a model: there are no stories, so the LLM would be
inventing prose from a field diff. It gets a templated body instead.

Two contracts this module exists to hold:

- **`model` is a sentinel, never a real model id.** No call is made, and `ingest.llm_call` /
  `ingest.run_llm_usage` are the system's cost ledger — a row naming a real model there would
  price tokens that were never spent.
- **The wording lives in one place.** Three trigger sites (release date, status, credits) write
  these bodies; §5.4's phrasing must not be copy-pasted across them, and `prompt_version` must
  move when the phrasing does.

Callers own the transaction, in line with the rest of the ingest pipelines.
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.synthesize.store import upsert_summary

# Written to `event_summary.model` in place of a model id. Deliberately not a valid
# `(provider, model)` pricing key: `rates_for` raises on it rather than mispricing it.
DETERMINISTIC_MODEL = "deterministic"

# Written to `event_summary.prompt_version`. Namespaced so it can never be confused with the
# summarizer's own version counter (`SUMMARY_PROMPT_VERSION`, a bare integer). Bump it whenever
# a template below changes wording, so a body can be traced back to the phrasing that produced it.
TEMPLATE_VERSION = "deterministic-1"


@dataclass(frozen=True)
class ReleaseDateSet:
    """A film that had no release date now has one — the majority case after the undated-film
    expansion, and the one ADR-0002's story-trigger rule was never written for."""

    new_date: date


@dataclass(frozen=True)
class ReleaseDateMoved:
    """A film's release date moved from one date to another."""

    previous_date: date
    new_date: date


@dataclass(frozen=True)
class StatusChanged:
    """A TMDB production-status transition (Planned → In Production, …)."""

    new_status: str


@dataclass(frozen=True)
class CreditAttached:
    """A director, writer or cast member newly credited on the film. `character` is only
    meaningful for `cast` and is omitted from the body when absent."""

    role: str  # "director" | "writer" | "cast"
    name: str
    character: str | None = None


CatalogChange = ReleaseDateSet | ReleaseDateMoved | StatusChanged | CreditAttached

# Keyed on TMDB's `status` values. An unknown status still gets a body (see `_render_status`) —
# TMDB may add one, and a stage that raised here would leave the event with no summary row,
# which is the one failure mode this module exists to prevent.
_STATUS_BODIES = {
    "In Production": "The film has entered production.",
    "Post Production": "The film has entered post-production.",
    "Released": "The film has been released.",
    "Canceled": "The film has been canceled.",
    "Planned": "The film is now listed as planned.",
    "Rumored": "The film is now listed as rumored.",
}


def _format_date(value: date) -> str:
    """`14 August 2026` — no zero padding on the day (`%-d` is not portable)."""
    return f"{value.day} {value:%B %Y}"


def _render_status(change: StatusChanged) -> str:
    return _STATUS_BODIES.get(
        change.new_status, f"The film's production status is now {change.new_status}."
    )


def _render_credit(change: CreditAttached) -> str:
    match change.role:
        case "director":
            return f"{change.name} attached to direct."
        case "writer":
            return f"{change.name} attached to write."
        case "cast":
            if change.character:
                return f"{change.name} joins the cast as {change.character}."
            return f"{change.name} joins the cast."
    raise ValueError(f"unknown credit role: {change.role!r}")


def render_summary(change: CatalogChange) -> str:
    """The user-facing body for one catalog change. Pure — no DB, no clock."""
    match change:
        case ReleaseDateSet(new_date=new_date):
            return f"Release date set to {_format_date(new_date)}."
        case ReleaseDateMoved(previous_date=previous, new_date=new_date):
            return f"Release date moved from {_format_date(previous)} to {_format_date(new_date)}."
        case StatusChanged():
            return _render_status(change)
        case CreditAttached():
            return _render_credit(change)


async def write_deterministic_summary(
    session: AsyncSession,
    *,
    event_id: UUID,
    change: CatalogChange,
    source_updated_at: datetime,
) -> str:
    """Write the one `EventSummary` row for a catalog-sourced event and return its body.

    `source_updated_at` is the event's `updated_at`, matching what the summarizer records — so
    when a trade story later clusters on and the real summarizer supersedes this row, the two
    are directly comparable. Caller owns the commit."""
    body = render_summary(change)
    await upsert_summary(
        session,
        event_id=event_id,
        summary=body,
        model=DETERMINISTIC_MODEL,
        prompt_version=TEMPLATE_VERSION,
        source_updated_at=source_updated_at,
    )
    return body
