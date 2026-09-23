"""Candidate generation for one organisation mention on a linked film (EF-12).

The first half of organisation resolution, and `candidates.py`'s shape for the two kinds that
are not people: given a `news.story_entity` row — a studio or franchise name a trade story
wrote, on a story the linker has already attached to a film — produce the small set of TMDB
organisations it could plausibly be. **Nothing here scores, ranks or decides anything**
(`org_scoring.py` does that), so no threshold, no name comparison and no confidence lives in
this module.

Two sources rather than the person side's three, unioned and de-duplicated by TMDB id:

- **`/search/company` or `/search/collection` on the name** — the only source that can reach an
  organisation the catalog has never held, which is the common case for a studio boarding a
  film it is not yet credited on.
- **The film's own current organisations** — its `catalog.film_production_company` rows for a
  company, and the single `catalog.collection` its `film.collection_id` names for a franchise.
  A studio named in a story about a film is very often already on it.

**There is no change-stream source, and that is a difference from the person side, not an
omission.** `candidates.py` reads `film_credit_change` for someone who *just* joined or left,
because a detachment leaves no current credit to find them by. The organisation equivalent —
`catalog.film_company_change`, and `film_field_change` on `collection_id` — is a strictly
narrower set than the film's current rows plus TMDB's name search in every case a name search
can reach, which is every case: a company that left the film last week is still returned by
`/search/company` under its own name, whereas a person who left is returned by
`/search/person` only if TMDB happens to rank them, which for a common name it does not. The
window would buy candidates the search already has, at the price of a query per mention.

**The cap is anchored-first, but the anchored tier cannot take the whole cap.** Being on the
film is the strongest thing the catalog knows about an organisation, so those go first — but a
film carries as many production companies as its financing needed, and ten is not rare. Left
unbounded the anchored tier would fill the cap on exactly those films and evict every search
hit, so a studio the story named that is *not yet* on the film could never be a candidate: it
would score nothing, and with the search non-empty it would route `unlinked` rather than
resolve. The person side does not have this problem because seed grade already narrows which
credited people claim a place (`candidates.py`); an organisation has no grade to cut on, so the
cut is a reservation instead — up to half the cap is held for search-only hits whenever there
are any.

Within the anchored tier, the organisations the *name search also returned* go first. Those are
the ones the reservation must never evict: the search matched them on the name the story wrote
and the film holds them, which is both features at once.

**The popularity prior is counted out of the catalog, not read off the hit.** Neither
`/search/company` nor `/search/collection` reports a popularity signal the way
`/search/person` does, so the prior D-21 allows as a *tiebreak* is how many of this catalog's
films the organisation holds — a number the database already knows and one query answers for
the whole union. It never enters a score; see `org_scoring.rank_org_candidates`.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Collection, Film, FilmProductionCompany, ProductionCompany
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.schemas import TMDBCollectionSearchHit, TMDBCompanySearchHit
from upmovies.news.models import ORGANISATION_KINDS

ORG_CANDIDATE_CAP = 10
"""Most candidates one organisation mention may carry, the person side's cap unchanged: the
resolve stage shows this shortlist to a model in a closed-set prompt (D-22), so it is a
prompt-size bound as much as a scoring one."""

COMPANY = "company"
COLLECTION = "collection"

OrgSearchHit = TMDBCompanySearchHit | TMDBCollectionSearchHit


@dataclass(frozen=True)
class OrgCandidate:
    """One organisation a mention could be, with its provenance and the facts scoring reads."""

    entity_id: int
    kind: str
    name: str
    original_name: str | None = None
    from_search: bool = False
    attached: bool = False
    """Whether this organisation is on the film *now* — a `film_production_company` row, or
    the film's `collection_id`. The organisation arm's only corroborating feature."""
    catalog_reach: int = 0
    """How many catalog films this organisation holds. The popularity prior, and a sort
    tiebreak only (D-21) — it is deliberately not a term of the score."""


@dataclass(frozen=True)
class OrgCandidateSet:
    """What one gather produced: the capped candidate list, and the raw search hits behind it.

    The hits are kept alongside for `CandidateSet`'s two reasons. The `not_in_tmdb` route
    (INV-8) turns on the search having returned *nothing*, which the capped list alone cannot
    say; and accepting an organisation TMDB found but the catalog has never held means writing
    `catalog.production_company` or `catalog.collection` first, which is done from the hit
    rather than from the fields a `OrgCandidate` happens to have kept.
    """

    candidates: list[OrgCandidate]
    search_hits: list[OrgSearchHit]

    def hit_for(self, entity_id: int) -> OrgSearchHit | None:
        return next((hit for hit in self.search_hits if hit.id == entity_id), None)


def build_org_candidates(
    *,
    kind: str,
    search_hits: Sequence[OrgSearchHit],
    attached: Sequence[OrgCandidate],
    catalog_reach: dict[int, int] | None = None,
    cap: int = ORG_CANDIDATE_CAP,
) -> list[OrgCandidate]:
    """Union the two sources, de-duplicate by TMDB id, tag provenance, cap.

    Pure: every read the sources need has already happened by the time this is called, which
    is what lets the union, the flags and the cap be tested without a database or a network.
    `gather_org_candidates` is the half that fetches.

    Order is anchored-first — the organisations on the film that the search also returned,
    then the rest of the film's own, then the search hits in TMDB's own relevance order. Within
    a tier that order decides only who survives the cap; nothing here claims one of a film's
    studios is likelier than another to be the one the story named.

    The anchored tier is bounded so that up to half the cap stays available to search-only
    hits — see the module docstring for the film this exists for. With nothing to reserve for,
    nothing is reserved and the anchored tier takes the whole cap as before.
    """
    reach = catalog_reach or {}
    attached_by_id = {candidate.entity_id: candidate for candidate in attached}
    hit_by_id: dict[int, OrgSearchHit] = {}
    for hit in search_hits:
        hit_by_id.setdefault(hit.id, hit)

    corroborated = [eid for eid in attached_by_id if eid in hit_by_id]
    anchored_only = [eid for eid in attached_by_id if eid not in hit_by_id]
    search_only = [eid for eid in hit_by_id if eid not in attached_by_id]
    reserved = min(len(search_only), cap // 2)
    anchored = [*corroborated, *anchored_only][: max(cap - reserved, 0)]

    ordered_ids = list(dict.fromkeys([*anchored, *search_only]))[:cap]
    out: list[OrgCandidate] = []
    for entity_id in ordered_ids:
        hit = hit_by_id.get(entity_id)
        stored = attached_by_id.get(entity_id)
        # Where a hit and a stored row both carry a name, the hit wins — it was fetched for
        # this mention, while the catalog row was last written whenever some film holding the
        # organisation was last ingested. `candidates._candidate`'s rule, for two fields.
        out.append(
            OrgCandidate(
                entity_id=entity_id,
                kind=kind,
                name=(hit.name if hit is not None else None)
                or (stored.name if stored is not None else "")
                or "",
                original_name=_original_name(hit)
                or (stored.original_name if stored is not None else None),
                from_search=hit is not None,
                attached=stored is not None,
                catalog_reach=reach.get(entity_id, 0),
            )
        )
    return out


async def gather_org_candidates(
    session: AsyncSession,
    client: TMDBClient,
    *,
    kind: str,
    film_id: UUID,
    name_as_written: str,
    cap: int = ORG_CANDIDATE_CAP,
) -> OrgCandidateSet:
    """Read both sources for one organisation mention and build its candidate set.

    One TMDB request per mention and two queries, neither of them per candidate. Which
    endpoint and which loader are read straight off `kind`, which the extraction pass has
    already narrowed to `ORGANISATION_KINDS` — an unknown one is a wiring bug rather than a
    mention to resolve, and raises instead of silently searching the wrong catalogue.
    """
    if kind not in ORGANISATION_KINDS:
        raise ValueError(f"unknown organisation kind {kind!r}")
    if kind == COMPANY:
        search_hits: list[OrgSearchHit] = list(await client.search_company(name_as_written))
        attached = await load_film_companies(session, film_id)
    else:
        search_hits = list(await client.search_collection(name_as_written))
        attached = await load_film_collection(session, film_id)
    entity_ids = {*(hit.id for hit in search_hits), *(c.entity_id for c in attached)}
    return OrgCandidateSet(
        candidates=build_org_candidates(
            kind=kind,
            search_hits=search_hits,
            attached=attached,
            catalog_reach=await load_catalog_reach(session, kind=kind, entity_ids=entity_ids),
            cap=cap,
        ),
        search_hits=search_hits,
    )


async def load_film_companies(session: AsyncSession, film_id: UUID) -> list[OrgCandidate]:
    """The production companies currently on the film.

    `film_production_company` is delete-and-rebuilt on every ingest (`ingest.tmdb.upsert.
    _rebuild_joins`), so this is the film's companies *now* — which is what `attached` means.
    A company that has since left is reached through the name search like any other.
    """
    stmt = (
        select(ProductionCompany.id, ProductionCompany.name)
        .join(
            FilmProductionCompany,
            FilmProductionCompany.company_id == ProductionCompany.id,
        )
        .where(FilmProductionCompany.film_id == film_id)
        .order_by(ProductionCompany.id)
    )
    # By TMDB id, which is deterministic and nothing more: `film_production_company` records no
    # billing order for companies and TMDB publishes none, so there is no "lead studio" to put
    # first. `build_org_candidates` is what keeps this order from mattering on a film with more
    # companies than the cap.
    return [
        OrgCandidate(entity_id=row.id, kind=COMPANY, name=row.name, attached=True)
        for row in await session.execute(stmt)
    ]


async def load_film_collection(session: AsyncSession, film_id: UUID) -> list[OrgCandidate]:
    """The collection the film is filed under, as a list of nought or one.

    A list rather than an optional, so both loaders answer `build_org_candidates` in the same
    shape and the anchored tier does not need to know which kind it is looking at.
    """
    stmt = (
        select(Collection.id, Collection.name)
        .join(Film, Film.collection_id == Collection.id)
        .where(Film.id == film_id)
    )
    return [
        OrgCandidate(entity_id=row.id, kind=COLLECTION, name=row.name, attached=True)
        for row in await session.execute(stmt)
    ]


async def load_catalog_reach(
    session: AsyncSession, *, kind: str, entity_ids: Iterable[int]
) -> dict[int, int]:
    """How many catalog films each of these organisations holds — the popularity prior.

    One grouped count over the whole union rather than a request per candidate, and read from
    the catalog rather than from TMDB because neither organisation search endpoint reports a
    popularity signal at all. An organisation the catalog has never held counts zero, which is
    the right answer for the namesake a name search turned up and nothing corroborates.
    """
    ids = list(dict.fromkeys(entity_ids))
    if not ids:
        return {}
    if kind == COMPANY:
        stmt = (
            select(FilmProductionCompany.company_id, func.count())
            .where(FilmProductionCompany.company_id.in_(ids))
            .group_by(FilmProductionCompany.company_id)
        )
    else:
        stmt = (
            select(Film.collection_id, func.count())
            .where(Film.collection_id.in_(ids))
            .group_by(Film.collection_id)
        )
    return {entity_id: count for entity_id, count in await session.execute(stmt)}


def _original_name(hit: OrgSearchHit | None) -> str | None:
    """A collection hit's original-language name, and nothing for a company hit — TMDB's
    company records carry one spelling only."""
    return hit.original_name if isinstance(hit, TMDBCollectionSearchHit) else None
