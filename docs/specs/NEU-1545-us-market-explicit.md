# NEU-1545 — Make the US focus of release dates and the calendar explicit

**Project:** bl: Maintenance · **Repos:** backend + frontend (one spec, here) · **ADR:**
`docs/adr/0024-the-product-is-us-market.md` (written with this spec, 2026-10-09)
**Related:** D-26 (`bl-consumer-pivot-project-spec.md` §5), ADR-0023, NEU-1397 decision 1,
NEU-1542 (`CALENDAR_REGION` named there), NEU-1543 (slate intro), NEU-1384 (iCal feed).

## Why

The product is US-market by decision, and nothing on screen says so. TMDB returns every
country's dates and the ingest stores them all; the cut to US is ours (`release_grade.py`,
D-26). The calendar page, the iCal feed, the settings copy and the digest slate name no
country. The only reader-facing "US" is the calendar's SEO meta description. ADR-0024 records
the decision; this ticket makes the words match the cut.

## Decisions (grilled 2026-10-09)

| # | Question | Decision |
| -- | -- | -- |
| D-1545.1 | Scope of the statement | **The product is US-market**: dates, availability, ratings, currency, formatting, outbound links. Not "dates only", not "the calendar only". |
| D-1545.2 | Where recorded | **ADR-0024** in the backend repo. D-26 gets a cross-reference, not an edit. No frontend ADR: the frontend cites ADR-0024 in comments and carries the glossary term. |
| D-1545.3 | Film page vs calendar asymmetry | **Kept and stated.** Origin-country theatrical rows stay on the film page and in the headline release (NEU-1397 d.1); the calendar, iCal feed and slate stay US-only. ADR-0024 names it the one exception. |
| D-1545.4 | Calendar page | **One subtitle under the `Calendar` heading**, shared by both tabs and both kinds. Kind and tab labels unchanged. |
| D-1545.5 | iCal feed | **Add `X-WR-CALDESC`.** `X-WR-CALNAME` and event `SUMMARY`s unchanged, so nothing moves or duplicates for current subscribers. |
| D-1545.6 | Other surfaces | All four: **settings calendar section**, **digest slate intro** (html + txt), **film page release-list caption**, **global footer**. |
| D-1545.7 | Region constants | **Collapse `CALENDAR_REGION` into `PRIMARY_REGION`.** Pure rename, no behaviour change. |
| D-1545.8 | Glossary term | **Primary region** — added to both `CONTEXT.md` files with this spec. |
| D-1545.9 | Copy | The set in §Copy below, as approved. |

## Copy

Exact strings. Each says "US" once.

| Surface | String |
| -- | -- |
| Calendar subtitle | `US release dates, in theaters and at home.` |
| iCal `X-WR-CALDESC` | `US release dates for the films you follow on backlotter.` |
| Settings calendar section | `Subscribe your calendar to the US release dates of the films you follow — in theaters and at home, each as an all-day event that moves when the date does.` |
| Digest slate intro (html) | `The next {{ slate_window_days }} days of US release dates for films you follow.` |
| Digest slate heading (txt) | `YOUR SLATE — the next {{ slate_window_days }} days of US release dates for films you follow` |
| Film page, under "Release dates" | `US dates, plus theatrical dates in the film's own country.` |
| Global footer | `Release dates and availability are for the US.` |

The calendar's existing meta descriptions already say "US" and are unchanged.

## Backend

### `public/service.py`

- Delete `CALENDAR_REGION`. `_calendar_governing_cte` and any other reader use
  `release_grade.PRIMARY_REGION` (already imported in the module). Grep for the old name
  afterwards: it must survive nowhere in `src/` (the NEU-1542 spec may keep its historical
  mention).

### `public/ical.py`

- New module constant `CALENDAR_DESCRIPTION = "US release dates for the films you follow on
  backlotter."` with a docstring in the style of `CALENDAR_NAME`'s: `X-WR-CALDESC` is not in
  RFC 5545; Apple Calendar and Outlook show it in the calendar's info panel, Google ignores it.
- `render_calendar` emits
  `f"X-WR-CALDESC:{_escape(CALENDAR_DESCRIPTION)}"` directly after `X-WR-CALNAME`. It goes
  through `_escape` and `_fold` like every other TEXT line.

### `mail/templates/digest/body.html` and `body.txt`

- The slate intro / heading strings change as in §Copy. No context change: `slate_window_days`
  is already in the context.

### Docs

- `catalog/release_grade.py` module docstring: where it says "US only (D-26)" for the home
  release, add "and ADR-0024 for the product". Keep it to a clause; the docstring is already
  long.
- `bl-consumer-pivot-project-spec.md` D-26: append an italic amendment in the style of the
  NEU-1542 one: *(Generalised 2026-10-09 by ADR-0024: the product is US-market; the film page's
  origin-country theatrical rows are the one exception.)*
- `CONTEXT.md`: **Primary region** entry (done with this spec).
- `CLAUDE.md` line 17 says "16 ADRs"; it is stale already (23 exist). Make it 24 or drop the number.

### Tests

- `tests/unit/public/test_ical.py`: the rendered header contains
  `X-WR-CALDESC:US release dates for the films you follow on backlotter.` on the line after
  `X-WR-CALNAME`, and the line is folded/escaped per the existing header assertions.
- `tests/integration/routers/test_public_ical.py`: if it asserts the full header, extend it.
- `tests/integration/app/test_digest_sender.py`: no existing test asserts the slate intro
  string, so add one assertion on a slate-carrying render (html and txt) for the new string.
- No new calendar tests: the constant collapse changes no query.

## Frontend

### `routes/calendar.tsx`

- Under the `<h1>Calendar</h1>`, a `<p className="mt-1 text-sm text-muted-foreground">` with
  the subtitle. It is in the route component, not `CalendarView`, so it is server-rendered for
  the anonymous page and sits above the tab list for the entitled one. One line, both tabs,
  both kinds (D-1545.4).

### `pages/Settings.tsx` — `CalendarSection`

- The subscribe sentence gains "US " before "release dates" (§Copy). Nothing else.

### `components/film/ReleaseDates.tsx`

- Under the `<h2>Release dates</h2>`, a `<p className="mt-1 text-sm text-muted-foreground">`
  with the caption. Rendered whenever the section renders (the section already returns null
  with no rows). The per-row country pill stays.

### `components/layout/GlobalFooter.tsx`

- A new sentence after the TMDB attribution paragraph's last sentence, in the same `<p>`:
  `Release dates and availability are for the US.` Same paragraph, so the footer's layout does
  not gain a row.

### Docs

- `CONTEXT.md`: **Primary region** entry (done with this spec).
- Comments on the four edits cite `backend/docs/adr/0024-the-product-is-us-market.md`.

### Tests

- `routes/calendar.test.tsx`: the page renders the subtitle text; one assertion on the
  anonymous render is enough, since the element is outside the tabbed island.
- `pages/Settings.test.tsx`: the calendar section copy includes `US release dates`.
- `components/film/ReleaseDates.test.tsx`: the caption renders with rows and does not render
  with none.
- `components/layout/GlobalFooter.test.tsx`: the footer contains the new sentence.

## Acceptance

1. Anonymous `/calendar`, `/calendar/home`, and the entitled My films views all show
   `US release dates, in theaters and at home.` directly under the heading, server-rendered.
2. A fetched `.ics` feed carries `X-WR-CALDESC:US release dates for the films you follow on
   backlotter.` and is otherwise byte-identical to before (no `SUMMARY`, `UID` or
   `X-WR-CALNAME` change).
3. The settings Calendar section, the film page release list, and the global footer carry the
   §Copy strings.
4. The daily and weekly digest, when they carry the slate, show the new intro in html and txt.
5. `grep -rn CALENDAR_REGION backend/src` returns nothing; the calendar and iCal test suites
   pass unchanged.
6. ADR-0024 exists; D-26 carries its amendment; both `CONTEXT.md` files define **Primary
   region**.

## Out of scope

- Any widening of the region: a country selector, North America, origin-country dates on the
  calendar. Each reopens ADR-0024.
- Changing the headline release's region set (NEU-1397 decision 1 stands).
- Renaming the iCal feed or its event summaries.
- Where-to-watch or provider copy: the box was removed in NEU-1542, and the `now_available`
  cards name the service, not a country.

## Deploy

No ordering constraint. Each repo's change is copy or a rename; the `.ics` header line is
additive and every client tolerates unknown `X-` properties.
