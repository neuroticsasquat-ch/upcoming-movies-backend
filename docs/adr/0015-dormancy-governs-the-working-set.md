# Dormancy, keyed on quiescence, governs the ingestion working set

**Status:** accepted

## Context

A dated film ages out of the working set by itself: it releases, `release_date < today`, and
`active_film_clause` drops it. An undated film never does — the clause deliberately keeps
`release_date IS NULL` rows active, and TMDB almost never flips a dead project to `Canceled`.
Dev-hell entries sit at `Planned` indefinitely.

So once the undated-film expansion lands, every film we ever admit stays in the candidate
retrieval index and draws a per-film news query in perpetuity. The working set becomes
monotonically increasing, and the cap saturation the candidate-retrieval spec already predicts
(p90 ~45 candidates at 5× catalog, ~180 at 20×) never gets relief.

The seed set for discovery (ADR-0013) has the same shape: admitted films contribute their own
credits as seed people, which find more films, which contribute more credits.

`active_film_clause` gates exactly two consumers — `link/retrieval/index.py` and
`news/fetcher.py::_film_titles` — and no public surface. That makes "leave the working set" and
"disappear from the site" separable.

## Decision

An undated film goes **dormant** when, for N days, TMDB has recorded no semantic change to it
(no `catalog.film_field_change` row) **and** no story has linked to it. Dormant films leave
`active_film_clause`, and therefore leave the retrieval index, the per-film query list, and the
seed-person query. Any subsequent change or linked story **revives** it automatically — the
predicate is derived, not a stored flag.

**Keyed on quiescence, not age.** A film can be real and quiet for a year; an age rule would
retire it wrongly.

**Dormant is quieter, not free.** Dormant films remain on a reduced-cadence refresh. Detecting
the change that revives a film *requires re-fetching it*, so a dormancy that also stopped the
refresh would be a one-way door with no handle on the other side. N and the dormant refresh
cadence are both set from the discovery probe, not guessed.

**Films are never deleted, and no public surface changes.** The prerelease spec is explicit that
status filtering "is not a pruning mechanism". A dormant film keeps its page and its events.

## Considered alternatives

- **Key dormancy on age since admission.** Simpler and more predictable, but wrong for a
  slow-burn project that is genuinely alive.
- **Delete or archive dormant films.** Rejected: contradicts standing retention policy, and
  throws away events we already published.
- **Hide dormant films from public surfaces too.** Rejected: dormancy exists to control ingestion
  cost, and un-announcing a film we announced is a product regression, not a saving.
- **Stop refreshing dormant films entirely.** Rejected: creates the revival deadlock above.
- **A hard cap on seed-set or index size.** Rejected: an arbitrary constant that discards the
  *newest* or *least popular* entries rather than the *dead* ones.

## Consequences

- Dormancy is load-bearing for **three** independent cost curves: the retrieval index, the
  per-film query list, and the discovery seed set. A large tuned N inflates all three together,
  so the tuning ticket must check all three rather than retrieval alone.
- The refresh set never shrinks to zero, so the sweep's floor cost grows slowly and permanently
  with everything ever admitted.
- Revival latency is bounded by the dormant refresh cadence, not by the daily sweep — a project
  that comes back to life is noticed a cadence-period late.
