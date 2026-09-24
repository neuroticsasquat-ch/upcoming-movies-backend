# bl: Digest Content — project spec

**Status:** approved design, scaffolded in Linear by `/personal:projectit` (2026-09-24)
**Builds on:** `docs/specs/bl-consumer-pivot-project-spec.md` D-30 to D-34 (the mail gateway,
the notification queue, the digest cadence, the slate) and
`docs/specs/bl-entity-follows-project-spec.md` EF-3, EF-7, EF-10, EF-13 to EF-15 (what a
digest carries and which films the slate lists). Nothing here reopens either.
**Repos:** `upcoming-movies-backend` (primary), `upcoming-movies-frontend`
**Glossary:** `CONTEXT.md` — **Digest** rewritten; **Slate**, **Slate day**, **Lead film**,
**Film entry** added. Use those terms.
**ADR:** none — no decision below is hard to reverse.

This document is the project-wide spec that `/personal:implementit` falls back to for any
ticket in the project (ADR-0004 of the personal plugin). It records the outcome of the
project-shaping interview held with Tom on 2026-09-24 (a `/grilling` pass inside
`/projectit`): every decision, what it changes about the mail that ships today, and the
milestone contracts. Where it is silent, the Entity Follows project spec governs delivery, and
the Consumer Pivot project spec governs the queue.

---

## 1. Purpose

The daily and weekly digests were built as delivery plumbing (Consumer Pivot M7, NEU-1381):
the decision pass queues a `digest` row per (user, event) for everything a follow reaches, and
the sender mails the queue on the user's cadence. Nothing was ever decided about what the mail
*says*. What ships today is a day-grouped dump — a film heading repeated under every day it had
a beat, beat lines with no date, no confidence, no source and no hint of *why* the reader is
seeing the film, a count-based subject, no visual hierarchy, and a slate only the weekly
cadence ever carries.

This project decides and builds the content: one shape for both cadences, ranked by what
matters, attributed to the follow that delivered it, with the slate on one product-wide slate
day for everyone. Around the content it closes three gaps the content work exposes: the
JustWatch credit TMDB's terms require wherever provider names render (both mails miss it
today), the inbox plumbing that makes a bulk sender behave (`List-Unsubscribe`, a preheader),
and a way to *look* at a digest without sending one.

## 2. Scope

**In**

- Both digest cadences' content: structure, ranking, per-entry and per-beat fields, subject,
  preheader, cap, the slate's scope and markers, and the slate day rule for the daily cadence.
- The follow-attribution query the "Following:" line needs.
- The HTML/text templates for `digest`, and the `alert` template restyled to match.
- `Envelope` headers, the one-click unsubscribe endpoint, and the unsubscribe token.
- An admin preview and test-send facility (backend routes + a small admin page).
- The settings page copy for the cadence control.

**Out**

- The cadence model. One cadence per user (`daily | weekly | off`), one slot per cadence, the
  queue the decision pass writes, the entitlement gate, the "nothing to say → no mail" rule:
  all as built (D-31, D-33, D-37 to D-41).
- What a digest *carries*: still every card the timeline carries, `rumored` included (EF-7).
  Ranking, grouping and the cap are presentation; the queue is sent whole.
- The slate's film set (EF-14: title follows only) and window (30 days).
- Per-user time zones. Everything stays UTC, and the mail renders dates only (DC-12).
- Section toggles or per-beat filters in settings (DC-15). Trailer thumbnails (DC-16).
- Push and the alert mail's *content* — the alert is restyled, not redesigned.

## 3. Goals and acceptance (project level)

1. A weekly reader's mail opens with their slate and reads one entry per film, most
   significant first; a film with five beats in the week is one entry with five dated lines.
2. A daily reader gets the same shape for the day's cards and, on the slate day, the slate in
   front — the only day a daily reader sees dates.
3. Every entry a title follow did not put there says which follows did, linked.
4. A `rumored` line says so. A story-backed line links its outlet. A `now_available` line is
   credited to JustWatch. No line ever shows a clock time.
5. The subject names the lead film and its beat; the preheader names the next ones.
6. Gmail shows an Unsubscribe button on the mail, and pressing it turns the digest off.
7. An admin can render any user's next digest in the browser, on either cadence, on any
   date, and mail it to themselves — without marking a row.
8. The settings page describes what each cadence actually sends.

## 4. What already exists (do not rebuild)

- `app/services/digest_sender.py` — recipients per cadence (`COALESCE`d default), the queued
  timeline loader, the slate loader (`title_follow_film_ids`, governing date per
  `(film, US, type)`, `SLATE_WINDOW_DAYS = 30`), the entitlement re-check, the
  per-user session / abort-guard / heartbeat loop, `mark()` outcomes, the detail line. All of
  its *delivery* behaviour stays; its *content* model (`DigestDay → DigestFilm → DigestEvent`)
  is replaced by §5's film entries.
- `app/services/alert_sender.py` — `film_url`, `poster_url` (`w154`), `settings_url`,
  `BEAT_LABELS`, `mark`. Shared by the digest; extend, do not fork.
- `mail/` — `Mailer.send(to, template, context)`, `Envelope(sender, to, subject, text, html)`,
  the Jinja loader with `StrictUndefined`, `validate_templates()` at boot, `NoopTransport`
  (`MAIL_PROVIDER=noop`, the default), the Resend transport with `Idempotency-Key`.
- `app/follow_queries.py` — `title_follow_film_ids`, `follow_reach` (two booleans),
  `_entity_event_pairs(user_id, only)` yielding `(entity_type, entity_id, event_id,
  created_at)` for the five entity branches — the attribution builder is built from it.
- `public/arc.py` — `_EVENT_STAGE` and `most_significant_event_type(event_types)`, the
  significance the feed already ranks by. `public/service.py` — `_directors_for_films`,
  `_production_countries_for_films`, `_release_year`, the parenthetical's inputs.
  `catalog.headline_release` — the governing date the film page leads with.
- `catalog/ref.py` — `film_ref`, `person_ref`, `company_ref`, `collection_ref`; frontend routes
  `/film/:ref`, `/person/:ref`, `/studio/:ref`, `/franchise/:ref`.
- `synthesize/deterministic.py` — catalog bodies never name the film ("Now streaming on
  Netflix.", "US wide release date slipped from … to …"); the LLM prompt forbids restating the
  title. **A beat line is only legible under its film's header** — the reason DC-3 groups by
  film.
- Frontend `lib/format.ts::filmParenthetical` — "(USA/UK, Dir: Joel Coen/Ethan Coen, 2010)";
  countries capped at 3, directors at 2, "+N" for the rest; `arcStageLabel` when all three are
  missing. `components/film/labels.ts` — `EVENT_TYPE_LABELS` mirrors `DIGEST_BEAT_LABELS`,
  `confidenceLabel` → "unconfirmed", `JustWatchAttribution` ("Availability from JustWatch").
- Frontend `pages/Settings.tsx::DigestSection` — three radios, saved on change.

## 5. Decisions (from the interview)

### The two cadences

- **DC-1 The daily is "yesterday on your timeline".** A complete mirror of the cards queued
  since the last daily send, in the shape of §5 *The mail*. No highlights-only cut, no
  significance floor: EF-7 holds.
- **DC-2 One cadence per user, and one slate day for both.** `digest_cadence` stays one value
  per user; nobody receives both mails. A weekly reader gets the slate and the week's cards.
  A daily reader gets the day's cards and, **on the slate day only**, the slate in front —
  today a daily reader never sees upcoming dates at all. The slate day is Thursday,
  product-wide: setting `SLATE_WEEKDAY` (`Literal["monday", …, "sunday"]`, default
  `thursday`), read by the daily slot (`today.weekday()` matches → load the slate) and
  documented as the day the weekly Coolify slot must be scheduled on (AGENTS.md; the repo
  cannot enforce a Coolify schedule). The weekly slot always carries the slate, whatever day it
  runs — the setting governs the daily slot and documents the weekly one.
  *Consequence:* on the slate day a daily reader with nothing queued and a non-empty slate gets
  a mail — the same rule the weekly already has.
- **DC-13 Nothing to say → no mail.** As built. No quiet-week note, no always-send day.

### The mail

- **DC-3 Both cadences group by film.** One **film entry** per film, its beats in the order
  they were published (`created_at`, ADR-0016 — the same axis as today; `occurred_at` breaks
  ties). Entries are ranked by the most significant beat among them
  (`most_significant_event_type` over the entry's types, `_EVENT_STAGE` rank), then title
  (casefolded), then `tmdb_id`. The feed's day grouping is the feed's; the mail does not
  repeat a film per day. The ranking is a pure function in the sender, unit-tested on its own.
- **DC-4 Film header.** Poster (`w154`, rendered at 62px on compact entries) + title, both
  linking to the film page; the feed's parenthetical **spelled exactly as `filmParenthetical`
  spells it** (countries "/"-joined, cap 3; `Dir:` names "/"-joined, cap 2; year; "+N"; the
  arc-stage label when all three are missing) — a backend `film_parenthetical()` helper
  mirrors the frontend rule and is tested against its fixtures; and one **status line**: the
  film's headline release as the film page leads with it ("Wide release · 14 August 2026" /
  "Digital release · 8 October 2026"), or its arc stage when undated ("In production").
- **DC-5 Beat line.** `{date} · {Beat label} · {summary}`, then, on its own line, the source.
  - *Date* is the publication day, UTC, short form "22 Sep"; the year is appended only when it
    is not the current year (a tall digest after `off` can span one).
  - *Unconfirmed marker:* a `rumored` card carries the word **Unconfirmed** after the beat
    label, styled as the feed's amber pill in HTML and as `[unconfirmed]` in text. The loader
    selects `Event.confidence`; nothing else changes — the digest carried these cards already.
  - *Source:* story-backed cards (`provenance = story`) link the first source in the order
    `EventOut.sources` lists them, by outlet name; catalog cards read "via TMDB", unlinked.
    One source per line, never the list.
  - No per-beat deep link: the film header's link is the entry's link. (`#event-<uuid>`
    anchors exist on the film page; not used.)
  - **DC-16** a `trailer` line is a plain line like any other; no thumbnail, no YouTube link.
  - **DC-17** any entry with a `now_available` line renders "Availability from JustWatch"
    once, under its last beat, in both parts. The alert mail gets the same line under a
    `now_available` item. This is TMDB's condition on the provider data, not a style choice.
- **DC-6 "Following:" attribution, one line per entry.** The entity follows that reached any
  beat in the entry, as `Following: Denis Villeneuve, Legendary Pictures` — names linked to
  `/person/:ref`, `/studio/:ref`, `/franchise/:ref`. Omitted when the only reach is a title
  follow: the reader asked for that film by name. A new builder in `follow_queries`:
  `follow_attribution_pairs(user_id) -> Select[(entity_type, entity_id, event_id)]` — the
  union of `_entity_event_pairs(user_id=…)` and a title arm
  `('title', film_id, event_id)` for `Event.film_id IN title_follow_film_ids(user_id)`. The
  sender joins it to the batch's event ids, resolves names through the catalog tables, and
  renders only the entity rows. The line is omitted when the entry has **no** entity pair at
  all; a film reached by both a title follow and a director follow still names the director.
  The title arm exists for the preview and for tests to assert an entry's full reach, not
  for the line.
- **DC-7 Subject.** Three forms, chosen by what the mail holds:
  - entries: `{lead title} — {lead beat label, lower}` + `, + {N} more film{s}` when other
    entries exist (`N` counts entries beyond the lead, **including** those past the cap);
  - entries and slate: the form above + ` · your slate`;
  - slate only: `Your slate: {N} upcoming date{s}`.
  The **lead film** is the top-ranked entry (DC-3). A subject never carries a count of beats.
- **DC-8 Cap: 20 entries.** `DIGEST_MAX_ENTRIES = 20`, a constant. Entries past the cut are
  not rendered; one line closes the section — `and {N} more film{s} on your timeline`,
  linking to `PUBLIC_BASE_URL/` — and every queued row in the batch is still marked `sent`
  (the timeline is where the rest lives; nothing is re-queued). `N` in the subject and in this
  line agree.
- **DC-10 Preheader.** A hidden first element in the HTML body (the usual
  `display:none;max-height:0;overflow:hidden` span), no counterpart in the text part:
  `Your slate: {N} dates in the next 30 days.` when the slate is present, then `Also: {entry 2
  title} — {beat}; {entry 3 title} — {beat}` for up to two entries after the lead. Empty when
  there is nothing to add (the span is omitted, not rendered empty).
- **DC-14 Visual shape.** A one-line product wordmark (`product_name`, text, no image) at the
  top of the card; the lead film renders as a **lead card** — poster at `w185`/92px, header,
  status line, beats — and every other entry as the compact row (62px poster). The slate is a
  plain dated list above the timeline section, as today. The **alert** template is restyled
  in the same ticket to the same wordmark, widths and type so the two mails read as one
  sender; its content is unchanged apart from DC-17.
- **DC-12 Dates, never times.** All UTC. Beat lines use the short date; slate headings keep
  `day_heading`'s full form ("Thursday, October 8, 2026"). No clock time appears anywhere in
  either part.

### The slate

- **DC-9 Scope as built, plus markers.** Title follows only (EF-14), US, the four displayable
  buckets, `SLATE_WINDOW_DAYS = 30`, one governing date per `(film, type)`, soonest first —
  the calendar's and the `.ics` feed's set, unchanged. Each row gains a marker when its date
  was **set or moved since the previous slate day**: the film has a published `release_date`
  event whose subject covers `US:<bucket token>` (D-26's `subject_key` tokens) with
  `created_at` within the last 7 days. **new** when that event recorded no previous date,
  **moved** otherwise (the deterministic body's own distinction: "set to" vs "moved/slipped
  from … to …"; read the persisted change, and fall back to the verb in the summary only if
  the change is not persisted). Markers render as a small pill in HTML and `[new]` / `[moved]`
  in text. A row with no such event carries nothing.

### Inbox plumbing

- **DC-10 (headers) `List-Unsubscribe` and one-click.** `Envelope` gains
  `headers: Mapping[str, str]` (default empty); the Resend transport passes them as the
  API's `headers` object; the noop transport keeps them on the envelope for tests. The digest
  sets `List-Unsubscribe: <{API_BASE_URL}/digest/unsubscribe/{token}>` and
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. The alert mail sets neither (alerts are
  a follow's consequence, not a subscription — the footer's settings link stands).
  - **`API_BASE_URL` is new.** The backend has no setting for its own public origin:
    `PUBLIC_BASE_URL` is the frontend's, and the one API-origin link a user already holds (the
    `.ics` URL) is assembled by the frontend. A mail header has no frontend to assemble it, so
    the setting lands here: `API_BASE_URL` (str, default `http://localhost:8000`, no trailing
    slash), validated in `validate_mail_configuration` beside `PUBLIC_BASE_URL` — a
    transmitting provider with the default value is the same misconfiguration a localhost
    `PUBLIC_BASE_URL` is. Seeded in `docker-compose.prod.yml`; **set in Coolify on deploy**
    (`§7`). It is not routed through the frontend origin because the production proxy shape is
    not the backend's to know.
  - **Token:** `user_settings.unsubscribe_token` (text, unique, NOT NULL, generated like
    `ical_token` via a `new_unsubscribe_token()` in `app/tokens.py`; backfilled by the
    migration for existing rows; created with the settings row). Opaque, never expires, not
    rotatable in v1 — it only ever turns a digest off.
  - **Routes** (public, no session, rate-limited with the existing `rate_limit` dependency,
    own bucket): `POST /digest/unsubscribe/{token}` sets `digest_cadence = off`, answers
    `204`; unknown token `404`; idempotent. `GET /digest/unsubscribe/{token}` — for clients
    that open the link instead of posting — **also** sets `off` and then `302`s to
    `PUBLIC_BASE_URL/me/settings?digest=off`, where the settings page shows a one-line
    "Your digest is off." notice above the cadence control (frontend copy ticket). The mail's
    footer link stays the settings page; the token link is the header's and the footer's
    "unsubscribe" word only.
  - No entitlement check: an unentitled user turning the digest off is still turning it off.

### Preview

- **DC-11 Admin preview and test-send.** Behind `require_current_admin`:
  - `GET /admin/digest/preview?user_id=<uuid>&cadence=daily|weekly&format=html|text&today=YYYY-MM-DD`
    renders the digest the user would receive on that cadence, from their live `queued` rows
    and follows, as `text/html` or `text/plain`. **Marks nothing**, sends nothing, and
    ignores the gate (an admin may preview a lapsed user's mail). `today` defaults to the
    current UTC date and drives the slate window and the slate-day rule. `404` for an unknown
    user; `200` with an empty-state body (`"Nothing to send."`) when the mail would not go
    out, rather than the `MailError` an empty render raises.
  - `POST /admin/digest/test {user_id, cadence, today?}` renders the same mail and sends it
    to the **calling admin's own address** through the configured transport; answers `202
    {message_id}`. Marks nothing. Subject prefixed `[test for <user email>] `.
  - Frontend `/admin/digest` (beside the other admin pages, `RequireAdmin`): user picker
    (email or id, resolved through `GET /admin/users`), cadence radios, date field, an
    `<iframe srcdoc>` preview with an HTML/text toggle, and a "Send to me" button that reports
    the message id. The rendering path is one function (`render_digest(session, user_id,
    cadence, today, settings) -> Envelope | None`) that the sender, the preview and the test
    route all call — the preview cannot drift from the send.

### Settings

- **DC-15 Copy only.** No section toggles, no per-beat filters. New help strings:
  - Daily — "Every morning there is news on your follows, plus your slate on Thursdays."
  - Weekly — "Your slate and the week's news, in one mail every Thursday. The default."
  - Off — "No digest. Alerts for the films you follow still arrive." (unchanged)
  Section intro becomes: "What happened to the films, people, studios and franchises you
  follow, one entry per film — and on Thursdays, the dates coming up for the films among
  them." The weekday is spelled from a frontend constant that mirrors `SLATE_WEEKDAY`'s
  default; the backend does not expose the setting (it is not per-user).

## 6. Milestones and shared contracts

Deploy order: M1 and M2 are independent; M3 reads M1's renderer; M4's copy must not ship
before M1's daily-slate ticket is live (it would promise a Thursday slate the daily slot does
not send).

### M1 — One shape for both mails (backend)

**Goal:** the digest reads by film entry, ranked, attributed, dated and sourced, with the
lead film in the subject and on top, the slate on the slate day for both cadences, and the
JustWatch credit in place. Everything in §5 *The mail* and *The slate*.

**Shared contracts**

- `follow_queries.follow_attribution_pairs(user_id)` (DC-6) — the one builder the sender
  reads; `_entity_event_pairs` stays private.
- Sender model: `DigestEntry(film, header, following, beats, rank_key)`, `DigestBeat(day,
  label, confidence, summary, source | None)`, `SlateRow(day, title, release_label, marker |
  None, film_url, poster_url)`; `DigestBatch` holds `entries` (ranked, uncapped), `slate`,
  `unsendable`; `rendered_entries` / `overflow` derive from `DIGEST_MAX_ENTRIES`.
  `digest_context()` is the only place the template's dict is built; templates never compute.
- `render_digest(session, user_id, cadence, today, settings) -> Envelope | None` — the
  function M3 calls. `None` when there is nothing to say.
- `SLATE_WEEKDAY` setting; AGENTS.md's digest section states the weekly slot runs on it.
- Templates: `digest/{subject.txt,body.txt,body.html}` rewritten; `alert/body.html` and
  `body.txt` restyled and given the JustWatch line. `tests/unit/mail/test_digest_template.py`
  rewritten to the new contract (text/html parity assertions kept in spirit: every title,
  beat, date, source name, marker and URL appears in both parts; text has bare URLs, no `<a`).

### M2 — Inbox plumbing (backend)

**Goal:** the digest carries the headers a bulk sender owes, and the Unsubscribe button works.

**Shared contracts**

- `Envelope.headers`; Resend `headers` passthrough; noop keeps them.
- `API_BASE_URL` setting (DC-10), the origin every API-side link in a mail is built on.
- `user_settings.unsubscribe_token` + migration + `new_unsubscribe_token()`.
- `POST|GET /digest/unsubscribe/{token}` as in DC-10; rate-limit bucket `digest_unsubscribe`.
- `GET /me/settings` does **not** expose the token.

### M3 — Seeing the mail (backend + frontend)

**Goal:** an admin can render any user's digest on any date and mail it to themselves,
without touching the queue.

**Shared contracts**

- `GET /admin/digest/preview` and `POST /admin/digest/test` as in DC-11; response bodies
  above. Both call M1's `render_digest`.
- Frontend `/admin/digest` page, `api/admin.ts` gains `previewDigest` / `sendTestDigest`.

### M4 — Settings copy (frontend)

**Goal:** the settings page says what each cadence sends, and confirms an unsubscribe.

**Shared contracts**

- `CADENCES` help strings and the section intro per DC-15; `?digest=off` notice per DC-10.
- No API change.

### Ticket map (Linear, created 2026-09-24)

Project `bl: Digest Content` (`P-NEU-92`,
https://linear.app/neuroticsasquatch/project/bl-digest-content-d11074830771). Stories carry no
label; tickets carry `loop-ready` + `repo:<name>`.

| Milestone | Story | Ticket | Repo | Blocked by |
|---|---|---|---|---|
| M1 | NEU-1454 one entry per film | NEU-1459 `follow_attribution_pairs` | backend | — |
| M1 | NEU-1454 | NEU-1460 film entries: ranked, attributed, dated, sourced, capped; `render_digest` | backend | NEU-1459 |
| M1 | NEU-1454 | NEU-1461 visual pass + alert restyle + JustWatch | backend | NEU-1460 |
| M1 | NEU-1455 slate on Thursday, new/moved | NEU-1462 `SLATE_WEEKDAY`, daily slate day, slate markers | backend | NEU-1460 |
| M2 | NEU-1456 unsubscribe from the inbox | NEU-1463 `Envelope.headers`, token, `/digest/unsubscribe` | backend | NEU-1460 |
| M3 | NEU-1457 admin preview | NEU-1464 preview + test-send routes | backend | NEU-1462 |
| M3 | NEU-1457 | NEU-1465 `/admin/digest` page | frontend | NEU-1464 |
| M4 | NEU-1458 settings copy | NEU-1466 cadence copy + `?digest=off` notice | frontend | NEU-1462, NEU-1463 |

## 7. Prerequisites and deploy notes

- **Coolify:** confirm the `digest weekly` slot runs on Thursday (the `SLATE_WEEKDAY`
  default). If it runs on another day, either move it or set `SLATE_WEEKDAY` to match — the
  two must agree or daily and weekly readers get their slates on different days.
- **M2 migration** backfills `unsubscribe_token` for existing settings rows; the header is
  emitted only once the column exists, so deploy the migration with the code (one PR).
- **`API_BASE_URL` must be set in Coolify** with M2's deploy — the API's public origin (e.g.
  `https://api.<domain>`), no trailing slash. Unset, the boot-time mail validation refuses a
  transmitting provider, so a missed variable fails loudly rather than mailing a localhost
  unsubscribe link.
- **M4 after M1:** the copy promises a Thursday slate to daily readers.
- Resend must accept the `headers` object (it does; documented API) — the transport test
  asserts the wire shape.

## 8. Open items (not blocking)

- **Alert mail content** was not redesigned; only restyled (DC-14) and credited (DC-17). If
  the alert should also carry the parenthetical and the "Following:" line, that is a
  follow-up ticket reading M1's helpers.
- **Time zones** (DC-12): a per-user zone would need the feed to follow. Revisit if readers
  outside UTC-adjacent zones report off-by-one days.
- **Token rotation** for the unsubscribe token: not in v1.
- **Entity-reached films on the slate** (Q9's third option) was declined to keep the slate,
  the calendar and the `.ics` feed on one set; reopening it is an EF-14 change, not a digest
  change.
