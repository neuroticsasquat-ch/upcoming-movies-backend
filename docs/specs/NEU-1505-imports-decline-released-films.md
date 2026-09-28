# NEU-1505 — Imports decline released films, and admission attaches only unreleased ones

**Target repos:** upcoming-movies-backend (this file is the decision record), upcoming-movies-frontend
(`frontend/docs/specs/NEU-1505-imports-decline-released-films.md` is the frontend half). One
ticket, two PRs; see §7 for the deploy order.

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1505 (Bug, High)
**Project / milestone:** bl: Entity Follows, M5 — Imports you review
**Related:** NEU-1436 (the admission exception this gates), NEU-1448 / NEU-1449 (the import
path this narrows), ADR-0014 (baseline rule), ADR-0019 decision 4 (EF-4)
**Ground truth read (2026-09-27):** `ingest/tmdb/upsert.py`, `ingest/tmdb/credit_history.py`,
`ingest/tmdb/company_history.py`, `ingest/tmdb/collection_history.py`, `ingest/tmdb/filters.py`,
`ingest/tmdb/resolution.py`, `ingest/imports/{apply,runner,tmdb_account,review}.py`,
`app/dto.py` (`ImportJobOut`, `ImportUnmatchedOut`, `ImportCandidateOut`), `app/models.py`
(`ImportCandidate`), `app/repos/import_candidate_repo.py`, `catalog/queries.py`
(`alert_window_clause`), `news/models.py` (`Event.status`), `scripts/prune_primary_release_events.py`,
frontend `components/onboarding/{ImportReview,ImportProgress}.tsx`, `api/types.ts`.

---

## 1. What happened, and what changes

Another user's Letterboxd import on the night of 2026-09-26 brought four long-released David
Fincher films into the prod catalog. Each was a first observation, so EF-4's admission exception
wrote every followed person's credits as `added`; the rows cleared quarantine and became
`crew_attached` cards in Tom's timeline and the 2026-09-27 digest. Two defects, both fixed here:

1. **The admission exception has no release gate.** `admission_attachments`,
   `admission_company_attachments` and `record_collection_admission` treat any first observation
   as an attachment, whatever the film's age. ADR-0019 decision 4 was written for "a director's
   *next* film entering the catalog with the director already on it". A film that opened in 2008
   did not just attach its director.
2. **Imports ingest films outside the alert window.** EF-21 upserted an out-of-window film and
   listed it unticked. Those films cost a `/movie/{id}` each, slowed the import, confused the
   importing user (a list dominated by greyed-out released films), and are what fed defect 1.

Imports are the only path that first-observes a released film: the discover ingest skips
`Released` via `classify_skip`, the sweep enumerates undated films only, and the refresh phase
re-reads films already observed. The gate still goes in the admission rule (§3.1), not in the
import, because the rule is what is wrong.

### Decided in the planning session (2026-09-27)

- **D-1505.1 The gate is one predicate, decided once per upsert.** A pure
  `is_unreleased(details, *, today)` in `ingest/tmdb/filters.py`, evaluated once in
  `upsert_film` and handed to the three admission call sites as a bool. The three admission
  functions stay pure and untouched.
- **D-1505.2 "Unreleased" is literal.** Primary `release_date` is NULL or on/after `today`
  (UTC), **and** `status` is not `Released` or `Canceled`. No grace period. The festival case
  (premiered, not yet opened) is accepted: such a film is admitted by the sweep or discover long
  before its premiere, so only an import can meet it at that point, and a premiered film has not
  just attached its director either. Not the 365-day alert window: a film that opened six months
  ago is in the window and still did not "attach" anyone.
- **D-1505.3 An import never lists a film it cannot follow.** Tom's call, from watching the
  other user's import: the review list holds followable films only, its intro says plainly that
  these are the upcoming and recently released films from the watchlist, and one summary line
  gives the counts of what was left out. No disabled rows of any kind — not too-old films, not
  unmatched titles. *Supersedes the EF-21/EF-22 "listed unticked with a reason" rule and
  NEU-1450's review-list rendering of `unmatched` rows.*
- **D-1505.4 `import_candidate.skip_reason` is retired.** Every candidate is followable by
  construction, so the column, its two CHECK constraints, `IMPORT_SKIP_REASONS`,
  `ImportCandidateOut.skip_reason` and the frontend's `skip_reason` branch all go. `selected`
  stays: it is still the tick the list opens with and the M5 contract's name for it, and the
  confirm reads it.
- **D-1505.5 Every window miss is `unmatched` kind `outside_window`.** The kind NEU-1449 marked
  historical and `ImportJobOut` dropped on the way out is revived, for both the pre-fetch date
  check and the post-upsert `in_alert_window` check. The drop (`_MATCHED_KINDS`) is removed: a
  NEU-1448-era row of that kind meant "matched, outside the window", which is exactly what the
  new rows mean, and the frontend now renders it as a count of released films rather than under
  "titles we could not match". No new vocabulary, no new counters: the frontend derives the
  summary counts from `unmatched[].kind`.
- **D-1505.6 The date is checked before any request it can save.** Letterboxd: a row whose
  `Year` is certainly outside the window makes no request at all; a search hit whose
  `release_date` is outside it makes no `/movie/{id}` request and writes no `catalog.film` row.
  TMDB account: the same check on each `TMDBMovieSummary.release_date` before `propose_film`.
  A film already in the catalog is judged by the same date rule first, so an old film admitted
  before this fix is reported `outside_window` rather than offered.
  *Gap closed by NEU-1510: when the runner held no date (an undated TMDB list entry) or the
  fetched details disagreed with it, the upsert still wrote the film before `in_alert_window`
  declined it. A film with no stored row is now judged on its fetched details before anything
  is written.*
- ~~**D-1505.7 Old films already admitted by imports stay in the catalog.** Harmless once the
  admission rule is gated; a purge would also have to reason about follows and events on them.~~
  *Reversed by NEU-1508 (2026-09-28): they have no business in the database. A one-off script
  removes every film that was outside the alert window on the day it was created, by the
  release date it had then, deleting its title follows and events and unlinking its stories;
  see `docs/specs/NEU-1508-purge-imported-released-films.md`.*
- **D-1505.8 The wrongly published cards are deleted, by a dry-run script, after an audit.**
  Supersession does not fit: a superseded card stays on the public feed, the film page and
  title-follow timelines (only the entity arms and the digest exclude it), and needs a removal
  event to point `superseded_by` at, which does not exist. Precedent for deleting cards the
  film page contradicts: `scripts/prune_primary_release_events.py` (NEU-1121). The change rows
  behind them go too, so the sweep's 7-day lookback cannot re-card them.

---

## 2. Acceptance

### Admission (`ingest/tmdb/filters.py`, `upsert.py`)

- `is_unreleased(details, today=…)`: True for `release_date=None, status=None`; True for
  `release_date=today, status="Post Production"`; False for `release_date=today - 1 day`
  whatever the status; False for `status="Released"` with `release_date=None`; False for
  `status="Canceled"` with a future date. Unit tests beside `test_filters.py`'s `classify_skip`
  cases.
- A first observation of a **released** film (primary date yesterday, status `Released`) with a
  followed director, a followed production company and a followed collection in the payload
  writes **no** `film_credit_change`, **no** `film_company_change` and **no** synthetic
  `film_field_change` row; `credits_observed_at` and `companies_observed_at` are still set.
- The same payload with `release_date` a year out and status `Post Production` still writes
  the three `added` rows — every existing test in `tests/integration/ingest/tmdb/test_upsert.py`
  and `tests/integration/ingest/sweep/test_admission_attachments.py` stays green. (Check their
  fixture films are undated or future-dated; a fixture with a past primary date must be moved,
  not the rule.)
- A released film observed once with nobody following, then a follow, then a second ingest
  with identical credits → zero rows (the D-49 property is unchanged by the gate).
- An **already observed** released film whose director genuinely changes between two ingests
  still writes the ordinary diff: the gate touches the `previous is None` branch only.

### Imports — Letterboxd (`ingest/imports/runner.py`, `resolution.py`, `apply.py`)

- A row with `Year=2019` (today 2026-09-27, `PROVIDER_POLL_MAX_AGE_DAYS=365`) makes **no**
  TMDB request (respx asserts zero calls) and is reported `unmatched` with
  `kind="outside_window"`, `name` and `year` the export's.
- A row with `Year=2024` is searched; if the accepted hit's `release_date` is `2025-06-01`
  (before `today - 365 days`), no `/movie/{id}` is requested, no `catalog.film` row exists for
  its id afterwards, and the row is reported `outside_window`.
- A row with `Year=2025` whose hit's `release_date` is inside the window is fetched, upserted
  and listed as a ticked candidate, as today.
- A row with no `Year` is still reported `kind="watchlist"` with no request (unchanged).
- A hit inside the window by date whose stored row says `Canceled` is reported
  `outside_window` and writes **no** candidate (the existing "canceled however recent" test
  changes its assertion from "unticked candidate" to "unmatched, no candidate").
- `test_a_film_outside_the_window_is_still_upserted` is **inverted**: it asserts the film is
  not in the catalog.

### Imports — TMDB account (`ingest/imports/tmdb_account.py`)

- A watchlist entry whose summary `release_date` is before `today - 365 days` makes no
  `/movie/{id}` request, writes no film, and is reported `outside_window` with the summary's
  title and year.
- An entry with `release_date=None` on the summary is fetched and judged post-upsert, as today.

### The window predicate is one rule (`apply.py`)

- A unit test evaluates the new Python-side `release_date_in_window(date | None, *, today,
  max_age_days)` and `alert_window_clause` over the same table of dates (None, today − 366,
  today − 365, today − 364, today, today + 1) and asserts they agree row for row. This is the
  drift guard `in_alert_window`'s docstring asks for.

### Report and contract (`app/dto.py`, `app/models.py`, migration, `review.py`)

- `GET /me/imports/{id}` for an `awaiting_review` job returns `candidates[]` without a
  `skip_reason` key and `unmatched[]` rows carrying `kind="outside_window"` verbatim; a job
  row holding a NEU-1448-era `outside_window` unmatched entry now returns it too.
- `POST /me/imports/{id}/confirm` with every candidate id follows every candidate — there is no
  longer an id it silently drops.
- Migration: deletes `import_candidate` rows where `skip_reason IS NOT NULL` (they would
  otherwise become selectable), then drops `ck_import_candidate_skip_reason`,
  `ck_import_candidate_skipped_unselected` and the column. `test_migrations.py` round-trips.
- `Progress.record_unmatched` accepts `kind="outside_window"`; `UnmatchedKind` and
  `ImportUnmatchedOut.kind` list it as live, `rating` stays historical.

### Prod cleanup (`scripts/prune_admission_cards.py`)

- Dry run prints, per event: film title, `event_type`, `subject_key`, `occurred_at`, and the
  `app.notification` rows for it (`user_id`, `status`, `sent_at`) plus the users whose
  `app.follow` rows name the subject (the timeline reach, which is not logged anywhere).
- `--apply` deletes the events (with `event_story` and `event_summary`, explicitly, as the
  precedent does) and the change rows behind them, and prints the counts.
- Selection: `provenance='catalog'`, `event_type IN (casting, crew_attached, company_attached,
  collection_attached)`, `created_at >= 2026-09-22` (NEU-1436's deploy), the film's primary
  `release_date < DEFAULT_CUTOFF` (the fix's deploy date, a module constant set at deploy), and
  `occurred_at = film.credits_observed_at` for the two credit types,
  `= film.companies_observed_at` for `company_attached`, or `= the synthetic
  film_field_change.changed_at` (`field='collection_id'`, `old_value IS NULL`) for
  `collection_attached`. Postgres `now()` is transaction-start time, so an admission's rows and
  marker share one timestamp exactly.
- Integration test on `app_test`: one admission card on a released film and one on an
  unreleased film; the dry run reports the first only, `--apply` removes it and its change
  row and leaves the second, and `app.notification` rows for it are gone (the FK cascades).

### Tooling

- `task format`, then `task test && task lint && task typecheck` in the api container
  (backend); `pnpm test`, `pnpm lint`, `pnpm typecheck` in the web container (frontend).

---

## 3. Design

### 3.1 The release gate (`filters.py`, `upsert.py`)

`ingest/tmdb/filters.py` — pure ingest-time rules on a `TMDBMovieDetails` — gains:

```python
UNRELEASED_DEAD_STATUSES = frozenset({"Released", "Canceled"})

def is_unreleased(details: TMDBMovieDetails, *, today: date) -> bool:
    """Whether the film has yet to open, for EF-4's admission exception (NEU-1505). ..."""
    if details.status in UNRELEASED_DEAD_STATUSES:
        return False
    return details.release_date is None or details.release_date >= today
```

`upsert_film` evaluates it once — `attachable = is_unreleased(details, today=datetime.now(UTC).date())`
— and passes `attachable: bool` to `record_collection_admission`, `_rebuild_joins` and
`_upsert_credits`. Each `previous is None` (or `film_inserted`) branch becomes
`changes = admission_attachments(...) if attachable else []` (and the collection writer returns
early). The followed-set reads on those branches can also be skipped when `attachable` is
False; do so for companies and collections (they are read only there), leave the credit read
where it is (the diff needs it either way).

Why a bool from the caller rather than a `details` argument on the three functions: the three
call sites already sit in one file, the rule is then evaluated exactly once per upsert, and the
admission functions' existing unit tests (`test_credit_history.py`, `test_company_history.py`)
need no change. Why not `classify_skip`'s excluded set: that set is a setting
(`TMDB_EXCLUDED_STATUSES`) and this rule is not tunable — the ticket's argument, and
`ALERT_WINDOW_DEAD_STATUSES`'s.

Docstrings to reword: `credit_history.py` and `company_history.py` module docstrings (the
"one exception" paragraph gains "while the film is unreleased"), `admission_attachments`,
`admission_company_attachments`, `collection_history.py` module docstring and
`record_collection_admission`, the two step-3 comments in `upsert.py`.

### 3.2 The import declines before it fetches (`apply.py`, `runner.py`, `resolution.py`, `tmdb_account.py`)

**One Python-side window predicate**, in `ingest/imports/apply.py`:

```python
def release_date_in_window(release_date: date | None, *, today: date, max_age_days: int) -> bool:
    """`alert_window_clause`'s date half, for a date that is not yet a row. ..."""
    return release_date is None or release_date >= today - timedelta(days=max_age_days)
```

It cannot see a status, which is fine: `Canceled` is outside the window anyway, and the existing
post-upsert `in_alert_window` (SQL, on the stored row) still runs for every film that passes the
date and catches it. The drift guard is the paired test in §2.

**Letterboxd, before the search** (`runner.py`): a row is certainly outside the window when
`year + FESTIVAL_YEAR_SLACK < (today - max_age_days).year`. `resolve` accepts a hit only when
`|hit.year − year| ≤ FESTIVAL_YEAR_SLACK`, so every acceptable hit's primary date is at latest
31 December of `year + 1`; if that is before the window's start year, no request can find a
film inside it. Today: `year ≤ 2023` skips. Reported `outside_window` with the export's name
and year, zero requests. Rows with no year keep their `watchlist` report, as now.

**Letterboxd, after the search**: `ResolvedTitle` gains `release_date: date | None` (the
accepted hit's). `_import_watchlist_row` checks `release_date_in_window` on it before
`propose_film`; a miss is `outside_window`, no `/movie/{id}`.

**TMDB account**: `import_tmdb_account` checks each `movie.release_date` the same way before
`propose_film`, reporting `outside_window` with the summary's title and year.

**`propose_film`** keeps its post-upsert `in_alert_window` check but no longer writes an
unticked candidate: a miss returns `"outside_window"` and writes nothing, and both runners
record it under `unmatched` (Letterboxd with the export's name and year, TMDB with the
summary's). `WatchlistOutcome`'s docstring and the module docstring's EF-21 paragraph are
rewritten to say the import *declines* out-of-window films rather than holding them. The
"a film somebody listed is worth holding in the catalog" argument in `propose_film`'s
docstring is retired — Tom reversed it.

`CREDITS_FRESH`'s "what this bound does not buy" note still holds and is unchanged.

### 3.3 The contract (`dto.py`, `models.py`, `import_candidate_repo.py`, `review.py`, migration)

- `ImportCandidate`: drop `skip_reason`, both CHECKs and `IMPORT_SKIP_REASONS`; rewrite the
  class docstring's `selected`/`skip_reason` paragraph ("every row is followable; `selected` is
  the tick the list opens with, true on write").
- Migration `…_retire_import_candidate_skip_reason_neu_1505`: `DELETE FROM app.import_candidate
  WHERE skip_reason IS NOT NULL`, drop the two constraints, drop the column. Downgrade re-adds
  the column and constraints (nullable, nothing to restore).
- `import_candidate_repo.add` loses `skip_reason`; `selectable_ids` drops its
  `skip_reason IS NULL` term (keep the function: it still bounds the confirm to the job's own
  rows); the "skipped rows" query at ~line 55 goes with whatever reads it.
- `ImportCandidateOut` loses `skip_reason`. `ImportUnmatchedOut.kind` becomes
  `Literal["watchlist", "rating", "tmdb_missing", "outside_window"]` with `outside_window`
  documented as live ("matched, or certainly matchable, but outside the alert window; not
  fetched"). `_MATCHED_KINDS` and `_failures_only` are deleted. `ImportJobOut.unmatched`'s
  comment: "Titles the import could not place **or declined as too old**".
- `review.py`: any `skip_reason` reference goes; confirm semantics otherwise unchanged.

### 3.4 The cleanup script (`scripts/prune_admission_cards.py`)

Modelled on `prune_primary_release_events.py`: module docstring naming the incident, `--apply`
flag, `DEFAULT_CUTOFF` as the deploy boundary, explicit deletes of `event_story` and
`event_summary`, one `SessionLocal` session. Additions: the selection joins `catalog.film` for
the marker equality and the release-date bound (§2); the report includes the `app.notification`
audit and the follow-based reach, printed **before** any delete because the notification rows
cascade; the change rows are selected by the same `(film_id, changed_at = marker)` equality and
deleted after the events (their `carded_by_event_id` is NULL on sweep-carded rows, so nothing
references them). Run once in prod after the fix deploys, dry first; paste the dry-run output
on the PR.

### 3.5 Frontend half — see `frontend/docs/specs/NEU-1505-imports-decline-released-films.md`

In one paragraph: `ImportCandidate.skip_reason` and the disabled-row branch go; the review list
renders candidates only, with the intro "These are the upcoming and recently released films
from your watchlist. Confirm to follow them." and a summary line built from `unmatched[].kind`
counts ("We left out 14 titles released more than a year ago and 2 we could not match."); the
finished report's `UnmatchedList` shows `outside_window` rows as a count line and lists the
other kinds as today; `ImportUnmatched.kind` gains `"outside_window"`.

### 3.6 Docs in this repo (done in the planning session, working tree)

- ADR-0019: amendment block after the decision list (decision 4 gated on unreleased; EF-21
  narrowed to "declined, not held").
- ADR-0014: one-paragraph amendment after the NEU-1436 block.
- `docs/specs/bl-entity-follows-project-spec.md`: EF-4 and EF-21 lines annotated.
- `CONTEXT.md`: **Alert window** and **Import** entries.
- Still to do in the PR: `AGENTS.md` if it describes the import's window behaviour; the
  NEU-1436, NEU-1448, NEU-1449, NEU-1450 specs are history and stay as written.

---

## 4. Out of scope / deferred

- Purging the old films imports already admitted (D-1505.7).
- A grace period for festival premieres (D-1505.2); revisit only if a real case surfaces.
- Any change to the sweep's or discover's admission, `classify_skip`, or
  `TMDB_EXCLUDED_STATUSES`.
- Retiring `import_candidate.selected` or `watchlist_created`: both still carry the M5
  contract's meaning.
- Listing the declined titles by name on the review list (Tom chose counts only, D-1505.3);
  the finished report still names unmatched and deleted-at-TMDB titles.
- A `withdrawn` event status: the cleanup is a one-off delete with precedent; a third status
  would need every status-blind reader (feed, film page, title-follow arm) to learn it.

---

## 5. Deploy notes

- **Frontend first, or the same release.** A new backend under the old frontend drops
  `skip_reason` from candidates, and the old `selectable` predicate (`skip_reason === null`)
  then disables every row. The new frontend under the old backend merely shows unticked
  candidates as tickable, which the confirm's selectable filter still refuses.
- The migration deletes unticked candidates from any `awaiting_review` job in flight; those
  films were never followable, so the user loses nothing they could have confirmed.
- After both halves are live: run `scripts/prune_admission_cards.py` in the api container dry,
  read the audit, then `--apply`. Verify Tom's timeline and the next digest carry none of the
  four Fincher cards; the `app.notification` rows for them are gone with the events.
- No Coolify changes.
