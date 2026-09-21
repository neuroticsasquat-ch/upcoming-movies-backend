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
- **The wording lives in one place.** Six trigger sites (release date, status, credits,
  production companies, the watch-provider poll and the video poll) write these bodies;
  §5.4's phrasing must not be copy-pasted across them, and `prompt_version` must move when
  the phrasing does.

Callers own the transaction, in line with the rest of the ingest pipelines.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import MONETIZATION_TYPES
from upmovies.catalog.seed_grade import ROLE_ORDER
from upmovies.news.models import EventSummary
from upmovies.synthesize.store import upsert_summary

# Written to `event_summary.model` in place of a model id. Deliberately not a valid
# `(provider, model)` pricing key: `rates_for` raises on it rather than mispricing it.
DETERMINISTIC_MODEL = "deterministic"

# Written to `event_summary.prompt_version`. Namespaced so it can never be confused with the
# summarizer's own version counter (`SUMMARY_PROMPT_VERSION`, a bare integer). Bump it whenever
# a template below changes wording, so a body can be traced back to the phrasing that produced it.
TEMPLATE_VERSION = "deterministic-8"


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
    """`limited`, `wide`, `digital` or `physical` — `public.release.RELEASE_BUCKET_LABELS`
    renders the display form. One template covers all four: "US digital release date set to
    14 October 2026" needs no phrasing of its own, and a second one would be the drift this
    module exists to prevent."""
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
    """A director, writer, cast member or other crew member newly credited on the film.
    `character` is only meaningful for `cast` and is omitted from the body when absent.

    `crew` is the role a followed person's non-seed crew credit carries (D-49) — every job
    that is neither directing nor writing, folded into one clause because the attachment does
    not carry the job."""

    role: str  # "director" | "writer" | "cast" | "crew"
    name: str
    character: str | None = None
    credit_order: int | None = None
    """TMDB's billing position for a `cast` credit, read from live `catalog.film_credit`.
    None for crew, which is ranked by `ROLE_ORDER` rather than by a number, and for a caller
    that has no billing data to give."""


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


@dataclass(frozen=True)
class CreditDetached:
    """A director, writer, cast member or other crew member no longer credited on the film."""

    role: str  # "director" | "writer" | "cast" | "crew"
    name: str


@dataclass(frozen=True)
class CreditsDetached:
    """Every credit one observation detached, rendered as one body (NEU-1200).

    Mirrors `CreditsAttached`: one removal card per observation, all roles in one body
    since `credit_removed` is a single type and `uq_event_catalog_change` allows one
    catalog event per film, type and timestamp.
    """

    credits: tuple[CreditDetached, ...]


@dataclass(frozen=True)
class CompanyAttached:
    """A production company newly listed on the film (EF-5).

    The display name, not the id: the id is the card's identity and rides in
    `Event.subject_key` (`news.subject_key.company_subject_token`), while the body needs the
    string a reader recognises.
    """

    name: str


@dataclass(frozen=True)
class CompaniesAttached:
    """Every company one sweep pass saw attach to a film, as one body (D-7).

    Grouped for the reason `CreditsAttached` is: `uq_event_catalog_change` allows one catalog
    event per film, type and timestamp, and a film entering production routinely gains its
    studio, its financier and its production arm in a single TMDB edit. Three cards each
    reading "X joins the production." would be three cards about one beat.
    """

    companies: tuple[CompanyAttached, ...]


@dataclass(frozen=True)
class CompanyDetached:
    """A production company no longer listed on the film (EF-5)."""

    name: str


@dataclass(frozen=True)
class CompaniesDetached:
    """Every company one sweep pass saw leave a film, as one body. `CompaniesAttached`'s
    mirror, on the same grouping rule."""

    companies: tuple[CompanyDetached, ...]


@dataclass(frozen=True)
class AvailableOn:
    """One monetization type a film was newly observed under, and the services carrying it.

    `providers` are display names in the order the poll observed them — TMDB's own ordering
    within the monetization list, which is the order the where-to-watch box renders too.
    """

    monetization_type: str  # "flatrate" | "rent" | "buy"
    providers: tuple[str, ...]


@dataclass(frozen=True)
class NowAvailable:
    """Every monetization type one observation of a film first saw, as one body (D-28).

    Grouped for the reason `ReleaseDatesChanged` and `CreditsAttached` are:
    `uq_event_catalog_change` allows one catalog event per film, type and timestamp, and a
    title reaching the home market routinely turns up to rent *and* to buy in the same poll.
    The card carries a `US:rent`-style `subject_key` token per type, so the per-type grain
    D-28 cards on survives in the one place a consumer of the card needs it.

    Only the group is a `CatalogChange`: unlike a release-date move, no caller ever holds a
    single type on its own — the poll always hands over whatever one observation newly saw.
    """

    offers: tuple[AvailableOn, ...]


@dataclass(frozen=True)
class TrailerReleased:
    """A film's first sighting of a new YouTube trailer (D-35).

    Carries nothing, and that is the decision rather than an omission. The two things a reader
    might expect in the body are both wrong here: TMDB's video `name` is editor-entered free
    text ("Official Trailer", "TRAILER #2 (HD) 4K", the occasional stray caption), so rendering
    it would put unreviewed strings on the feed; and the YouTube key belongs on the event, not
    in prose — it rides in `Event.subject_key` and surfaces as `EventOut.video_key`, which is
    what the film page embeds a player from (NEU-1386).

    So the body says the one thing the poll actually knows, and the card's value is the video
    beside it. A marker rather than a bare string constant because `render_summary` dispatches
    on the change type, and a change with no data is still a change.
    """


CatalogChange = (
    ReleaseDateChanged
    | ReleaseDatesChanged
    | StatusChanged
    | CreditAttached
    | CreditsAttached
    | CreditDetached
    | CreditsDetached
    | CompanyAttached
    | CompaniesAttached
    | CompanyDetached
    | CompaniesDetached
    | NowAvailable
    | TrailerReleased
)

# Keyed on TMDB's `status` values. An unknown status still gets a body (see `_render_status`) —
# TMDB may add one, and a stage that raised here would leave the event with no summary row,
# which is the one failure mode this module exists to prevent.
_STATUS_BODIES = {
    # Phrased around *shooting* rather than around TMDB's stage names, and deliberately
    # matching the arc vocabulary the film page already renders beside them
    # (`public.arc`: In Production → "shooting", Post Production → "wrapped"). "Entered
    # post-production" also buries the thing a reader cares about: the shoot is over.
    # No "on the film" — the card sits under the film's own title, so naming it again is the
    # redundancy the summarizer prompt already tells the model to avoid.
    "In Production": "Shooting has started.",
    "Post Production": "Shooting has wrapped.",
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
    types.

    A move whose new date is *strictly later* is a **slip** and swaps the verb (D-32,
    NEU-1403): "slipped from … to …" against the direction-neutral "moved from … to …" for an
    earlier date. Decided here, per clause, and nowhere else — the alert mail renders this
    body verbatim, so a second derivation of direction in the sender would be free to drift
    from the card. An equal pair should never arrive (the sweep only sets `previous_date` on a
    `moved` row) and renders as "moved" rather than raising: the renderer stays total."""
    market = f"{change.region} {change.label}"
    if change.previous_date is None:
        return f"{market} release date set to {_format_date(change.new_date)}."
    verb = "slipped" if change.new_date > change.previous_date else "moved"
    return (
        f"{market} release date {verb} from {_format_date(change.previous_date)} "
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


# Sorts after every real billing position, so a credit with no `credit_order` never
# displaces one that has it. Wider than TMDB's cast lists will ever be.
_UNBILLED = 1 << 30


class BilledCredit(Protocol):
    """What `credit_order_key` needs of a credit. A protocol so the sweep can order its own
    `AttachedCredit` rows by the same key without first converting them to `CreditAttached` —
    the ordering has to be settled before the group knows which credits it will name."""

    @property
    def role(self) -> str: ...

    @property
    def credit_order(self) -> int | None: ...


def credit_order_key(credit: BilledCredit) -> tuple[int, int]:
    """Canonical order for the credits of one group: strongest role first, then billing
    order within the role (D-7).

    Shared with the sweep's `group_attachments`, so the names a card *stores* in
    `subject_key` sit in the same order the body *reads* them — one definition of "billing
    order", not two that drift.

    Crew credits carry no `credit_order` and so all share the sentinel: a stable sort leaves
    them in the order the caller supplied, which is the order the credit history produced.
    `ROLE_ORDER` is the only ranking between a director and a writer, and inventing a second
    one here would fight it.
    """
    role = ROLE_ORDER.index(credit.role) if credit.role in ROLE_ORDER else len(ROLE_ORDER)
    return (role, _UNBILLED if credit.credit_order is None else credit.credit_order)


def _render_role(role: str, people: list[CreditAttached]) -> str:
    # Unlike a TMDB status, `role` is set by our own trigger sites from a fixed vocabulary — an
    # unknown one is a bug in the caller, not new data from upstream, so it raises rather than
    # falling back to a body nobody wrote.
    # Billing order is the cast's own ranking, and a burst card can name six of them (D-7),
    # so the body has to read top-billed first rather than in whichever order the history
    # diff emitted. Crew all share the sentinel, so this is a no-op for them.
    people = sorted(people, key=credit_order_key)
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
        case "crew":
            # A followed person's non-seed crew credit (D-49). The job it was for is
            # deliberately not named: `catalog.film_credit_change` records `job`, but the
            # attachment this card is built from carries only its role, and a body that
            # sometimes said "as cinematographer" and sometimes did not would be two templates.
            verb = "joins" if len(people) == 1 else "join"
            return f"{names} {verb} the crew."
    raise ValueError(f"unknown credit role: {role!r}")


def _render_credits(change: CreditsAttached) -> str:
    """One clause per role, strongest attachment first (`ROLE_ORDER`, spec §3.2), so a
    director and a writer attached in the same edit read in a fixed order rather than in
    whichever order the diff happened to emit them.

    Within the `cast` clause the people read in billing order (`credit_order_key`, D-7): a
    burst card collapses everyone who cleared quarantine in one pass, and the order they are
    named in is the only ranking the body carries."""
    by_role: dict[str, list[CreditAttached]] = {}
    for credit in change.credits:
        by_role.setdefault(credit.role, []).append(credit)
    unknown = [role for role in by_role if role not in ROLE_ORDER]
    if unknown:
        raise ValueError(f"unknown credit role: {unknown[0]!r}")
    return " ".join(_render_role(role, by_role[role]) for role in ROLE_ORDER if role in by_role)


def _render_detached_role(role: str, people: list[CreditDetached]) -> str:
    names = _join_names([p.name for p in people])
    verb = "is" if len(people) == 1 else "are"
    match role:
        case "director":
            return f"{names} {verb} no longer attached to direct."
        case "writer":
            return f"{names} {verb} no longer attached to write."
        case "cast":
            departs = "departs" if len(people) == 1 else "depart"
            return f"{names} {departs} the cast."
        case "crew":
            departs = "departs" if len(people) == 1 else "depart"
            return f"{names} {departs} the crew."
    raise ValueError(f"unknown credit role: {role!r}")


def _render_detachments(change: CreditsDetached) -> str:
    by_role: dict[str, list[CreditDetached]] = {}
    for credit in change.credits:
        by_role.setdefault(credit.role, []).append(credit)
    unknown = [role for role in by_role if role not in ROLE_ORDER]
    if unknown:
        raise ValueError(f"unknown credit role: {unknown[0]!r}")
    return " ".join(
        _render_detached_role(role, by_role[role]) for role in ROLE_ORDER if role in by_role
    )


def _render_companies(change: CompaniesAttached) -> str:
    """ "Legendary Pictures joins the production." — one clause, however many companies.

    **The film is not named**, which is where this departs from the illustrative phrasing in
    the EF-5 spec line ("Legendary Pictures joins *Dune: Part Three*"). Every other body in
    this module leaves the title out for a reason that applies here unchanged and is recorded
    on `_STATUS_BODIES`: the card renders under the film's own title on the feed, the film page
    and in both mails, so naming it again is the redundancy the summarizer prompt already tells
    the model to avoid. The spec line reads as a description of the beat rather than as the
    literal template, and following it literally would make the studio bodies the only ones on
    the feed that repeat their heading.

    "the production" rather than "the film": it is what the beat is called, and it keeps the
    clause parallel with the crew body's "join the crew."
    """
    names = _join_names([c.name for c in change.companies])
    verb = "joins" if len(change.companies) == 1 else "join"
    return f"{names} {verb} the production."


def _render_company_detachments(change: CompaniesDetached) -> str:
    """ "Legendary Pictures is no longer attached." — the mirror, phrased off the detached
    credit bodies ("X is no longer attached to direct") so one vocabulary covers both halves.

    No "to produce": the credit bodies can name the job because the credit carries one, and a
    company row does not — TMDB publishes no role for a production company, so a body that
    claimed one would be inventing it."""
    names = _join_names([c.name for c in change.companies])
    verb = "is" if len(change.companies) == 1 else "are"
    return f"{names} {verb} no longer attached."


# Keyed on the monetization types the poll stores (`catalog.models.MONETIZATION_TYPES`), which
# a CHECK constraint holds the ledger to — so unlike a TMDB status an unrecognised key here is a
# bug in the caller, not new data from upstream, and `_render_now_available` raises on it.
# "Now streaming" rather than "Now available to stream": it is what the beat is called, and the
# card sits under the film's own title so the title is not named again.
_AVAILABILITY_BODIES = {
    "flatrate": "Now streaming on {providers}.",
    "rent": "Available to rent on {providers}.",
    "buy": "Available to buy on {providers}.",
}


def _render_now_available(change: NowAvailable) -> str:
    """One clause per monetization type, in the order the where-to-watch box lists them (D-29)
    rather than in whichever order the poll's payload emitted — a card that first saw a film to
    rent and to stream reads the same way whatever TMDB put first.

    Services are named with `_join_names`, the same clause-joiner the credit bodies use: this
    module exists to keep one phrasing, and a second way of writing a list of names is the
    drift it is here to prevent."""
    unknown = [
        o.monetization_type
        for o in change.offers
        if o.monetization_type not in _AVAILABILITY_BODIES
    ]
    if unknown:
        raise ValueError(f"unknown monetization type: {unknown[0]!r}")
    by_type = {o.monetization_type: o for o in change.offers}
    return " ".join(
        _AVAILABILITY_BODIES[kind].format(providers=_join_names(list(by_type[kind].providers)))
        for kind in MONETIZATION_TYPES
        if kind in by_type
    )


# The trailer card's whole body (D-35). "A new trailer" rather than "the trailer": a film
# trailers more than once, the poll cannot tell the first from the third, and `alert_sender`
# already labels the beat "New trailer" — one phrasing, in the two places it renders.
_TRAILER_BODY = "A new trailer is out."


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
        case CreditDetached():
            return _render_detachments(CreditsDetached(credits=(change,)))
        case CreditsDetached():
            return _render_detachments(change)
        case CompanyAttached():
            return _render_companies(CompaniesAttached(companies=(change,)))
        case CompaniesAttached():
            return _render_companies(change)
        case CompanyDetached():
            return _render_company_detachments(CompaniesDetached(companies=(change,)))
        case CompaniesDetached():
            return _render_company_detachments(change)
        case NowAvailable():
            return _render_now_available(change)
        case TrailerReleased():
            return _TRAILER_BODY


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
