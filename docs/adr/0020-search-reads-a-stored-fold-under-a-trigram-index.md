# Public search reads a stored fold under a trigram index

**Status:** accepted 2026-09-26 — implementation tracked in NEU-1469 (bl: Maintenance)

## Context

The four public search endpoints match a folded form of each title or name — lowercased,
diacritics mapped to base letters, non-alphanumerics stripped — against the folded query with
`LIKE '%q%'`. The fold was computed **in the query**, on every row, every time. It is
non-sargable, so every search was a sequential scan, and the `translate()` step over a
130-character diacritic map made that scan six times slower than a plain `LIKE`. Each endpoint
ran it twice (`total`, then the page). Measured on a refresh of prod: `/people/search` 2.8 s
over 129k people, `/films/search` 0.7 s. The header fans out to all four and waits for all,
so every header search paid the people number.

ADR-0008 declined `pg_trgm` for **candidate retrieval** on two grounds: it would need an
extension install on a shared database, and a pure in-memory function already did the job
once per run. Neither ground applies to public search, which is per keystroke, against the
whole catalog, and already in Postgres.

## Decision

- The fold becomes a **stored generated column** (`<col>_fold TEXT GENERATED ALWAYS AS (…)
  STORED`) beside each searched column: `film.title` / `original_title`,
  `film_alternative_title.title`, `person.name` / `original_name`, `production_company.name`,
  `collection.name`. Postgres computes it on write. The expression is the one the query used,
  unchanged, from one constant the Python-side `_normalize_query` shares.
- Each fold column carries a **`pg_trgm` GIN index** (`gin_trgm_ops`). `LIKE '%q%'` on a
  three-or-more-character query becomes an index probe; a two-character query still scans,
  but the cheap stored column, not the fold.
- `pg_trgm` is created in `migrations/env.py::_ensure_schemas` beside `citext` and `pgcrypto`,
  and in the test and bench bootstraps that mirror it. It is a trusted extension, the prod
  image ships it, and the migration role already creates extensions there.
- What matches, and in what order, does not change. The index accelerates the same predicate.

## Considered alternatives

- **A functional index on the fold expression, no new columns.** The planner uses it only when
  the query's expression is byte-identical to the index's; SQLAlchemy would emit it from one
  place and Alembic from another, and nothing pins them equal. A stored column makes the
  match structural.
- **Stored columns, no extension.** People search falls from 2.8 s to ~150 ms, but it is still
  a full scan that grows with the catalog, and the extension costs one line in three bootstraps.
- **`unaccent`.** Available on the image, but a different fold from the Python side's hand
  map, and the two must agree character for character or a query stops finding what the
  column holds.
- **Dropping `total` for the header's calls.** With the index both statements are cheap; a flag
  would be API surface for nothing.

## Consequences

- Every search is expected under 100 ms on the current catalog; the frontend half of NEU-1469
  adds a running indicator on the assumption that it is now rarely visible.
- One of ADR-0008's two grounds is gone: `pg_trgm` is installed. Retrieval's Postgres route
  stays closed on the other (a pure function suffices, once per run); revisiting it is a
  separate decision, and the retrieval module's docstring says so.
- The fold now exists in two places on purpose — the SQL constant and `_normalize_query` —
  and a test pins their agreement. Changing the fold means changing both and a migration that
  rewrites the seven columns; that is the price of a stored fold and it is recorded here so it
  is not paid by surprise.
- The parity test compares index definitions textually, so model `Index(...)` and migration
  `create_index(...)` must render the same `USING gin (… gin_trgm_ops)`; it does **not** compare
  generated expressions, which is why the expression lives in one constant.
