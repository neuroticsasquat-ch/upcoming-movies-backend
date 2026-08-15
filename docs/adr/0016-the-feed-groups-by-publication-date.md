# The feed groups by publication date, not by when the change occurred

**Status:** accepted

## Context

`field_events` sets `Event.occurred_at` to the time the underlying change happened, and
`Event.created_at` to the time we wrote the row. For a story-triggered event the two are close.
For a catalog-sourced event (ADR-0014) they are not: the sweep reads a window of
`catalog.film_field_change` rows, so a change TMDB recorded a week ago can be published today.

Every public surface uses `created_at`:

- `public/service.py::get_feed_grouped` — the day heading is
  `cast(func.timezone("UTC", Event.created_at), Date)`
- `public/service.py::get_film` — the film timeline orders by `Event.created_at.asc()`
- `public/service.py::get_feed` — the flat feed orders by `Event.created_at.desc()`
- `EventOut` does not expose `occurred_at` at all

On 2026-08-11 the first sweep with the directors tranche live carded 77 catalog-sourced events
covering the previous 7 days of field changes. The homepage rendered all of them under a single
heading — *"tuesday, august 11, 2026 — Show all 74 updates"* — against 1–5 updates on each
surrounding day, and a film-page card read *"tuesday, august 11, 2026"* for a change whose
`occurred_at` was 2026-08-04. That reads as a bug on sight, and was nearly "fixed" as one. This
ADR exists so it is not.

## Decision

**`created_at` is the axis for every public surface. Do not change it to `occurred_at`.**

The front page is a **publication log**: "latest updates" means updates newly published to the
site, not events newly occurred in the world. Grouping by `occurred_at` would scatter a day's
newly-published events backwards into dates a returning reader has already scrolled past, so
genuinely new information would arrive already buried.

The same axis is the right trigger for **watchlists and notifications**. Publication is the
moment there is something to tell a reader about. Keying a notification off `occurred_at` means
either firing today for something dated last week, or — under any "only notify on recent events"
rule — firing nothing at all for a backdated event that is, to the reader, brand new.

The 2026-08-11 pile-up is a **one-time migration artifact**. The sweep runs daily, so in steady
state `occurred_at` and `created_at` differ by at most a day and the two groupings are
indistinguishable. Any future backfill or catalog tranche will produce the same one-day spike,
and that is the designed behaviour: a backfill *is* a publication event.

## Considered alternatives

- **Group by `occurred_at`.** Rejected — buries new information under dates the reader has
  already passed, and breaks the notification trigger described above.
- **Group by `occurred_at` but re-sort backdated events to the top.** Rejected: this is
  `created_at` ordering with an inconsistent date label stapled on.
- **Suppress or spread the backfill spike across the days it covers.** Rejected: it fabricates a
  publication history that did not happen, and the spike is one-time by construction.

## Consequences

- A backfill, a new catalog tranche, or any sweep that widens its lookback window will land as
  one tall day on the homepage. Expected, not a defect. NEU-1123 tracks sizing that window.
- A card published today about a change that occurred a week ago carries no indication of the
  gap: the body reads *"Release date moved from X to Y"* with no date qualifier. Harmless at a
  one-day lag.
- **Residual, deliberately left open.** If some future source produces materially backdated
  events as a routine matter, the fix is to **disclose `occurred_at` on the card** — not to
  regroup the feed. `FeedItem` already carries `occurred_at`; `EventOut` does not, and nothing
  in the frontend renders it. That is the work, if it is ever wanted.
