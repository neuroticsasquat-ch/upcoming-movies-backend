# NEU-1538 — A pre-order is not availability: `now_available` waits for the US digital date

**Target repo:** upcoming-movies-backend only. No migration, no frontend change, no Coolify change.
One one-off prod script (§3), run by Tom after deploy.

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1538 (bug, no priority)
**Project:** bl: Maintenance (no milestone, no project spec)
**Related:** D-27 / D-28 (the provider poll and the first-observation rule this narrows),
NEU-1542 / ADR-0023 (the site tracks a film to its first home availability; the D-1542.8 chip
already keys on the US digital date), NEU-1532 (the three-phase providers run, D-1532.4 phase
order, D-1532.7 prune-by-script precedent), NEU-1206 (governing date = earliest per
(country, type)), D-26 (the US digital date as the announced half of home release).
**Ground truth read (2026-10-08):** `ingest/providers.py` (`offers_for_region`, `_ledger_types`,
`_insert_first_seen`, `_card_now_available`, `run_provider_poll`, `providers_detail`),
`ingest/tmdb/schemas.py` (`TMDBWatchProviderRegion`), `pipeline_run.py::run_providers_stage`,
`catalog/release_grade.py` (`HOME_RELEASE_TYPES`, `PRIMARY_REGION`), `catalog/queries.py`,
`catalog/models.py` (`AvailabilityFirstSeen`, `FilmReleaseDate`), `scripts/prune_released_trailer_cards.py`,
`scripts/rerender_now_available_summaries.py`, `tests/integration/ingest/test_providers.py`,
`tests/integration/scripts/test_prune_released_trailer_cards.py`, `CONTEXT.md` **Now-available
event**, **Home-release date**, ADR-0023, and a live TMDB probe (below).

---

## 1. What is wrong, and what changes

Fandango At Home lists films for purchase weeks before they can be watched, and JustWatch —
TMDB's source for `/movie/{id}/watch/providers` — passes the listing on as an ordinary `buy`
offer. The payload carries no pre-order flag, no date, nothing: `TMDBWatchProviderRegion` is
`link` plus three provider lists, and the three lists are all there is. D-28 cards the first
observation of a monetization type, so a pre-order cards `now_available` and the film page and
feed say the film is out at home while it is still in cinemas.

**A live probe (2026-10-08) pins the shape.** Of the 25 most popular films in the local snapshot
with a US theatrical date between 2026-08-20 and 2026-10-07, 12 had US offers. Nine were real
releases: each had a US digital (type 4) date on or before the probe day and three to six
providers under both `rent` and `buy`. The other three were the bug:

| tmdb_id | US theatrical | US digital in TMDB | offers |
|---|---|---|---|
| 1283515 | 2026-10-02 | none | `buy`: Fandango At Home |
| 1375441 | 2026-09-25 | none | `buy`: Fandango At Home |
| 1244523 | 2026-10-02 | none | `buy`: Fandango At Home |

A film a week out of cinemas, buy-only, one provider, and **no digital date entered yet**. The
same probe over 23 older films (theatrical 2025-12 to 2026-06) found offers on 21, and 20 of
those carried a US type-4 date; the one that did not is a 2015 title with a 2026 re-release.
So the announced digital date is reliable where it matters, and its absence is the pre-order's
fingerprint rather than a gap to work around.

**The ledger makes the first card the only card.** `availability_first_seen` is insert-only and
`now_available` fires for a type the film had no row under. A pre-order row under `buy` means
the real release — the day the title can actually be bought and, usually, rented — cards
nothing for `buy` ever again. So a fix that only suppresses the card is not a fix, and the
cards already raised cannot be cleaned up without their ledger rows (§3).

**Where the bad cards show.** The film page's where-to-watch box went with NEU-1542; what the
ticket calls "appearing on film pages" is the `now_available` event card in the film's day
groups, the same event the feed, the title-follow timelines and the digest carry. One event,
one fix.

### Decided in the planning session (2026-10-08)

- **D-1538.1 An offer is availability only from the US digital date.** The provider poll
  accepts a film's offers — any monetization type, any provider — only when the film has a US
  (`PRIMARY_REGION`) type-4 governing date (earliest type-4 row, per NEU-1206) **on or before
  the observation day**. Otherwise every offer in that observation is held. This is the rule
  D-1542.8 already applies to the TMDB watch-page chip, applied to the ledger. *Rejected:* a
  rent/flatrate rescue for undated films (pre-orders are buy-only today, but that is a second
  rule resting on a vendor habit); the buy-only heuristic alone (a real buy-first release never
  cards, a pre-order under `rent` slips through); excluding provider 7, Fandango At Home (Apple
  and Amazon list pre-orders too, and Fandango-only real availability goes unreported).
  **Accepted cost:** a film TMDB never gives a US digital date never cards `now_available` —
  1 in 21 in the probe, and that one a re-release.
- **D-1538.2 Held means nothing written.** A held observation writes no `availability_first_seen`
  row and no event; the poll re-judges the film on its next daily pass, and the first pass on
  or after the digital date inserts and cards with that pass's `first_seen_at`. The
  `watch_provider` upsert still runs for held offers (it is a name/logo table, not a fact about
  the film). Nothing is remembered about a hold — no column, no counter per film — because the
  next pass sees the same offers again and the date decides.
- **D-1538.3 The date comes from the catalog, not from TMDB live.** The gate reads
  `film_release_date`, filled by the sweep's refresh phase for in-play films and by the
  providers run's own release-date pass (D-1532.4) for released ones. That pass runs *after* the
  provider pass, so a digital date first entered in TMDB today is seen by tomorrow's provider
  pass — a one-day lag, only on a date entered the day it lands. Accepted; the phase order
  stays (the provider pass tombstones 404s first so the later passes do not re-ask).
- **D-1538.4 One predicate, two callers.** The gate is written once — a clause on the film's US
  type-4 governing date against an `as_of` day, beside the other film clauses in
  `catalog/queries.py` (or a pure helper the clause and the script both call; the implementer's
  choice, but one definition) — and the poll and the cleanup script (§3) both use it. The
  script judges each card as of the UTC day of its `occurred_at`, which is the day the poll
  observed the offers.
- **D-1538.5 The cards already raised are deleted with their ledger rows, by a dry-run script.**
  D-1532.7's reasoning for delete-not-supersede holds. Unlike the trailer prune, the ledger rows
  behind each deleted card go too — `availability_first_seen` rows with the card's `film_id`,
  `region` and `first_seen_at == occurred_at`, the same join `rerender_now_available_summaries`
  uses — because leaving them keeps those films silent for the real release. After the script,
  the next poll re-judges each film under D-1538.1: a pre-order still without a date is held,
  a film whose date has since passed cards again on that pass.
- **D-1538.6 The hold is visible on the run.** `ProvidersResult` gains `held` (films whose
  observation was held, not offers) and the detail line reports it beside `first seen`, so a
  poll that holds everything because the release-date pass broke is readable in `ingest_run`.

---

## 2. Acceptance — the poll (`ingest/providers.py`, `catalog/queries.py`)

- In `run_provider_poll`, after `offers_for_region` and before `_ledger_types` /
  `_insert_first_seen`: if the film fails the D-1538.1 gate as of `today` (the one `today`
  `run_providers_stage` hands every phase), the film's ledger insert and carding are skipped,
  `result.held += 1`, `result.offers` still counts the offers seen, `record_progress` and the
  commit still happen. A film with no offers at all is not "held" (nothing to hold).
- The gate reads the film's US type-4 governing date: `MIN(release_date)` over
  `film_release_date` rows with `iso_3166_1 == PRIMARY_REGION` and `release_type == 4`, compared
  as a date against `as_of` the way `poll_set_clause` compares its theatrical dates. No row →
  fails. It does not consult type 5 (never displayed, never carded — D-1542.5) or any other
  region.
- `providers_detail` adds `{held} held` to the line; `held` is 0 in every existing test's
  detail assertion (update them).
- Tests in `tests/integration/ingest/test_providers.py`, each with respx answering
  `/watch/providers` with a `buy` offer on provider 7 unless stated:
  - **no US digital date → held**: no `availability_first_seen` row, no event, `held == 1`,
    `first_seen == 0`, `cards == 0`, `offers == 1`;
  - **US digital date tomorrow → held**; **US digital date today → accepted** (row + card);
    **US digital date yesterday → accepted**;
  - **two US type-4 rows, earliest on or before today, later one in the future → accepted**
    (earliest governs);
  - **only a non-US type-4 date on or before today → held**; **only a US type-5 date on or
    before today → held**;
  - **held then accepted**: poll once with the date in the future (held), set the date to
    today, poll again with a later `now` — one ledger row and one card, `occurred_at ==` the
    second `now`, nothing from the first pass;
  - **flatrate with no date → held** (the rule has no per-type branch; pin it);
  - **a film in the set with no offers is neither held nor first-seen** (`held == 0`);
  - every existing carding test's fixture film gains a US type-4 date on or before the test's
    `today`, and its assertions stay as they are.

## 3. Acceptance — the cleanup (`scripts/prune_preorder_availability.py`)

Modelled on `scripts/prune_released_trailer_cards.py`: argparse, dry run by default, `--apply`
to write, audit printed from a dry pass before any delete, `COPY`'d into the image per
`AGENTS.md`.

- **Selects** catalog-provenance `now_available` events whose film fails the D-1538.4 predicate
  as of the UTC day of `occurred_at` (US type-4 governing date missing, or after that day).
  Story-provenance events are not `now_available` and are not touched.
- **Deletes**, with `--apply`: `event_story` and `event_summary` rows for those events
  explicitly, the events (`app.notification` cascades), and the `availability_first_seen` rows
  matching each event's `(film_id, region, first_seen_at == occurred_at)`. Counts per table are
  logged. *Widened in implementation:* every ledger row on a pruned card's film that fails the
  same predicate as of its own UTC `first_seen_at` day goes, a superset of the observation
  rows — a second store listing the pre-order on a later day inserted an uncarded `buy` row,
  and leaving it would keep the film silent for the real release exactly as D-1538.5 warns.
- **Audit** per card: film title, tmdb_id, the US digital date as the catalog holds it now (or
  `none`), `occurred_at`, `subject_key`, the providers named by the ledger rows about to go,
  and the digest rows it earned (user_id, status, sent_at — user id, not email, as the trailer
  prune does).
- **Tests** in `tests/integration/scripts/test_prune_preorder_availability.py`: a pre-order
  card (no date) is selected and, with `--apply`, the event, summary, story links, notification
  and ledger rows are gone and a sibling legitimate card on another film (date before
  `occurred_at`) and its ledger rows remain; a card whose film's date is *after* `occurred_at`
  is selected; a card whose date equals the `occurred_at` day is kept; dry run deletes nothing;
  after `--apply`, a second `run_provider_poll` over the pruned film with the date now in the
  past cards it again (the D-1538.5 round trip).

## 4. Docs

- `CONTEXT.md` **Now-available event**: "first observed … on or after its US digital date";
  note the hold. (Edited in planning; keep it in step with the final wording.)
- ADR-0023: one consequence line noting this amendment (edited in planning).
- Module docstring of `ingest/providers.py`: a paragraph on the gate under "Insert-only
  ledger", naming the pre-order case and D-1538.1–2.
- `AGENTS.md` providers-slot section: one sentence that a `held` count that jumps to the whole
  set means the release-date pass stopped writing dates.

## 5. Production notes (Tom)

1. Deploy the gate first. Running the script before the gate deploys would let the next poll
   re-card the same pre-orders.
2. `task shell` → `python scripts/prune_preorder_availability.py` (dry) → read the audit → `--apply`.
   Expect the three probe films above among the cards if they are in the poll set, plus any
   earlier pre-orders. Legitimate cards on films TMDB has never dated are also selected; that
   is D-1538.1's accepted cost applied retroactively, and the audit shows them.
3. The next `providers` run reports `held` for the first time; a large `held` against a small
   `first seen` is normal the day after the prune.

## 6. Out of scope / deferred

- A pre-order on a film whose TMDB digital date is wrong (already past) still cards; TMDB is the
  system of record and nothing here second-guesses a date that has landed.
- Films TMDB never dates: accepted silent (D-1538.1). No rescue rule, no provider denylist.
- Phase reordering in `run_providers_stage` (D-1538.3), the `subject_key` grain, the summary
  renderer, the re-render script, the frontend, JustWatch attribution, the D-1542.8 chip.
- Churn, departures, snapshots: unchanged non-goals (ADR-0023).
