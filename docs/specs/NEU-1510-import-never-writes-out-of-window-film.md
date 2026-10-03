# NEU-1510 — An import never writes a film its fetched details put outside the alert window

**Target repo:** upcoming-movies-backend only. No migration, no frontend change, no Coolify change.

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1510 (Bug, Low)
**Project:** bl: Maintenance (no milestone, no project spec)
**Related:** NEU-1505 (D-1505.6 promised this and kept it only for dates the runner held before
the fetch), NEU-1508 (purged the rows this leak and its predecessor created; its re-run is the
cleanup here, §5)
**Ground truth read (2026-09-28):** `ingest/imports/apply.py` (`propose_film`, `film_id_for`,
`fetch_details`, `release_date_in_window`, `date_in_window`, `in_alert_window`, module
docstring), `catalog/queries.py` (`alert_window_clause`, `ALERT_WINDOW_DEAD_STATUSES`),
`ingest/tmdb/upsert.py::upsert_film`, `ingest/imports/runner.py` (Letterboxd's hit-date check),
`tests/integration/ingest/imports/` (`test_tmdb_runner.py`, `test_letterboxd_runner.py`,
`test_window_predicates.py`), `docs/specs/NEU-1505-imports-decline-released-films.md` D-1505.6,
`docs/specs/NEU-1508-purge-imported-released-films.md` §3.3, `CONTEXT.md` **Import**.

---

## 1. What is wrong, and what changes

D-1505.6 says a search hit dated outside the alert window "makes no `/movie/{id}` request and
writes no `catalog.film` row". The runners keep that promise only for a date they already hold
**before** the fetch. When they hold none — a TMDB-account list entry with no `release_date`
(`release_date_in_window(None)` is `True`) — or when the list's date and `/movie/{id}`'s primary
date disagree across the window line, `propose_film` calls `film_id_for`, which **upserts the
film**, and only then asks `in_alert_window` of the stored row. A miss writes no candidate and is
reported `outside_window`, but the row stays: `propose_film`'s docstring says so outright. That
is the one path left by which an import puts a film already outside the window into
`catalog.film`, the class of row NEU-1508 just deleted (600 in prod). The row gets a public page,
search presence and, through the provider poll's title-follow rule, `now_available` cards.

`film_id_for` already fetches before it writes (stored-row lookup → early return when credits
are fresh → `fetch_details` → `upsert_film` → commit), so the fix sits between the fetch and the
upsert. It does not need the upsert split from the fetch.

### Decided in the planning session (2026-09-28)

- **D-1510.1 Check the fetched details before the upsert, for a film with no stored row.** In
  `film_id_for`, when `stored is None` and the details fail the window (D-1510.3), return
  `outside_window` without calling `upsert_film` or committing. Nothing is inserted, so nothing
  needs undoing or trusting to a cascade. *Rejected:* upsert, then delete the row if this import
  inserted it. That needs an "inserted" signal out of `upsert_film`, and it briefly admits a
  film that other sessions could observe.
- **D-1510.2 A film already in the catalog keeps today's treatment.** Fresh credits → early
  return, judged by `in_alert_window` on the stored row, no fetch (unchanged). Stale credits →
  fetch, **upsert (refresh)**, commit, then `in_alert_window` declines it (unchanged). The row
  exists on its own terms (discover, the sweep or a pre-gate import that NEU-1508 left in because
  it was in the window then), keeping it current is what every other ingest path does, and an
  import never deletes a film it did not create.
- **D-1510.3 The check is the whole alert window: date half and `Canceled`.** Matches
  `alert_window_clause` / `in_alert_window` exactly. Discover never admits a `Canceled` film
  either (`classify_skip`), so an import admitting a new one is the same leak. Spelled once in
  Python, next to `release_date_in_window` and built on it, with the status half read from
  `catalog.queries.ALERT_WINDOW_DEAD_STATUSES` rather than re-listed, and held to
  `alert_window_clause` by the parity test (§2).
- **D-1510.4 No cleanup code in this ticket.** Re-running NEU-1508's purge with `--before` set
  after this deploy catches every out-of-window-*dated* film this leak created since NEU-1505
  deployed: its rule is "release date at creation outside the window". A film created `Canceled`
  but in-window is not caught. That is vanishingly rare and accepted.

---

## 2. Acceptance

### The predicate (`ingest/imports/apply.py`)

- A Python predicate over `(release_date, status)` — e.g. `details_in_window(details)` wrapping
  `film_in_window(release_date, status, *, today, max_age_days)` — that is
  `release_date_in_window(...)` **and** `status not in ALERT_WINDOW_DEAD_STATUSES` (a `None`
  status is in, as in the clause). Read today and `provider_poll_max_age_days` the way
  `date_in_window` does. The exact names are the implementer's; the rule is not.
- `tests/integration/ingest/imports/test_window_predicates.py` gains a parity case: for every
  date in `DATES` × status in `(None, "Released", "Post Production", "Canceled")`, the new
  predicate agrees with `alert_window_clause` row for row. The existing date-half test stays.

### `film_id_for` / `propose_film`

- `film_id_for` gains a third outcome for "fetched, outside the window, not written". Its one
  caller is `propose_film`, so a return type like `UUID | Literal["tmdb_missing",
  "outside_window"]` (or a small result type) is fine. `propose_film` passes that outcome
  straight through as its own `outside_window`, and the runners report it as they already do.
- The order inside `film_id_for` for a film with no stored row: fetch → `None` →
  `tmdb_missing`; window miss → `outside_window` with **no** `upsert_film` and **no** commit;
  else upsert, commit, return the id.
- The stored-row paths are unchanged (D-1510.2), including the post-upsert `in_alert_window`
  check in `propose_film`, which still covers a stored row (fresh or refreshed) outside the
  window.

### Tests (`tests/integration/ingest/imports/`)

- **TMDB account:** `test_an_undated_list_entry_is_fetched_and_judged_on_the_stored_row` also
  asserts `1003` is **not** in `catalog.film` (it is renamed if "stored row" no longer fits:
  it is judged on the fetched details now).
- **TMDB account, existing stale film:** `1003` already in the catalog (`add_film`, `2019-06-01`,
  `Released`, `credits_observed_at` NULL so it is stale) with an undated list entry → fetched,
  still in the catalog afterwards (the same `id`, i.e. refreshed, not deleted or re-created),
  no candidate, reported `outside_window`.
- **Canceled, both runners:** the existing `test_a_canceled_film_is_skipped_however_recent_its_date`
  cases also assert the film is **not** in `catalog.film`.
- **Letterboxd, dates disagree:** a search hit dated inside the window whose `/movie/{id}`
  details carry a primary date outside it → one `/movie/{id}` request, not in `catalog.film`,
  no candidate, reported `outside_window` by the export's name and year.
- Every existing import test stays green. Any test that relied on the out-of-window row existing
  after the run is wrong by this spec and is fixed, not the rule.

### Docs

- `propose_film` docstring: drop "The film row the upsert wrote stays…"; say a new film is
  judged on its fetched details before anything is written (NEU-1510), and a stored one on its
  row after the refresh.
- `apply.py` module docstring: the sentence on `propose_film` asking `in_alert_window` "once
  more after the upsert" is amended to the two cases above.
- `docs/specs/NEU-1505-imports-decline-released-films.md` D-1505.6 and
  `docs/specs/NEU-1508-purge-imported-released-films.md` §3.3: a one-line annotation that the
  post-fetch gap is closed by NEU-1510 (done in the planning session).
- `CONTEXT.md` **Import**: "never fetched, never enters the catalog" corrected to what is true
  after this ticket (done in the planning session).

---

## 3. Design notes

### 3.1 Why not `upsert_film`'s own admission gate

`upsert_film` is shared by discover, the sweep and the refresh, and it already asks
`is_unreleased` for EF-4's attachment exception. Teaching it to refuse out-of-window films
would change what three other callers admit. The window is an import rule (EF-21), so it
lives in the import's fetch-then-write seam.

### 3.2 What still reaches `catalog.film` through an import

Only films inside the alert window by their fetched details, plus refreshes of rows that
already existed. The review list still holds only films `in_alert_window` accepts.

---

## 4. Out of scope / deferred

- Deleting a stored out-of-window film an import merely refreshed (D-1510.2).
- A purge for `Canceled`-at-creation import films (D-1510.4).
- Any change to discover, the sweep, the refresh or `upsert_film` (§3.1).

---

## 5. Deploy notes

- Merge and deploy. Then, if NEU-1508's `--apply` has not run yet, run it with `--before` set to
  a timestamp **after this deploy**: dry first, then `--apply`. That removes whatever this leak
  created between NEU-1505's deploy and this one. If it already ran, one more dry pass with a
  post-deploy `--before` shows whether anything slipped in between; apply if it lists anything.
