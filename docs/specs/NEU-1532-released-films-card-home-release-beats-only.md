# NEU-1532 — A released film cards home-release beats only: the video poll stops at release, the release-date read carries on

**Target repo:** upcoming-movies-backend only. No migration, no frontend change, no Coolify change.
One one-off prod script (§2, Prune), run by Tom after deploy.

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1532 (bug, no priority)
**Project:** bl: Maintenance (no milestone, no project spec)
**Related:** NEU-1385 (D-35, the video poll this narrows), NEU-1417 / D-46 (named "the late
trailer" as a home-release beat; reversed here), NEU-1121 / NEU-1206 (the release-date history
and carder this extends), D-26 (the home-release date card that, it turns out, never fired after
release), NEU-1505 (`is_unreleased`, and the prune-script precedent), ADR-0014.
**Ground truth read (2026-10-05):** `ingest/videos.py`, `ingest/providers.py`
(`poll_set_clause`, `load_poll_set`, `run_provider_poll`), `ingest/sweep/refresh_phase.py`
(`refresh_set_clause`), `ingest/sweep/release_events.py`, `ingest/tmdb/release_date_history.py`,
`ingest/tmdb/upsert.py::_rebuild_release_dates`, `ingest/tmdb/client.py` (`movie_details`,
`movie_videos`), `ingest/tmdb/filters.py` (`is_unreleased`), `catalog/queries.py`
(`in_play_clause`, `alert_window_clause`, `ALERT_WINDOW_DEAD_STATUSES`),
`catalog/release_grade.py`, `link/cluster.py::is_stale_stage`, `pipeline_run.py::run_providers_stage`,
`scripts/prune_admission_cards.py`, `tests/integration/ingest/{test_videos,test_providers}.py`,
`tests/integration/ingest/sweep/test_release_events.py`, `config.py`
(`TMDB_RELEASE_WINDOW_PAST_DAYS=0`, `TMDB_EXCLUDED_STATUSES=Released,Canceled`,
`SWEEP_EVENT_LOOKBACK_DAYS=7`), `docs/specs/bl-consumer-pivot-project-spec.md` D-26/D-27/D-28/D-35/D-46,
`docs/specs/NEU-1417-alert-window-status-term.md`, `CONTEXT.md` **In play**, **Alert window**,
**Release-date event**.

---

## 1. What is wrong, and what changes

*Spider-Man: Brand New Day* opened on 2026-07-29 (primary date; TMDB status `Released`). On
2026-10-04 the video poll carded a `trailer` event for it. The card is not a bug in the poll: it
is D-35 working as written. The poll runs "on the same scoped set as D-27", whose first rule is
*films 14 to 365 days past their US theatrical date* — so the bulk of what the video poll reads
is released films — and D-46 / NEU-1417 then listed "the late trailer (D-35)" as one of the
home-release beats the alert window exists to deliver. Tom's rule is the opposite: once a film
is out, the only beats left are home media and streaming. This ticket records that reversal.

**Which producers can reach a released film at all.** The sweep's refresh set is
`in_play_clause` — a film leaves it the day its primary date passes or TMDB marks it `Released`
— and discover's past window is 0 days. So after release only two things read TMDB for a film:
the provider poll (`now_available`, wanted) and the video poll (`trailer`, this bug). Credit,
company, collection, status and release-date cards can still land on a released film, but only
from the very observation that *releases* it (the last in-play refresh records the status flip
and whatever else moved in that payload) or from an import refreshing a stale stored row. The
local prod snapshot (90 days to 2026-09-01, before the video poll shipped) shows exactly that
shape: 6 `release_date`, 3 `crew_attached`, 2 `casting` and 1 `production_wrap` on released
films, every one a same-observation or pre-NEU-1505 import card. A direct production read to
count the trailer cards was denied in the planning session; the prune script's dry run is that
count.

**A second finding, folded in at Tom's request.** D-26 promised a `release_date` card when a US
digital or physical date is *set after release*. It cannot fire: the refresh stops reading the
film at release, nothing else fetches `/movie/{id}` for it, and the provider and video polls do
not carry release dates. After release, home-media news today is `now_available` alone. The
fix for the trailer and the fix for the missing home-release date are the same shape — the
providers run is the one pass that visits released films daily — so they ship together.

### Decided in the planning session (2026-10-05)

- **D-1532.1 Scope is the two polls, not a universal gate.** The video poll stops reading
  released films; a new release-date pass starts reading them. No write-time predicate across
  the six catalog producers (the release-day observation's own cards stay as they are), and
  the story path is untouched: `link.cluster.is_stale_stage` keeps letting a trade-reported
  `trailer` / `first_look` through on a released film, because an outlet writing one up is
  editorial judgement that it is news, and the ticket's case was a catalog card.
- **D-1532.2 "Released" is the in-play rule, judged on the primary date.** The one literal
  definition the codebase already has (D-1505.2 `is_unreleased`; `in_play_clause` with
  `TMDB_EXCLUDED_STATUSES`): primary `release_date` NULL or on/after today, **and** status not
  `Released` / `Canceled`. The festival gap is accepted: a film that premiered in September and
  opens in the US in December counts as released from September, so a trailer dropped in between
  does not card — but the refresh phase already stops reading that film at its premiere, so its
  other beats are lost there too, and a second definition of released for one poll is worse.
  *Rejected:* the US theatrical governing date (a second definition); status alone (lags the
  opening by days to weeks, NEU-1417 counted 539 lagging films).
- **D-1532.3 The poll set splits in two by that rule, and the provider poll reads both
  halves.** `poll_set_clause` is unchanged. The **unreleased half** (`poll_set AND in_play`) is
  the video poll's set — in practice title-followed films that have not opened, which is where
  trailers happen. The **released half** (`poll_set AND NOT in_play`) is the new release-date
  pass's set — rule-1 films plus title-followed films that have opened. No card-time gate in
  the video poll: a film that is polled is unreleased today, so its new trailers card, and a
  released film is never polled again. Its `film_video` ledger simply freezes.
- **D-1532.4 The release-date read is a third phase of the providers run.** Order: providers
  (tombstones 404s first, as today) → release dates (released half) → videos (unreleased half).
  Each film: `GET /movie/{id}/release_dates` — a new `TMDBClient` method, no `append_to_response`,
  for the reason `movie_videos` gives — then the **existing** rebuild and diff
  (`_rebuild_release_dates`, refactored to take the `TMDBReleaseDates | None` payload it
  already only reads, with `upsert_film` passing `details.release_dates`). It writes
  `film_release_date_change` rows, sets `release_dates_observed_at`, and rebuilds
  `film_release_date`. It upserts nothing else — no credits, no fields, no status — so no
  other card can come of it. *Rejected:* widening the refresh set to the alert window (the full
  upsert diffs credits and companies too, so cast and crew cards would start landing on released
  films — the opposite of this ticket — and the refresh set grows by hundreds of full-detail
  reads a day); a run kind of its own (one more Coolify slot and deadman check to forget).
- **D-1532.5 Carding stays with the sweep.** The new phase writes history; the sweep's
  `release_events` phase cards it from its 7-day window on the next run, as it does for every
  other change row. One carder, one `_already_carded` rule, one place for D-1532.6. The cost is
  a day's latency (providers runs after the sweep in the same slot), which the digest-only
  delivery (ADR-0021) absorbs: the card is in the next morning's mail either way.
- **D-1532.6 A release date already past when observed is silent.** In the release-date
  carder, a move whose new governing date is **before the day of its observation**
  (`changed_at`, not the carding day — a backlog worked through after an outage must judge each
  change as of when it was seen) writes no card. A group left empty cards nothing and is
  counted; a mixed group cards its surviving moves only, with `subject_key` and body naming
  just those. One rule, no released branch, any bucket. It is **vacuous for an in-play film**:
  every displayable date is on or after the primary date, which is on or after today. On a
  released film it does exactly the sorting Tom wants: a theatrical catch-up ("US wide set to
  30 May" entered in August) and a home date already past (the film is streaming; `now_available`
  carded it) go silent; an upcoming US digital or physical date (the D-26 beat) and the rare
  upcoming theatrical date (a festival premiere's US opening) card. *Rejected:* "home buckets
  only on a released film" — matches the ticket's wording literally, but cards past digital
  dates `now_available` already covered and loses the festival-to-wide beat.
- **D-1532.7 The cards already raised are deleted, by a dry-run script.** D-1505.8's reasoning
  holds unchanged: a superseded card still renders on the feed, the film page and title-follow
  timelines, and needs a `superseded_by` target that does not exist. The `film_video` rows
  behind the cards stay — the ledger is insert-only, and since released films are no longer
  polled nothing can re-card them.

---

## 2. Acceptance

### The video poll (`ingest/videos.py`, `pipeline_run.py`)

- `run_video_poll` selects `poll_set_clause(...) AND in_play_clause(today=today,
  excluded_statuses=...)`, the excluded statuses handed in from `settings.tmdb_excluded_statuses`
  exactly as the refresh phase receives them. `load_poll_set` is untouched; a small selection
  helper beside it (or a parameter on it) is the implementer's call, but the two halves must
  be spelled from the same two clauses so they cannot drift apart or overlap.
- Tests in `tests/integration/ingest/test_videos.py` (respx asserts the call count):
  - a film in the theatrical window with status `Released` (today's `_add_released_film`) is
    **not** polled — zero `/movie/{id}/videos` requests, `selected == 0`;
  - a title-followed released film is **not** polled;
  - a title-followed film with primary date `TODAY` and status `Post Production` **is** polled;
  - a title-followed undated film **is** polled; one with `status="Released"` and a NULL date is
    **not**; one with `status="Canceled"` and a future date is **not**;
  - every existing carding test moves its fixture onto a title-followed **unreleased** film
    (rule-1 films are all released by construction, so the carding fixtures need a `Follow`
    row, as `test_an_in_play_followed_film_is_polled` already has). The behaviour under test —
    baseline, one card per key, tie-breaks, teasers silent — is unchanged and every assertion
    stays as it is.
- `VideosResult.selected` / `videos_detail` keep their shape; the number is now the unreleased
  half's.

### The release-date pass (`ingest/tmdb/client.py`, `ingest/tmdb/upsert.py`, new module, `pipeline_run.py`)

- `TMDBClient.movie_release_dates(tmdb_id) -> TMDBReleaseDates`: `GET /movie/{id}/release_dates`,
  parsed by the existing schema (`extra="ignore"` drops the payload's `id`). Raises
  `TMDBNotFound` on 404 like `movie_videos`. Unit test beside `test_client.py`'s.
- `_rebuild_release_dates(session, film_id, release_dates: TMDBReleaseDates | None)`; `upsert_film`
  passes `details.release_dates`. Every test in `tests/integration/ingest/tmdb/test_upsert.py`
  and `test_release_date_history.py` (if present) stays green unchanged.
- A new module (suggested `ingest/release_dates.py`, beside `videos.py`) with
  `run_release_date_poll(...)` on the same contract as `run_video_poll`: `owned_session` per
  film, `record_progress`, `AbortGuard`, `Heartbeat`, `TMDBNotFound` → `mark_film_missing`
  without touching the guard, `httpx.HTTPError` → failure. Its result dataclass reports
  `selected`, `polled`, `changes` (change rows written), `baselined` (films whose
  `release_dates_observed_at` was NULL — rows written, nothing recorded, ADR-0014), `missing`,
  `failures`, `aborted`, `abort_error`; its detail function contributes the run line's middle
  clause.
- `run_providers_stage` runs providers → release dates → videos, fails the run if any phase
  aborted, and joins three detail clauses. Its docstring's "two phases" becomes three and says
  why the halves differ.
- Tests, new file `tests/integration/ingest/test_release_dates.py` on `test_videos.py`'s pattern:
  - a released in-window film holding a stored `US:3` row, payload adds `US:4` dated inside the
    future → one `film_release_date_change` row (`set`, `US`, `4`), `film_release_date` rebuilt
    with both rows, `polled == 1`, `changes == 1`;
  - a title-followed **unreleased** film is **not** read by this phase (zero requests);
  - a title-followed released film with no theatrical row **is** read;
  - a released film with `release_dates_observed_at` NULL: rows written, **no** change row,
    `baselined == 1`;
  - an unchanged payload on a second pass writes no change row;
  - a 404 tombstones and is not a failure; a sustained outage aborts the phase;
  - end to end: run the phase, then `run_release_date_events` with `now` = the next day → one
    `release_date` event, `subject_key == ["US:digital"]`, body "US digital release date set to
    …", `occurred_at` = the change's `changed_at`.

### Past dates are silent (`ingest/sweep/release_events.py`)

- `_card_group` (or a pure helper `group_moves` feeds — the implementer's seam) drops every move
  with `new_date < changed_at.date()`; a group with nothing left returns without writing and is
  counted on `ReleaseEventResult` (a new counter, e.g. `past`, reported on the detail line beside
  `skipped`); a mixed group's event carries only the surviving moves' subjects and body lines.
- Tests in `tests/integration/ingest/sweep/test_release_events.py` (`NOW` is 2026-08-10, the
  default `new` is 2026-12-04, so the existing suite is unaffected):
  - a `set` with `new = NOW.date() - 1 day` → no event, `past == 1`, `events_created == 0`;
  - a `set` with `new = NOW.date()` (the observation day itself) → cards;
  - a group of `US:3` set to a past date and `US:4` set to a future date, same `changed_at` →
    one event, `subject_key == ["US:digital"]`, body names digital only;
  - a change observed four days ago with a date between then and `NOW` → still cards (judged
    as of its observation day, not the carding day).

### Prune (`scripts/prune_released_trailer_cards.py`)

- Precedent: `scripts/prune_admission_cards.py` and its test. Dry run by default, `--apply`
  deletes. Selection: `provenance = 'catalog'`, `event_type = 'trailer'`,
  `created_at >= VIDEOS_SHIPPED` (2026-09-19, NEU-1385's merge; the first poll baselined, so
  nothing earlier exists), and the film's primary `release_date < created_at::date` — released
  on the day it was carded, the date half of D-1532.2. The status half cannot be reconstructed
  after the fact (nothing records what `status` said on the poll day) and is not attempted; a
  film released by status alone with a NULL or future primary date is left, and that is accepted.
- Dry run prints, per card: film title, `release_date`, `occurred_at` (when the trailer went up),
  `created_at`, `subject_key`, and the `app.notification` rows for it (`user_id`, `status`,
  `sent_at`), plus a total.
- `--apply` deletes `event_story` and `event_summary` rows explicitly (as the precedent does),
  then the events, and prints the counts. `app.notification` rows go by FK cascade.
  `catalog.film_video` is not touched.
- Integration test on `app_test`: a catalog trailer card on a released film, a catalog trailer
  card on an unreleased film, and a **story**-provenance trailer card on the released film; the
  dry run lists the first only, `--apply` removes it with its summary and leaves the other two
  and every `film_video` row.

### Docs

- `ingest/videos.py` module docstring: "The scoped set is `providers.load_poll_set`, unchanged"
  and the paragraph after it are rewritten for the unreleased half (D-1532.3); the
  followed-but-unreleased film is now the *only* subject rather than the typical one.
- `ingest/providers.py` module docstring: the providers run is three passes; the "M8 widened
  that rule" paragraph (if still present) loses its trailer-after-release sentence.
- `catalog/queries.py::ALERT_WINDOW_DEAD_STATUSES` docstring and `alert_window_clause`'s: the
  home-release beats are `now_available` (D-28) and the US home-release dates (D-26); "the late
  trailer (D-35)" comes out.
- `catalog/models.py::Film.videos_observed_at` docstring: "deliberately includes in-play films
  somebody follows" becomes "is in-play films somebody follows".
- `ingest/sweep/release_events.py` module docstring gains D-1532.6 and names the new writer of
  the rows it reads.
- Done in the planning session: `CONTEXT.md` **In play** (gains its second and third callers)
  and **Release-date event** (a past date is not news); ADR-0014 amendment (2026-10-05,
  NEU-1532); `docs/specs/bl-consumer-pivot-project-spec.md` D-35 and D-46 annotations;
  `docs/specs/NEU-1417-alert-window-status-term.md` annotation.

### Tooling

- `task format`, then `task test && task lint && task typecheck` in the api container. The
  full suite is ~2.5 minutes and runs on commit.

---

## 3. Design notes

### 3.1 Why the split is on in-play and not on the alert window

The alert window (D-46) is how long a film stays *interesting* — it bounds the provider poll
and the import. In-play is whether a film has *opened*. Trailers belong to the second question
and home-release dates to its complement, and both halves of the poll set are inside the alert
window already (rule 1 by construction, rule 2 because a title follow ignores the window).
Reusing `in_play_clause` gives the video poll the refresh phase's exact notion of released, so
the two passes that read TMDB for an unreleased film agree about when to stop.

`CONTEXT.md` said in-play "exists for exactly one caller". It now has three — the refresh set,
the video poll, and the released half's complement — and the glossary entry says so. What has
not changed is the warning it carried: everywhere the working set is being *spent* on
retrieval, the word is still **active**, and dormancy is part of what it means.

### 3.2 Call budget

Today every poll-set film costs two reads a day (providers, videos). After this ticket it still
costs two: the unreleased half gets providers + videos, the released half gets providers +
release dates. Roughly neutral, with the released half — the bulk of the set — now buying a
beat that can fire instead of one that cannot.

### 3.3 Day one

The first providers run after deploy re-reads release dates for every released in-window film
for the first time since each opened. Expect a batch of `film_release_date_change` rows and,
on the next sweep, a batch of cards — bounded by D-1532.6 to films whose newly observed dates
are still to come, which is a small, genuinely informative set ("US digital 14 October").
Nothing re-baselines: these films were all observed before release, so the diff is real.

### 3.4 What D-1532.6 changes for in-play films

Nothing, except where TMDB's primary date is inconsistent with its own per-country rows (a
primary date later than some displayable date). Such a past displayable date used to card
("FR limited release date set to <last week>") and now does not. A past date is not news
either way.

---

## 4. Out of scope / deferred

- A universal "no card on a released film" gate across the catalog producers (D-1532.1). The
  release-day observation can still card a status flip's companions; rare, and not this ticket.
- The story path: `trailer` / `first_look` stay out of `_STALE_EVENT_TYPES` (D-1532.1).
- Reading anything but release dates for a released film (D-1532.4). A released film's status
  lag (`Post Production` for weeks after opening) is not corrected here.
- Deleting past-dated `release_date` cards already on the feed, or the release-day companion
  cards the local audit found. Only the trailer cards are pruned (D-1532.7).
- The status half of the prune's selection (§2, Prune).

---

## 5. Deploy notes

1. Merge and deploy. No migration, no Coolify change.
2. `python scripts/prune_released_trailer_cards.py` in the api container: dry run, read the
   list (Spider-Man: Brand New Day should be on it), then `--apply`. Tom runs it.
3. After the first `providers` run, check `/admin/runs`: the run line now has three clauses;
   the release-dates clause should show a one-time spike in `changes`. After the next sweep,
   the release-events clause's `past` counter is where the silent catch-ups land; the cards that
   did publish should all be for dates still to come.
4. Watch the next daily digest for the home-release date lines.
