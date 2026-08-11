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

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.seed_grade import ROLE_ORDER
from upmovies.news.models import EventSummary
from upmovies.synthesize.store import upsert_summary

# Written to `event_summary.model` in place of a model id. Deliberately not a valid
# `(provider, model)` pricing key: `rates_for` raises on it rather than mispricing it.
DETERMINISTIC_MODEL = "deterministic"

# Written to `event_summary.prompt_version`. Namespaced so it can never be confused with the
# summarizer's own version counter (`SUMMARY_PROMPT_VERSION`, a bare integer). Bump it whenever
# a template below changes wording, so a body can be traced back to the phrasing that produced it.
TEMPLATE_VERSION = "deterministic-2"


@dataclass(frozen=True)
class ReleaseDateChanged:
    """One **displayable** release date set or moved, qualified by the market it belongs to.

    Qualified because unqualified was wrong (NEU-1121). "Release date moved to 15 September"
    was carded off `film.release_date`, TMDB's earliest release in any country of any type,
    while the page listed US-or-origin theatrical dates — so the card named a date the page
    never showed. A body that says *which* market moved cannot make that mistake, and the film
    page lists limited and wide separately, so the label is part of the subject too.
    """

    region: str
    """ISO 3166-1 alpha-2, e.g. `US`."""
    label: str
    """`limited` or `wide` — `public.release.RELEASE_BUCKET_LABELS` renders the display form."""
    new_date: date
    previous_date: date | None = None
    """None when the date is newly *set* for this market; a date when it *moved*."""


@dataclass(frozen=True)
class ReleaseDatesChanged:
    """Every displayable release date one observation changed, rendered as one body (NEU-1121).

    Grouped for the reason `CreditsAttached` is: `uq_event_catalog_change` allows one catalog
    event per film, type and timestamp, and the rebuild detects every change in a single
    observation — so US limited and US wide moving in one distributor announcement have to
    share a card. They usually do move together, and two cards would be two beats where the
    world had one.

    A one-change group renders exactly what a single change renders; one template, not a pair
    to drift apart.
    """

    changes: tuple[ReleaseDateChanged, ...]


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


@dataclass(frozen=True)
class CreditsAttached:
    """Every credit one observation of a film attached, rendered as one body (NEU-1083).

    The credit history is diffed per *observation*, not per person: TMDB routinely gains a
    whole top-billed cast between two ingests, and three cards each reading "X joins the
    cast." is three cards about one beat. `uq_event_catalog_change` says the same thing
    structurally — one catalog event per film, type and timestamp — so a director and a
    writer arriving in one edit have to share a body too.

    A one-credit group renders exactly what the singular change renders; there is one
    template, not a singular and a plural pair to drift apart.
    """

    credits: tuple[CreditAttached, ...]


CatalogChange = (
    ReleaseDateChanged | ReleaseDatesChanged | StatusChanged | CreditAttached | CreditsAttached
)

# Keyed on TMDB's `status` values. An unknown status still gets a body (see `_render_status`) —
# TMDB may add one, and a stage that raised here would leave the event with no summary row,
# which is the one failure mode this module exists to prevent.
_STATUS_BODIES = {
    # Phrased around *shooting* rather than around TMDB's stage names, and deliberately
    # matching the arc vocabulary the film page already renders beside them
    # (`public.arc`: In Production → "shooting", Post Production → "wrapped"). "Entered
    # post-production" also buries the thing a reader cares about: the shoot is over.
    "In Production": "Shooting has begun on the film.",
    "Post Production": "Shooting has wrapped on the film.",
    "Released": "The film has been released.",
    "Canceled": "The film has been canceled.",
    "Planned": "The film is now listed as planned.",
    "Rumored": "The film is now listed as rumored.",
}


def _format_date(value: date) -> str:
    """`14 August 2026` — no zero padding on the day (`%-d` is not portable)."""
    return f"{value.day} {value:%B %Y}"


def _render_release_date(change: ReleaseDateChanged) -> str:
    """One market's clause. `previous_date is None` is a first date for that market, which has
    no "moved from" to render — the same distinction the old unqualified pair encoded as two
    types."""
    market = f"{change.region} {change.label}"
    if change.previous_date is None:
        return f"{market} release date set to {_format_date(change.new_date)}."
    return (
        f"{market} release date moved from {_format_date(change.previous_date)} "
        f"to {_format_date(change.new_date)}."
    )


def _render_release_dates(change: ReleaseDatesChanged) -> str:
    """The group as one body, one sentence per market, in the order the diff produced."""
    return " ".join(_render_release_date(c) for c in change.changes)


def _render_status(change: StatusChanged) -> str:
    return _STATUS_BODIES.get(
        change.new_status, f"The film's production status is now {change.new_status}."
    )


def _join_names(names: list[str]) -> str:
    """`A`, `A and B`, `A, B and C` — the list as a clause reads it."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _render_role(role: str, people: list[CreditAttached]) -> str:
    # Unlike a TMDB status, `role` is set by our own trigger sites from a fixed vocabulary — an
    # unknown one is a bug in the caller, not new data from upstream, so it raises rather than
    # falling back to a body nobody wrote.
    names = _join_names([p.name for p in people])
    match role:
        case "director":
            return f"{names} attached to direct."
        case "writer":
            return f"{names} attached to write."
        case "cast":
            # The character is only legible when one performer is named — `film_credit_change`
            # does not record it, so in practice only a caller that has it supplies it.
            if len(people) == 1 and people[0].character:
                return f"{names} joins the cast as {people[0].character}."
            verb = "joins" if len(people) == 1 else "join"
            return f"{names} {verb} the cast."
    raise ValueError(f"unknown credit role: {role!r}")


def _render_credits(change: CreditsAttached) -> str:
    """One clause per role, strongest attachment first (`ROLE_ORDER`, spec §3.2), so a
    director and a writer attached in the same edit read in a fixed order rather than in
    whichever order the diff happened to emit them."""
    by_role: dict[str, list[CreditAttached]] = {}
    for credit in change.credits:
        by_role.setdefault(credit.role, []).append(credit)
    unknown = [role for role in by_role if role not in ROLE_ORDER]
    if unknown:
        raise ValueError(f"unknown credit role: {unknown[0]!r}")
    return " ".join(_render_role(role, by_role[role]) for role in ROLE_ORDER if role in by_role)


def render_summary(change: CatalogChange) -> str:
    """The user-facing body for one catalog change. Pure — no DB, no clock."""
    match change:
        case ReleaseDateChanged():
            return _render_release_dates(ReleaseDatesChanged(changes=(change,)))
        case ReleaseDatesChanged():
            return _render_release_dates(change)
        case StatusChanged():
            return _render_status(change)
        case CreditAttached():
            return _render_credits(CreditsAttached(credits=(change,)))
        case CreditsAttached():
            return _render_credits(change)


async def write_deterministic_summary(
    session: AsyncSession,
    *,
    event_id: UUID,
    change: CatalogChange,
    source_updated_at: datetime,
) -> str:
    """Write the one `EventSummary` row for a catalog-sourced event and return the body it
    rendered.

    `source_updated_at` is the event's `updated_at`, matching what the summarizer records — so
    when a trade story later clusters on and the real summarizer supersedes this row, the two
    are directly comparable.

    **Supersession is one-directional.** A later catalog change on the same event (the status
    moves again, another credit lands) must not walk an LLM summary — or an admin's wording —
    back to a template, so the write only lands on a row that is still an unedited deterministic
    one. The returned body is what this change *renders*, not necessarily what the row now
    holds. Caller owns the commit."""
    body = render_summary(change)
    await upsert_summary(
        session,
        event_id=event_id,
        summary=body,
        model=DETERMINISTIC_MODEL,
        prompt_version=TEMPLATE_VERSION,
        source_updated_at=source_updated_at,
        replace_when=and_(
            EventSummary.model == DETERMINISTIC_MODEL, EventSummary.edited_at.is_(None)
        ),
    )
    return body
