# NEU-1411 — `GET /me/calendar`: the watchlist release calendar as JSON

**Ticket:** [NEU-1411](https://linear.app/neuroticsasquatch/issue/NEU-1411/get-mecalendar-the-watchlist-release-calendar-as-json)
**Project:** bl: Consumer Pivot · **Milestone contracts honoured:** M7 (D-34 iCal / calendar, D-39 access gate)
**Decisions:** D-13 (follows produce timeline rows only), D-26 (home-release buckets), D-34, D-37, D-39 (`docs/specs/bl-consumer-pivot-project-spec.md` §5)
**Blocks:** NEU-1412 (frontend tabbed calendar — "My watchlist" / "All releases")
**Target repo:** upcoming-movies-backend · **Glossary term:** *Watchlist calendar* (`CONTEXT.md`)

## What to build and why

The personal release calendar exists in one format today: the tokenised iCal text at
`GET /calendar/{token}.ics` (NEU-1383). There is no JSON form, so the site cannot render a
reader their own calendar on a page. This ticket adds `GET /me/calendar`: the public
`GET /calendar` response, narrowed to the caller's **watchlist**.

The frontend (NEU-1412) will render both the all-releases calendar and this one through a single
component and a single set of grouping helpers, so the shape, paging and ordering must be the
public route's exactly. The only thing that differs is *which films* — and that set must be the
`.ics` feed's, because a reader whose subscribed calendar and on-screen calendar disagreed would
be right to file it as a bug.

**It is the watchlist, not the follow graph.** A follow produces timeline rows and nothing else
(D-13). Following a director puts nothing on this calendar. Derived watchlist items *do* count:
the follow graph put them on the watchlist on the user's behalf, and they are exactly the films
the user would otherwise miss.

## The endpoint

```
GET /me/calendar?limit=20&offset=0
```

- **Router:** a new module `routers/me_calendar.py`, prefix `/me/calendar`, tag `me`, built
  exactly like `routers/timeline.py`: `entitled = require_entitled()` applied once at the router
  via `dependencies=[Depends(entitled)]`, and the handler takes `user: User = Depends(entitled)`.
  Registered in `main.py` beside `timeline.router`. Read-only, so no CSRF. No `rate_limit`
  dependency — none of the `/me/*` routes carry one; the public rate limiter guards anonymous
  traffic.
- **Response model:** `CalendarResponse` from `public/dto.py`, unchanged. `CalendarItem` fields
  are populated exactly as `get_calendar` populates them (`film_ref`, `film_title`,
  `release_year`, `poster_path`, `release_date`, `release_type` bucket, `director`, `stars`,
  `genres`).
- **Query params:** `limit` default 20, `ge=1`, `le=200`; `offset` default 0, `ge=0`. Same
  bounds and same meaning as the public route: **limit/offset count distinct release dates,
  soonest first, not film rows**, and `total` is the number of distinct upcoming dates for this
  user. The frontend pages 20 dates at a time on both tabs (`DATES_PER_PAGE`).
- **Status codes:** 200 with the page; 401 for an anonymous caller (from `get_current_user`);
  403 `entitlement_required` for a signed-in caller without a live grant (from
  `require_entitled()`, D-39 — never an empty collection). An empty watchlist, or a watchlist
  with nothing upcoming, is **200 with `items: []`, `total: 0`**, not an error.

## The film set and the date rule

The query is the `.ics` feed's set (`get_ical_feed`) under the public calendar's window:

| Rule | Public `/calendar` | `.ics` feed | **`/me/calendar`** |
|---|---|---|---|
| Films | every film | caller's `watchlist_item` rows, any `source` | **caller's `watchlist_item` rows, any `source`** |
| Region / types | `CALENDAR_REGION` (US), `RELEASE_TYPE_BUCKETS` keys (2,3,4,5) | same | **same** |
| Governing date | `min(release_date)` per `(film, release_type)`, collapsed before the window filter (NEU-1206) | same | **same** |
| Window | `governing_date >= today` (Python-side date, one instant per response) | future + 365 days past | **`governing_date >= today`** (decision below) |
| Slug | required | required | **required** |
| Noise cuts (popularity > 1.5, runtime, adult) | applied | not applied | **not applied** |
| Within-date order | bucket rank (wide, limited, digital, physical), popularity desc nulls last, slug | bucket rank, title | **bucket rank, popularity desc nulls last, slug** — the public route's |

Consequences worth spelling out:

- A watchlist film with a US theatrical date *and* a US digital date produces **two rows** on two
  dates, as on the public calendar and as two VEVENTs in the feed. A film's `limited` and `wide`
  rows are separate subjects too.
- A watchlist film whose only US dates are past is absent, not shown with its past date. The
  glossary is explicit: the calendar, being upcoming-only, excludes a subject whose governing
  date is past rather than falling through.
- A watchlist film with no US displayable date at all contributes nothing. That is not an error.
- Another user's watchlist never leaks: the `watchlist_item.user_id` predicate is inside the
  governing CTE, exactly where `get_ical_feed` puts it.

## Key decisions

### D-1411.1 — Upcoming-only, not the `.ics` feed's past window

The `.ics` feed reaches 365 days back (`ICAL_PAST_WINDOW_DAYS`). That window exists for a
*client* reason: a subscribed calendar client drops every event a feed stops publishing, so
cutting at "today" would erase each release from the user's calendar the day after it happened.
A JSON page has no such client — it is re-rendered from scratch on every visit — and the tabbed
page pages soonest-first, so a past window would open page one on releases from a year ago.

`/me/calendar` therefore uses the public route's window: `governing_date >= today`. The glossary
records the calendar as upcoming-only and the iCal feed as "the same films and dates plus a
bounded reach into the past". "Same dates as the `.ics`" in the ticket's done-when is satisfied
in the sense that matters: for every film and bucket both surfaces show, the date is the same
value from the same governing-date rule.

*Rejected:* the 365-day past window (page one opens a year back; the frontend would have to
offset to today); a short past window such as 30 days (a third window rule no other surface
uses).

### D-1411.2 — No noise cuts; the set is the feed's, not the public listing's

The popularity, runtime and adult filters keep noise off a public listing. A film the user put on
their own watchlist (or the follow graph put there for them) is not noise to them — the same
reasoning `get_ical_feed` and `digest_sender.load_slate` record. Applying the cuts here would
make the on-screen calendar disagree with the subscribed one, which is the bug the ticket
exists to prevent.

### D-1411.3 — One shared page builder, so the two routes cannot drift

`get_calendar` today is one function: governing CTE → visibility predicates → distinct-date
count → date window → row select → decoration (`_directors_for_films`, `_calendar_stars`,
`_calendar_genres`) → `CalendarItem`s. Extract the part from "distinct-date count" onwards into a
private builder that takes the governing CTE and the row-visibility predicates as inputs and
returns the `CalendarResponse`. `get_calendar` passes the public CTE plus the public noise
predicates; a new `get_watchlist_calendar(session, *, user_id, limit, offset)` passes a CTE
joined to `WatchlistItem` for that user plus the slug-only predicate. The `>= today` window,
paging-by-date, ordering and decoration are then spelled once.

`get_ical_feed` stays as it is: it needs a DTSTAMP column and a different window, and its output
type is `CalendarFeedEvent`, not `CalendarItem`. Sharing the governing-CTE construction with it
is welcome if it falls out naturally; forcing it is not required.

**Constraint:** the refactor must not change the public route's output. `test_public_calendar.py`
is the guard; run it before and after.

### D-1411.4 — Router shape follows `routers/timeline.py`, not `routers/public.py`

The gate is applied at the router, once, so a second route added to `/me/calendar/*` later cannot
forget it (the pattern `user_settings.py` and `timeline.py` both record). The route does **not**
live in `public.py` next to `/calendar` and `.ics`: that router is anonymous-and-rate-limited by
construction, and a cookie-authed, entitled route inside it would be the odd one out.

## Acceptance criteria

Integration tests in `tests/integration/routers/test_me_calendar.py`, using the existing
fixtures (`entitled_client`, `authed_client`, `client`, `make_film`, `add_release_date`, and a
watchlist helper as in `test_public_ical.py`):

1. **Anonymous → 401.** `GET /me/calendar` with no cookie.
2. **Signed-in, unentitled → 403** with `detail == "entitlement_required"`, even with a
   populated watchlist. (Mirror `test_timeline_is_403_for_an_unentitled_user`.)
3. **Empty watchlist → 200**, `{"items": [], "total": 0, "limit": 20, "offset": 0}`.
4. **Only the caller's watchlist.** Two entitled users, each with one watchlisted film carrying a
   future US wide date; each user's response contains their own film and not the other's. A
   film with a future date and *no* watchlist row from anyone is absent from both.
5. **Follows do not count.** A film whose director the caller follows (a `Follow` row, no
   watchlist row) is absent.
6. **Derived items count.** A `watchlist_item` with `source="derived_from_follow"` appears.
7. **Same dates as the feed.** For one entitled user with a watchlist film holding US dates in
   types 2, 3, 4 and 5 (all future), `GET /me/calendar` yields four rows whose
   `(film_ref, release_type, release_date)` triples equal the `(URL film ref, bucket, DTSTART)`
   of the four VEVENTs in `GET /calendar/{token}.ics` for the same user — the governing date per
   subject, i.e. the *earliest* row when a subject has several.
8. **Upcoming-only.** A watchlist film whose only US date is past is absent; a film with a past
   `limited` date and a future `wide` date shows the `wide` row alone.
9. **No noise cuts.** A watchlist film with `popularity=0.1` (below the public floor), or
   `runtime=40`, or `adult=True`, still appears. The same film is absent from public `/calendar`.
10. **Slug required.** A watchlist film with `slug=None` is absent.
11. **Paging by date.** Three watchlist films on three distinct future dates; `limit=2` returns
    the two soonest dates' rows with `total == 3`; `offset=2` returns the third. Two films on one
    date are one page unit.
12. **Ordering within a date** matches the public route: `wide` before `limited` before `digital`
    before `physical`; within a bucket, higher popularity first.
13. **Item shape.** A row's `director`, `stars`, `genres`, `release_year`, `poster_path` are
    populated as on the public route (one assertion over a film with credits and genres).
14. **Public route unchanged.** `test_public_calendar.py` passes untouched.

Suite green (`task test` in the api container; the pre-commit hook runs the full suite, ~2 min).

## Out of scope / deferred

- **Frontend** (`routes/calendar.tsx` tabs, the `/me/calendar` fetcher in `api/me.ts`, empty-state
  copy) — NEU-1412.
- **A past window or "recently released" section** on the JSON calendar — not asked for; would be
  a new window rule (D-1411.1).
- **Per-item alert prefs, watchlist source or dismissal affordances on calendar rows** —
  `CalendarItem` is the shared public shape and gains no user-specific fields here. If NEU-1412
  needs "remove from watchlist" on a row it will say so and this DTO can grow then.
- **Changing `get_ical_feed`** — its window, ordering by title and DTSTAMP logic are its own
  (NEU-1383) and stay.
- **A `next`-style cursor or ETag/Cache-Control** — the public route has neither; the response
  is per-user and the frontend fetches it client-side after auth resolves.
- **Rate limiting `/me/*`** — no `/me/*` route has one today; not this ticket's to introduce.
