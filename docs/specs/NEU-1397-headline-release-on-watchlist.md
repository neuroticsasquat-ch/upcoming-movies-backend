# NEU-1397 — Headline release on `GET /me/watchlist`

**Ticket:** [NEU-1397](https://linear.app/neuroticsasquatch/issue/NEU-1397) · repo `upcoming-movies-backend` · milestone M3 · parent story NEU-1330
**Frontend half:** [NEU-1398](https://linear.app/neuroticsasquatch/issue/NEU-1398) (blocked by this ticket; backend ships first)
**Glossary:** `CONTEXT.md` → *Headline release*, *Governing release date*, *Primary release date*
**Grilled:** 2026-09-17

## What and why

`WatchlistFilmOut.release_date` copies `catalog.film.release_date`, TMDB's *primary* date: the
earliest release in any country of any type. The film page never lists that value (it survives
only as the year parenthetical), so a watchlist row can cite a date that the page it links to
does not show. NEU-1121 already closed the same trap for release-date events.

Replace the field with the film's **headline release**: a choice among its displayable
governing release dates, falling back to the primary date only when there is nothing
displayable at all, and carrying enough context (kind, country, bucket) for the frontend to
render each case honestly. One aggregate over the watchlist's films, never a query per row.

## Decisions (settled in grilling, 2026-09-17)

| # | Question | Decision |
| -- | -- | -- |
| 1 | Region set | The **displayable set**: `US` plus every origin country (`release_grade.displayable_regions`), types 2 and 3 only. Same rows the film page lists, so the two surfaces agree. Not US-only, despite NEU-1355's wording and the calendar's `CALENDAR_REGION`. |
| 2 | Which upcoming date is "next" | The **earliest upcoming governing date across all displayable subjects**. A limited opening is the film coming out; no wide-preference. |
| 3 | All displayable dates are past | Fall back to the **most recent past** governing date, kind `released`. A watchlist is partly a record of things already out; "No date yet" on a released film is wrong. |
| 4 | No displayable rows at all | Fall back to the **primary date**, kind `primary`, country and bucket null. Mirrors the film page's own unlabelled fallback in `get_film` so the two never disagree. The frontend marks it unconfirmed. |
| 5 | Payload | One nullable nested object `headline_release: { date, kind, country, bucket }`. Null only when the film has no displayable row **and** no primary date. |
| 6 | Name | `headline_release`, glossary term *headline release*. Not `next_release_date`: the value is not always "next". |
| 7 | Where the query lives | New `catalog/headline_release.py`, beside `release_grade.py`. The timeline and iCal are expected to reuse it. |
| 8 | Cross-repo landing | This ticket drops `release_date` outright. NEU-1398 updates the frontend. Between deploys every row reads "No date yet" (the existing null branch), accepted as short-lived. |

Fixed without a question, following the calendar's precedent: `today` is a Python-side UTC date
(`datetime.now(tz=UTC).date()`, never SQL `CURRENT_DATE`); a governing date equal to today is
`upcoming`; same-day ties break **wide before limited, then `US` before origin, then
`FilmReleaseDate.id`** so the result is deterministic.

## Contract

`WatchlistFilmOut` loses `release_date` and gains:

```python
class HeadlineReleaseOut(BaseModel):
    date: date
    kind: Literal["upcoming", "released", "primary"]
    country: str | None      # ISO 3166-1 alpha-2; None iff kind == "primary"
    bucket: str | None       # "limited" | "wide" (release_grade.RELEASE_TYPE_BUCKETS); None iff kind == "primary"

class WatchlistFilmOut(BaseModel):
    id: UUID
    tmdb_id: int
    slug: str | None
    title: str
    poster_path: str | None
    headline_release: HeadlineReleaseOut | None
```

`date` is a calendar date, as the calendar returns, not the film page's timestamp. Convert
`film_release_date.release_date` (timestamptz) the way `get_calendar` does:
`cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)`.

Every `/me/watchlist` verb that returns an item (`GET`, `POST`, `PATCH`) carries the field, so
`_to_out` takes the resolved headline release rather than reading it off `Film`.

## Selection rule, precisely

For one film with origin countries `O`, regions `R = {US} ∪ O`, today `T`:

1. **Subjects.** Group `catalog.film_release_date` rows where `iso_3166_1 ∈ R` and
   `release_type ∈ {2, 3}` by `(iso_3166_1, release_type)`; each subject's governing date is
   its `min(release_date)` cast to a UTC date. (Identical to what `get_film` lists and
   `get_calendar` computes.)
2. **Upcoming.** Among subjects with governing date `≥ T`, take the smallest date. Ties: type 3
   before 2, then `US` before any other region, then lowest row id. → `kind = "upcoming"`.
3. **Released.** Otherwise, among subjects with governing date `< T`, take the largest date,
   same tie-break. → `kind = "released"`.
4. **Primary.** Otherwise, if `film.release_date` is not null → `{date: film.release_date,
   kind: "primary", country: None, bucket: None}`.
5. Otherwise → `None`.

Scenarios the tests must pin (dates relative to `T`):

| Film's rows | Result |
| -- | -- |
| US/2 `T+10`, US/3 `T+24`, FR/2 `T+17` (origin FR) | `T+10`, upcoming, US, limited |
| US/3 `T+24`, DE/3 `T+3` (origin DE) | `T+3`, upcoming, DE, wide |
| US/3 `T+24`, DE/3 `T+3` (origin **US** only) | `T+24`, upcoming, US, wide — DE is not displayable |
| US/2 `T-40`, US/3 `T-26` | `T-26`, released, US, wide |
| US/2 `T-40`, US/3 `T+5` | `T+5`, upcoming, US, wide — a past limited run does not hide the next date |
| US/3 `T` (today) | `T`, upcoming |
| US/3 `T+9` twice, US/2 `T+9` | `T+9`, upcoming, US, wide — tie-break, not an error |
| only DE/3 `T+200`, origin US, `film.release_date = T+200` | `T+200`, primary, null, null (the Cliffhanger case) |
| only US/1 premiere `T+2`, `film.release_date = T+2` | `T+2`, primary — premieres are not displayable |
| no rows, `film.release_date` null | `None` |
| two films on the watchlist, one with rows and one without | one query resolves both; the empty one is `None`, not dropped |

## Implementation notes

- **`catalog/headline_release.py`** (new). A frozen dataclass `HeadlineRelease(date, kind,
  country, bucket)` and
  `async def headline_releases(session, film_ids: Collection[UUID], *, today: date) -> dict[UUID, HeadlineRelease]`.
  Two statements at most: one `DISTINCT ON (film_id)` (or window) over a governing-date CTE
  shaped like `get_calendar`'s, joined to `Film` for `origin_country`, with the
  upcoming-first / released-second ordering expressed as
  `ORDER BY film_id, (governing_date >= :today) DESC, CASE WHEN upcoming THEN governing_date END ASC, CASE WHEN past THEN governing_date END DESC, release_type DESC, (iso_3166_1 = 'US') DESC, min_id ASC`;
  then `film.release_date` for the ids still unresolved. Films absent from both are absent from
  the dict. The displayable-region test must use `displayable_regions` semantics in SQL
  (`iso_3166_1 = 'US' OR iso_3166_1 = ANY(film.origin_country)`), not a Python post-filter, so
  the function stays one round trip per statement regardless of watchlist size. Do not
  duplicate the type set: derive it from `release_grade.THEATRICAL_RELEASE_TYPES`.
- **Wiring.** `watchlist_service.list_items` (and the single-item paths behind `add` /
  `set_alert_prefs`) call it and hand the result to the router; `routers/watchlist.py::_to_out`
  takes `headline: HeadlineRelease | None`. `watchlist_repo` stays pure DB I/O and unchanged.
- **DTO.** `HeadlineReleaseOut` in `app/dto.py`, built from the dataclass. Keep the bucket
  identifiers lowercase (`limited` / `wide`) per `release_grade`; display labels are the
  frontend's.
- **Docstrings.** The module docstring says what the ticket says: why the primary is the last
  resort and not the first, and that this is a *choice among* governing dates. Point
  `release_grade.py`'s "anything reaching for the film's release date" line at it.
- **Tests.** Unit-level scenario table above against a seeded `film_release_date` set
  (`tests/fixtures/public.py` already builds `FilmReleaseDate` rows); integration test in
  `tests/integration/routers/test_watchlist.py` asserting the field on `GET`, `POST` and
  `PATCH`, including one film with no rows returning `headline_release: null` and still
  present. Existing tests that assert `release_date` on watchlist payloads are updated, not
  deleted.
- **Gate.** `task format`, then `task test && task lint && task typecheck`.

## Out of scope

- The film page's own primary-date fallback row (unlabelled `country=""`) stays as it is; this
  ticket mirrors it rather than fixing it.
- Home-release (digital/physical) dates: M6 widens `release_grade` (D-26) and the headline
  release picks them up through the same module then. Nothing here anticipates it.
- Timeline and iCal adoption of the headline release: later tickets, hence the shared module.
- Frontend rendering: NEU-1398.
