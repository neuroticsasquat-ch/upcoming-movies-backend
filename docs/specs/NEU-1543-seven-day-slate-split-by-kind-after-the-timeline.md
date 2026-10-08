# NEU-1543 — The slate is seven days, split by calendar kind, and follows the timeline

**Ticket:** [NEU-1543](https://linear.app/neuroticsasquatch/issue/NEU-1543) (title only: "Reduce
weekly slate in digest to next 7 days; split into separate theatrical/home slates; move AFTER
news updates"; no priority, no comments, no relations)
**Project:** bl: Maintenance (no milestone, no project spec, no shared contracts)
**Target repo:** upcoming-movies-backend only. The frontend has no slate copy that names the
window or the order (`pages/Settings.tsx` says "your slate on Thursdays" and "the dates coming
up for the films among them", both still true). **Base branch:** `main`. No migration, no
script, no Coolify change, no deploy-order constraint.
**Related:** D-33 (the weekly send; its "is the slate mail" reading is retired here), DC-2
(slate day; holds), DC-7 / FB-22 (subject; amended), DC-9 (scope + markers; window amended),
DC-10 (preheader; amended), FB-26 (slate = my-films calendar reproduced; layout amended),
D-1542.2 (calendar kinds), D-1542.3 (the home kind omits the bucket heading), **D-1542.4**
(rejected a split slate for a 30-day list — superseded by this ticket for a 7-day one),
ADR-0021 (the digest is the only delivery; untouched).
**Ground truth read (2026-10-08):** `app/services/digest_sender.py` (`SLATE_WINDOW_DAYS`,
`SLATE_MARKER_DAYS`, `SlateItem`, `SlateBucket`, `SlateDay`, `SlateMonth`, `group_slate`,
`slate_months`, `_bucket_rank`, `load_slate`, `load_slate_markers`, `DigestBatch.slate` /
`slate_count` / `has_content`, `digest_subject`, `digest_preheader`, `_slate_context`,
`digest_context`, `day_heading`, `carries_slate`), `mail/templates/digest/{body.html,body.txt}`,
`public/service.py` (`_calendar_page`, `_calendar_governing_cte(release_types=)`,
`CALENDAR_KIND_TYPES`, `_CALENDAR_BUCKET_ORDER`, `_my_films_visible`), `public/dto.py`
(`CalendarItem.release_type` is the bucket string), `catalog/release_grade.py`
(`THEATRICAL_RELEASE_TYPES`, `HOME_RELEASE_TYPES`, `RELEASE_TYPE_BUCKETS`), `CONTEXT.md`
**Slate**, **Slate day**, **Digest**, **Calendar kind**; `docs/specs/NEU-1542-*.md`;
`docs/specs/bl-digest-content-project-spec.md` (DC-2, DC-7, DC-9, DC-10);
`docs/specs/bl-feed-by-followed-entity-project-spec.md` (FB-22, FB-26); tests under
`tests/unit/app/test_digest_ranking.py`, `tests/unit/mail/test_digest_template.py`,
`tests/integration/app/test_digest_sender.py`, `tests/integration/routers/test_digest_admin.py`.

---

## 1. What is wrong, and what changes

The slate is the my-films calendar reproduced in the mail: today it covers **30 dates**, lists
theatrical and digital rows **under one date heading** (Wide / Limited / Digital buckets), and
sits **in front of** "New on your timeline" in the weekly and in the slate-day daily, because
D-33 read the weekly send as "the slate mail". Three things are wrong with that now.

- **Thirty days is too long.** On a weekly cadence the same date is listed four or five weeks
  running; the slate is mostly repetition, and the news below it is what changed.
- **The calendar split (NEU-1542) left the slate unsplit.** D-1542.4 kept one slate because
  "two sections for a 30-day list that rarely holds more than a handful of home dates" was
  not worth it. The reader of the mail now has a calendar page with an In theaters / At home
  control and a slate that mixes them. With a 7-day window the slate is short enough that the
  split is the natural layout, and the reasoning that rejected it no longer applies.
- **The news is the point of the mail.** The timeline rows are what happened since the last
  send; the slate is a standing reminder. The reminder goes after the news.

What this ticket is **not**: it does not change the slate's **set** (title follows only, US,
the three displayable buckets, one governing date per (film, type), the calendar page's own
rows and order — FB-26 / DC-9 scope), the **marker** rule or its 7-day lookback (DC-9), the
**slate day** or which cadence carries the slate (DC-2, `carries_slate`), the "nothing queued
and an empty slate gets no mail" rule, the access gate, the `.ics` feed, the calendar page, the
admin preview / test-send routes, or any frontend file.

### Facts that shaped the decisions

- `load_slate` is one `_calendar_page` call over the my-films governing CTE with
  `governing_date < today + SLATE_WINDOW_DAYS` and `limit=SLATE_WINDOW_DAYS` dates. The page
  pages by **distinct date**, so a limit equal to the window's day count returns the whole
  window whatever it holds. `release_types=None` keeps both kinds (physical is already gone,
  D-1542.5).
- `CalendarItem.release_type` is the **bucket string** (`"wide" | "limited" | "digital"`), and
  `load_slate_markers` is keyed by `(film_id, bucket)`. A kind is therefore a set of buckets:
  `theatrical` = the buckets of `THEATRICAL_RELEASE_TYPES`, `home` = the bucket of
  `HOME_RELEASE_TYPES`, both via `RELEASE_TYPE_BUCKETS`. Partitioning one unsplit page by
  bucket yields exactly the rows two kind-filtered pages would: the governing date is per
  (film, type), so narrowing the CTE to one kind removes rows without changing any.
- `day_heading` is "Friday, October 10, 2026": it already names the month and the year. The
  `SlateMonth` level ("a month heading only across a boundary") exists for a 30-day window
  where the reader loses track of the month; a 7-day window never needs it.
- `SLATE_MARKER_DAYS = 7` — the run's day and the six before — is already "since the previous
  slate day" for a Thursday slate. A 7-day window tiles the same way: Thu..Wed, then the next
  Thu..Wed, no gap and no overlap.
- The subject's "· your slate" suffix and the slate-only "Your slate: N upcoming dates" come
  from `digest_subject`; the preheader's "Your slate: N dates in the next 30 days." from
  `digest_preheader`. Both are rendered nowhere else. The `slate_window_days` context key feeds
  the body's "The next N days" line only.
- The HTML template keys the "New on your timeline" h2's top margin on `{% if slate %}`
  (24px when the slate precedes it); the text part separates the two sections with blank lines.

### Decided in the planning session (2026-10-08)

- **D-1543.1 The window is seven dates starting today.** `SLATE_WINDOW_DAYS = 7`: today and
  the six after it, the same convention as the 30-day window (the day the count runs out is
  the first one left off). A date that is today is still a date to know about. With a
  Thursday slate day consecutive slates tile exactly. *Rejected:* tomorrow..today+7 (a film
  opening on the slate day itself goes unlisted); today..today+7 (next Thursday's dates are
  listed in two consecutive slates).
- **D-1543.2 One "Your slate" section, two kind sub-sections.** The h2 "Your slate" and its
  "The next 7 days of films you follow." line stay. Under them, in this order, **In theaters**
  then **At home** — the calendar's kind labels (`CalendarKind` `theatrical` / `home`,
  D-1542.3's control). Each kind holds date headings (`day_heading`, soonest first), and under
  each date the calendar's film row as today (poster `w92`, title (year), marker pill,
  `Dir. …`, stars, genres). **In theaters keeps the Wide / Limited bucket sub-heading; At home
  has no bucket sub-heading** — one bucket, and "Digital" under every date is noise, exactly
  as the calendar page's home view omits it (D-1542.3). Within a theatrical date the bucket
  order is the calendar's (`_bucket_rank`: wide, limited). **An empty kind is omitted** — no
  "Nothing in theaters" placeholder; both kinds empty is an empty slate, as now. **The month
  level goes**: `SlateMonth` and `slate_months` are removed; the day heading names the month.
  *Rejected:* two sibling h2s ("In theaters this week" / "At home this week") — the slate
  stops being a named thing and the settings copy still calls it one; kind groups nested under
  each date — not what the calendar does, and a reader scanning for home dates walks every
  date.
- **D-1543.3 The slate follows the timeline.** In both parts the order is: greeting, "New on
  your timeline" (the week, or the days), then "Your slate", then the footer. A slate-only mail
  (nothing queued, non-empty slate) is greeting, slate, footer. The h2 margin that today keys
  on `slate` keys on `days or week` and moves to the slate's h2. D-33's "the weekly send *is*
  the slate mail" reading is retired: the slate is a section after the news, in both cadences,
  on the slate day (DC-2 unchanged). `DIGEST_TEMPLATE`'s docstring and the module docstring's
  "the weekly send is the 'your slate' mail" paragraph are rewritten to say so.
- **D-1543.4 The slate leaves the subject and the preheader.** `digest_subject` drops the
  "· your slate" suffix: a mail with timeline lines is headlined by its lead film and
  "+ N more films", whether or not a slate follows. `digest_preheader` drops the "Your slate:
  N dates …" part: for a mail with lines it is "Also: …" (up to two films after the lead) or
  empty, as the no-slate case already is. *Rejected:* keeping both and changing only the
  number; reordering the preheader to match the body — the slate is below the fold now, and
  the inbox line should sell what is above it.
- **D-1543.5 A slate-only mail's subject counts films, not dates.** `digest_subject` for a
  batch with no lines and a non-empty slate is **"N films you follow are out this week"**,
  with `N` the number of **distinct films** across both kinds (a film with a theatrical and a
  home date in the window is one film) and the singular form "1 film you follow is out this
  week". Its preheader names **up to three films, soonest first**, each as
  `Title (Ddd)` with the three-letter weekday of its earliest date in the window, joined by
  ", ": `Dune: Part Three (Fri), Zodiac (Tue)`. Walk order for both the count and the
  preheader: date ascending, then theatrical before home, then the bucket's own order, first
  occurrence of a film wins. The word "slate" no longer appears in the inbox. *Rejected:*
  keeping "Your slate: N upcoming dates" for this one case; naming the films in the subject.
- **D-1543.6 Implementation shape: partition, do not re-query.** `load_slate` keeps its one
  `_calendar_page` call (window `< today + 7`, `limit=7`) and partitions the rows by kind
  through a bucket→kind map **derived** from `CALENDAR_KIND_TYPES` and `RELEASE_TYPE_BUCKETS`
  (never literal strings), so a new displayable type lands in a kind or fails loudly, and the
  slate cannot hold a bucket the calendar does not. Markers are loaded once for the page, as
  now. *Rejected:* two kind-filtered `_calendar_page` calls — same rows, twice the queries, and
  the marker load would run per kind or need merging.

---

## 2. Vocabulary (apply to `CONTEXT.md` in the implementation PR — see §4)

- **Slate** — amend: "over the next **7 days**", "split by **calendar kind** — In theaters,
  then At home — each kind the my-films calendar reproduced for that kind: date heading, the
  theatrical kind's release-type bucket, the calendar's film row", and "it **follows** the
  timeline in the mail". The marker sentence holds. *Avoid* (add): "the slate mail" (the slate
  is a section after the news, not the mail's reason), "this week's releases" (it is the
  user's followed films only).
- **Digest** — amend "On the slate day either cadence carries the slate in front" to "… carries
  the slate **after** the timeline".
- **Slate day** — unchanged.

---

## 3. Build

### 3.1 `app/services/digest_sender.py`

- `SLATE_WINDOW_DAYS = 7`; rewrite its docstring ("today and the 6 after it"; the tiling
  with `SLATE_MARKER_DAYS` and a Thursday slate day is worth one sentence).
- **Model.** Remove `SlateMonth` and `slate_months`. Add
  `SlateKind` (frozen dataclass): `kind: CalendarKind`, `days: tuple[SlateDay, ...]`, with a
  `label` property — "In theaters" / "At home" — and `count`. `SlateDay`, `SlateBucket`,
  `SlateItem` stay. `group_slate(items) -> tuple[SlateKind, ...]`: partition by kind first
  (theatrical, home; an empty kind is not emitted), then the existing date → bucket nesting
  within each. `DigestBatch.slate: tuple[SlateKind, ...]`; `slate_count` sums through the
  kinds; `has_content` unchanged. Add `slate_films(batch) -> list[(film_ref, title, day)]`
  (or equivalent) in the walk order of D-1543.5, distinct by `film_ref` — the one source for
  the slate-only subject count and preheader.
- **Kind map.** `_SLATE_KIND_BUCKETS: dict[CalendarKind, frozenset[str]]` built from
  `CALENDAR_KIND_TYPES` and `RELEASE_TYPE_BUCKETS` at import; `_bucket_kind(bucket) ->
  CalendarKind` raises `KeyError`-style loudly for an unmapped bucket (the calendar never
  serves one; `_calendar_page` reads `RELEASE_TYPE_BUCKETS`).
- `load_slate`: unchanged apart from returning `group_slate`'s new shape.
- `digest_subject`: no slate suffix; slate-only branch per D-1543.5 (`_plural` handles
  "film"/"films"; the verb agrees: "is" / "are").
- `digest_preheader`: lines present → "Also: …" or ""; slate-only → the D-1543.5 film list.
  The "nothing to add → empty, element omitted" rule stands.
- `_slate_context(kinds, settings)` → `[{ "kind": "theatrical"|"home", "label": …,
  "days": [{ "heading": …, "buckets": [{ "label": "Wide"|"Limited"|None, "films": […] }] }] }]`.
  The home kind's bucket `label` is `None` so the templates render the h5 / indented bucket
  line only when a label is set — the templates compute nothing. Film dict unchanged.
- `digest_context`: `slate_window_days` stays (now 7).
- Docstrings: module header paragraph "**The weekly send is the 'your slate' mail (D-33)…**",
  `DIGEST_TEMPLATE`, `load_slate`, `SLATE_POSTER_SIZE` as needed — say the slate is a section
  after the timeline, split by kind, over 7 days.

### 3.2 Templates `mail/templates/digest/body.html`, `body.txt`

- Move the slate block below the timeline block in both parts. HTML: the timeline h2's top
  margin is 0; the slate h2's is 24px when `days or week`, else 0. Text: "NEW ON YOUR TIMELINE"
  first, "YOUR SLATE — the next {{ slate_window_days }} days of films you follow" after it,
  separated as the two sections are today.
- Replace the `{% for month in slate %}` / month-heading loop with `{% for kind in slate %}`:
  an h3 `{{ kind.label }}` (HTML, the former month heading's style) / `{{ kind.label | upper }}`
  (text); then the existing day loop; the bucket h5 / indented label only `{% if bucket.label %}`.
- Everything inside the film row is unchanged (poster, title, marker pills, director, stars,
  genres; the text line's `[new]` / `[moved]`).

### 3.3 Tests (TDD; the suite runs on commit, ~2.5 min)

- `tests/unit/app/test_digest_ranking.py`: subject — lines + slate has **no** suffix; slate-only
  is "N films you follow are out this week" / "1 film … is out this week", counting distinct
  films across kinds; preheader — slate-only lists up to three films soonest first as
  `Title (Ddd)`, lines + slate is "Also: …" only, one film + slate is empty; `group_slate`
  partitions theatrical before home, omits an empty kind, keeps date → bucket order inside a
  kind; the month tests (`test_a_slate_inside_one_month_has_no_month_heading`,
  `…across_a_month_boundary…`, `test_an_empty_slate_has_no_months`) are replaced by kind tests.
- `tests/unit/mail/test_digest_template.py`: fixtures `SLATE` / `MARKED_SLATE` take the kind
  shape; `test_the_slate_comes_before_the_timeline` becomes `…comes_after…`; a test that the
  theatrical kind shows Wide / Limited and the home kind shows no bucket label, in both parts;
  "7 days" in both parts; the month-boundary template test goes.
- `tests/integration/app/test_digest_sender.py`: the window tests (`…inside_the_window`,
  `…over_its_window`) already use `SLATE_WINDOW_DAYS` and keep passing; add one asserting a
  film with a theatrical and a home date in the window appears under both kinds and counts
  once in the subject; the slate-only weekly test asserts the new subject.
- `tests/integration/routers/test_digest_admin.py`: preview on the slate day — assert the slate
  renders after the timeline.

### 3.4 Docs

- `CONTEXT.md` per §2.
- `docs/specs/bl-digest-content-project-spec.md`: amendment notes on DC-7 (no slate suffix),
  DC-9 (`SLATE_WINDOW_DAYS = 7`), DC-10 (preheader without the slate), in NEU-1542's style
  (*Amended 2026-10-08 by NEU-1543: …*).
- `docs/specs/bl-feed-by-followed-entity-project-spec.md`: amendment note on FB-22 and FB-26
  (7 days; split by kind; follows the timeline; no month heading).
- `docs/specs/NEU-1542-calendar-split-and-initial-release-only.md` D-1542.4: a one-line
  amendment that the slate is split by NEU-1543 (the `.ics` half stands).
- `AGENTS.md` lines 39–40 ("with the slate on `SLATE_WEEKDAY`", "with the 'your slate'
  section") stay true; no edit.

---

## 4. Acceptance criteria

1. A weekly digest rendered for a user with timeline lines and dated follows reads, top to
   bottom: greeting, "New on your timeline", "Your slate" with "The next 7 days of films you
   follow.", footer — in both the HTML and the text part.
2. The slate lists only dates `today <= d < today + 7`; a film dated `today + 7` is absent, one
   dated `today` is present.
3. Under "Your slate": "In theaters" then "At home". Each kind lists its dates soonest first.
   In theaters shows "Wide" / "Limited" under a date; At home shows the film rows directly
   under the date with no bucket label. A kind with nothing in the window is absent; a user
   with only home dates sees "At home" alone.
4. There is no month heading anywhere in the slate.
5. The New / Moved marker pill (HTML) and `[new]` / `[moved]` (text) render as before.
6. Subject with lines: `<lead headline>[, + N more films]` — never "· your slate". Subject with
   no lines: "N films you follow are out this week" / "1 film you follow is out this week",
   N = distinct films across both kinds.
7. Preheader with lines: "Also: …" or empty (element omitted). Preheader with no lines:
   up to three films soonest first as `Title (Ddd)`, joined by ", ".
8. A user with nothing queued and no dates in the next 7 days gets no mail; one with nothing
   queued and one date gets the slate-only mail. The slate-day daily behaves the same way.
9. `/admin/digest` preview and test-send render the new layout without change to the routes.
10. `pyright`, `ruff`, and the suite pass; the frontend is untouched.

## 5. Out of scope / deferred

- Any change to the `.ics` feed, the calendar page or its kinds, the marker rule, the slate
  day, or which cadence carries the slate.
- A "nothing this week" placeholder for an empty kind or an empty slate.
- Frontend copy: `Settings.tsx` describes the slate without a window or an order and stays.
- Real-client (Gmail / Apple Mail) eyeball of the moved section — owed as it was for NEU-1530.
