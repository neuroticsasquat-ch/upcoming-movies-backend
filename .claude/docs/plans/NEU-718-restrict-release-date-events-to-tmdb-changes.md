# NEU-718 — Restrict release-date events to genuine TMDB changes

Grounds `release_date` event creation in TMDB's release-date change history instead of the
clustering LLM's classification. See `docs/adr/0002-release-date-events-grounded-in-tmdb-change.md`
for the decision and `CONTEXT.md` (Release-date events) for the vocabulary.

## The rule (replaces `link/cluster.py:547-567`)

For a group the LLM classifies as `release_date`, in `apply_cluster_decisions`:

1. **Corroborated?** `changed_at = await field_changed_at(session, film_id, "release_date")`.
   Corroborated iff `changed_at is not None and (as_of_date - changed_at.date()).days <= W`.
   - A `null → date` first assignment is a recorded change, so this covers "no date yet" too —
     **no separate null branch** (decision N1).
2. **Corroborated → create the event** (existing new-event path, region from the LLM).
3. **Not corroborated → triage on `claimed_date`** (used only to triage, never to create):
   - `claimed_date is not None and claimed_date != film.release_date` → **hold**: `continue`
     *without* touching the story — leave `link_status == "linked"`, create no `EventStory`, do
     not mark rejected — **unless** it has aged out: if `(as_of_date - story_ref.date()).days > W`
     reject with `link_note = "release-date-uncorroborated"`. The story stays in the unclustered
     pool (`cluster.py:308-314`) and is re-evaluated next run.
   - `claimed_date == film.release_date` → **reject now**, `link_note = "release-date-restated"`.
   - `claimed_date is None` → **reject now**, `link_note = "release-date-unchanged"` (vague filler;
     distinct note purely for observability).

`story_ref` = `published_at or fetched_at` (age = how long we've waited for TMDB).

### Held-story wiring caveat

The reject branches add the story to `assigned` before `continue`. A **held** story must be
skipped *without* being added to `assigned` and without an `EventStory` — confirm nothing
downstream treats an unassigned-but-linked story as an error (the loader simply reloads it next
run). This is the one non-obvious bit of the change; cover it with a test.

## Files to touch

- `src/upmovies/link/cluster.py` — replace the `547-567` guard with the rule above; ensure the
  held path bypasses the `EventStory`/`assigned` tail (`594-597`). Same change in the batch path
  (`apply_cluster_decisions` is shared, but check `build_cluster_batch_request` @ 602 and the
  `641/662` plumbing carry the renamed param).
- `src/upmovies/config.py:42` — rename `link_release_restate_days` →
  `link_release_change_window_days`, alias `LINK_RELEASE_CHANGE_WINDOW_DAYS`, default `7 → 14`.
- `src/upmovies/routers/ingest_admin.py:108` — pass the renamed setting.
- `src/upmovies/link/pipeline.py` — rename the threaded param (`186, 203, 230, 281, 314, 407`).
- `src/upmovies/link/cluster.py` — rename `release_restate_days` param (`434, 641, 662`) and the
  `ClusterPlan`/payload plumbing already carrying `film_release_date` / `film_created_at`
  (`film_created_at` is now only needed if we keep an insert-time baseline; with the unified rule
  we key purely off `field_changed_at`, so `film_created_at` may become dead — remove if so).

**No DB migration.** Reads existing `film_field_change`; `link_note` is free text; `claimed_date`
stays transient (not persisted on `Event`).

## Tests (TDD — write first)

Integration tests against the `session` fixture, driving `apply_cluster_decisions` (or the
cluster stage) with a seeded `Film` + `FilmFieldChange` rows:

1. Stable known date (no recent change), story restates it exactly → **rejected**, no event
   (`release-date-restated`).
2. Stable known date, vague story (`claimed_date` null) → **rejected** (`release-date-unchanged`).
3. `film_field_change` shows a `date→date` move within `W` → **event created**.
4. Change was `> W` days ago; roundup restates the now-stable new date → **rejected**.
5. Break-ahead: `claimed_date != film.release_date`, no TMDB change yet → **held** (story still
   `linked`, unclustered, not rejected, no event).
6. The held story from (5), after a `FilmFieldChange` lands within `W` → **event created** on the
   next run.
7. Held story aged past `W` with no corroboration → **rejected** (`release-date-uncorroborated`).
8. Null-date film → story held until a `null→date` change is recorded → **event created** (N1).
9. Regional-only move (primary `release_date` unchanged) → no event (documents R1 scope).

## Out of scope (follow-up tickets)

- Region-aware detection off `film_release_date` (needs change history for that table).
- Persisting `claimed_date` / the new date on `Event`.
- Splitting `W` into separate recency vs hold-TTL knobs if their failure modes diverge.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
