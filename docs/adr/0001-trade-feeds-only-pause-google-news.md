# Pause Google News ingestion; rely on curated in-code trade feeds

**Status:** accepted

## Context

News ingestion drew from two kinds of source: a handful of curated **trade feeds**
hard-coded in `news/feeds.py`, and **Google News** searches (four broad topic queries
plus one per-tracked-film title search). In practice almost every low-trust story on the
site arrived via Google News — content farms, SEO aggregators, and machine-reposters that
the LLM domain judge was built to triage after the fact. Google News also carried a latent
defect: its stories are redirect URLs that must be decoded to a publisher domain before the
admin block gate can act on them, so a Google story whose redirect never decoded was
**unblockable** — admin blocks appeared not to "stick."

## Decision

Pause all Google News ingestion and run on **trade feeds only**, on a reversible trial
basis, to evaluate whether the curated trades alone are sufficient.

- A single master flag, `NEWS_GOOGLE_ENABLED` (**default `false`**), hard-gates *both*
  Google mechanisms (broad queries and per-film searches). The existing
  `FEEDS_PER_FILM_ENABLED` setting and `?per_film=` trigger override survive untouched but
  are subordinate — moot while the master gate is off. Reverting the trial is a one-line
  default flip or an env var; no Google code is deleted.
- The trade set is expanded from 4 to 8 feeds (added: IndieWire, Filmmaker Magazine,
  The Wrap, Screen Daily), each verified to return parseable RSS.
- A one-time script (`scripts/cleanup_google_sources.py`, mirroring
  `cleanup_blocked_sources.py`) purges the existing Google-sourced backlog: `pending` +
  `linked` stories from Google sources are rejected with `link_note = "google-paused"`,
  and their events are repaired (emptied events deleted, mixed events re-summarized).

## Considered alternatives

- **Delete the Google code** rather than flag it — rejected: the ticket is an evaluation,
  and reverting must be cheap.
- **Let the backlog age out** instead of purging — rejected: the site is visibly "filled"
  with Google-sourced junk now, and leftover `pending` Google stories would still cluster
  on the next link run.
- **Fix the block-gate decode bug** — deferred: the bug is purely a Google-News artifact.
  Trade-feed stories carry a resolvable domain, so blocking works correctly for them, and
  turning Google off removes every story class that can exhibit the bug. It is resolved *by
  removal, not repair*; if Google is ever un-paused, this remains a known live issue.

## Consequences

- The source-quality machinery (LLM domain judge, block gate, admin Sources page) is left
  running as-is. It now guards only ~8 trade domains — negligible cost, and it keeps a
  working block lever should a trade feed turn noisy.
- A future project may add a DB-backed admin UI to manage sources; at that point the
  overlapping `NEWS_GOOGLE_ENABLED` / `FEEDS_PER_FILM_ENABLED` flags should be consolidated.
