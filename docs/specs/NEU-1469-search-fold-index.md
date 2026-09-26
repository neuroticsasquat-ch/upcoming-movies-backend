# NEU-1469 — Search reads a stored fold under a trigram index

**Status:** approved design (planit session with Tom, 2026-09-26)
**Ticket:** [NEU-1469](https://linear.app/neuroticsasquatch/issue/NEU-1469/search-is-very-slow)
**Project:** bl: Maintenance (no milestone) · **Related:** NEU-1430 (the header search that
fans out to all four endpoints), NEU-1355 (the follows-page box), NEU-1358 (the onboarding
people grid), ADR-0008 (which declined `pg_trgm` for retrieval)
**Target repos:** both. This file is the **backend half** (the speed). The frontend half (the
visual indicator) is `frontend/docs/specs/NEU-1469-search-running-indicator.md`. The two are
independent: neither changes the API contract, so they deploy in either order.
**Base branch:** `release/v1.1.1` (the current release branch; the skills resolve it from the
checkout).
**Blocked by:** nothing · **Blocks:** nothing
**Glossary:** `CONTEXT.md` — **Search fold** (new here), **Squash-fold** (retrieval's, and
deliberately not the same thing). **ADR:** `docs/adr/0020-search-reads-a-stored-fold-under-a-trigram-index.md`.

---

## 1. What to build and why

The four public search endpoints (`/films/search`, `/people/search`, `/companies/search`,
`/collections/search`) fold every candidate row at query time — `lower`, then `translate()`
over a 130-character diacritic map, then a regex strip of every non-alphanumeric — and
substring-match the fold with `LIKE '%q%'`. The fold is non-sargable, so each search is a
sequential scan that runs the fold on every row, twice (once for `total`, once for the page).
Measured on the dev database (a refresh of prod: 129,017 people, 9,977 films, 6,940 alternative
titles, 9,266 companies, 490 collections):

| Endpoint | Wall clock per request | Where it goes |
|---|---|---|
| `/people/search` | 2.8 s | 1.25 s per scan × 2 scans |
| `/films/search` | 0.7 s | 0.35 s per scan × 2, alt-title subquery included |
| `/companies/search` | 0.2 s | |
| `/collections/search` | 0.02 s | |

The `translate()` call is the expensive part: the same scan with a trivial translate runs in
200 ms, with the real map in 1,250 ms. The header (NEU-1430) waits for all four before it shows
anything, so every header search costs the people number; the follows box opens on the People
tab, so it does too.

This ticket makes the fold **stored** and **indexed**, with the fold's semantics unchanged:

- **D-1469.1 The fold becomes a stored generated column.** Each searched text column gains a
  sibling `<col>_fold TEXT GENERATED ALWAYS AS (<fold expr>) STORED`. Postgres computes it on
  write, once, and the search reads the column. The fold expression is the one
  `public/service.py::_normalized_col` emits today, verbatim — `lower` → `translate` with the
  same from/to strings → `regexp_replace('[^[:alnum:]]', '', 'g')` — so `_normalize_query` on
  the Python side keeps agreeing with it character for character. All three functions are
  `IMMUTABLE`, which is what a stored generated column requires.
- **D-1469.2 A `pg_trgm` GIN index on every fold column.** `CREATE INDEX … USING gin
  (<col>_fold gin_trgm_ops)`. This is what turns `LIKE '%q%'` from a scan into an index probe
  for any query of three or more folded characters. A two-character query (`MIN_QUERY_LEN`)
  extracts no trigram and still scans, but scans the stored column, which is the cheap ~50 ms
  scan rather than the 1.25 s one. Nothing changes about what matches: the index accelerates
  the same `LIKE`, it does not replace it with similarity.
- **D-1469.3 `pg_trgm` is created where `citext` and `pgcrypto` are.** `migrations/env.py::
  _ensure_schemas` gains `CREATE EXTENSION IF NOT EXISTS pg_trgm`, and so do
  `tests/conftest.py` and `scripts/bench_follows.py`, which bootstrap the same way. `pg_trgm`
  is a trusted extension since Postgres 13, the prod image offers it (ADR-0008 §Context lists
  it), and `env.py` already exercises the privilege on every `alembic upgrade`. No manual prod
  step.
- **D-1469.4 The alt-title clause becomes a semi-join on the indexed column.** Today
  `_title_match` uses a correlated `EXISTS` that re-runs the fold per film. It becomes
  `Film.id.in_(select(FilmAlternativeTitle.film_id).where(FilmAlternativeTitle.title_fold.like(p)))`
  (or an uncorrelated `EXISTS` on the same predicate — whichever `EXPLAIN` shows using the alt
  table's GIN index once). Each film still appears at most once; the ranking `case()` on the
  primary title match is unchanged.
  *As built:* `Film.id.in_(union_all(<primary-fold ids>, <alt-title ids>))`. `EXPLAIN` on dev
  showed that any OR of the primary match with an alt-title subquery, correlated or not,
  leaves `film` itself on a seq scan (6.9 ms); the one id set BitmapOrs both film fold indexes
  and probes the alt index once (0.5 ms).

Considered and declined (recorded in ADR-0020): a functional index on the expression with no
new columns (the planner uses it only if SQLAlchemy emits byte-identical SQL to the index
definition, which nothing pins); the stored columns without the extension (150 ms people
search, but still a scan that grows with the catalog); `unaccent` (available, but a different
fold from the Python side's, and the two must agree).

## 2. Ground truth (read before coding)

- `public/service.py`: `_build_diacritic_maps()` / `_DIACRITIC_FROM` / `_DIACRITIC_TO` /
  `_PY_DIACRITIC`, `_normalized_col(col)`, `_normalize_query(q)`, `_searchable_query(q)`,
  `_primary_title_match(nq)`, `_title_match(nq)`, `_name_match(nq, *cols)`, and the four
  `get_*_search` functions. The `FUTURE:` note in `_title_match`'s docstring is this ticket;
  delete it. `_normalized_col` is also used as the **sort key** in `get_company_search` and
  `get_collection_search` (`order_by(_normalized_col(Name).asc())`) — that becomes the fold
  column too.
- `catalog/models.py`: `Film` (`title`, `original_title`), `FilmAlternativeTitle` (`title`),
  `ProductionCompany` (`name`), `Collection` (`name`), `Person` (`name`, `original_name`).
  Index naming here is `ix_catalog_<table>_<what>`; the only expression-free precedent is
  `ix_catalog_film_slug`. No `Computed` or `postgresql_using` exists anywhere yet — this ticket
  is the precedent.
- `migrations/env.py::_ensure_schemas` — the extension bootstrap (D-1469.3).
- `tests/integration/test_migrations.py` — the parity test snapshots `information_schema.columns`
  (`column_default`, not `generation_expression`) and `pg_indexes.indexdef`. So the migration's
  index definition must render **identically** to `create_all`'s (`USING gin (name_fold
  gin_trgm_ops)`), and the generated expression is *not* compared — which is why D-1469.1
  insists the expression comes from one place (see §3) rather than trusting the test to catch
  drift.
- `link/retrieval/index.py` module docstring: "closed today only because pgvector is unavailable
  and `pg_trgm` is not installed on the shared instance." After this ticket half of that sentence
  is false. Reword it to say `pg_trgm` is now installed (for public search) and that retrieval's
  Postgres route stays closed on ADR-0008's other grounds; do not reopen retrieval here.
- Callers of the fold outside search: none. `_title_match` / `_name_match` / `_normalized_col`
  are referenced only from `public/service.py` (the import runner's title matching is
  `news/title_match.py`, a different function).

## 3. Model and migration

**Fold expression, one source.** Add a module-level constant in `catalog/models.py` (or a tiny
`catalog/fold.py` the models import) that renders the SQL text of the fold for a column name,
built from the same `_DIACRITIC_FROM` / `_DIACRITIC_TO` strings `public/service.py` uses. Move
`_build_diacritic_maps` and the two strings there; `public/service.py` imports them. The
`Computed(...)` in each model and `_normalize_query` in the service then read the same strings,
and the pyright/ruff pass plus a unit test (§5) pin that `Computed`'s text equals
`_normalized_col`'s compiled SQL.

**Columns.** Each is `Mapped[str | None]`, `mapped_column(Text, Computed(<fold sql>,
persisted=True), nullable=True)` — nullable because `original_title`, `original_name` are
nullable and the fold of NULL is NULL:

| Table | Fold columns |
|---|---|
| `catalog.film` | `title_fold`, `original_title_fold` |
| `catalog.film_alternative_title` | `title_fold` |
| `catalog.person` | `name_fold`, `original_name_fold` |
| `catalog.production_company` | `name_fold` |
| `catalog.collection` | `name_fold` |

Generated columns are never written by application code; the TMDB upsert paths need no change
(a generated column is rejected if named in an INSERT — audit `ingest/tmdb/upsert.py` and any
`insert(...).values(**row)` that could spread a model's columns; none is expected to, since
they build dicts from TMDB payloads).

**Indexes.** In each model's `__table_args__`, for each fold column:

```python
Index("ix_catalog_person_name_fold_trgm", "name_fold",
      postgresql_using="gin", postgresql_ops={"name_fold": "gin_trgm_ops"}),
```

Seven indexes in all, one per fold column, named `ix_catalog_<table>_<col>_trgm`.

**Migration.** One revision: `op.add_column(... sa.Computed(<same sql>, persisted=True) ...)` ×7,
then `op.create_index(... postgresql_using="gin", postgresql_ops={...})` × 7. Autogenerate will
emit the columns and the indexes; check that the ops dict survives into the file (older Alembic
drops `postgresql_ops` from autogenerated output — write it by hand if so) and that the
`Computed` text is the constant, not a pasted copy. Downgrade drops the indexes then the
columns. Adding a stored generated column rewrites the table under `ACCESS EXCLUSIVE`; on prod's
129k-row person table that is seconds, inside the deploy's migration step, and nothing reads the
catalog schema during a Coolify deploy that would care. No `CONCURRENTLY` (it cannot run inside
Alembic's transaction and is not needed at this size).

## 4. Service changes (`public/service.py`)

- `_name_match(nq, *cols)` takes the **fold columns** (`Person.name_fold`,
  `Person.original_name_fold`, `ProductionCompany.name_fold`, `Collection.name_fold`) and emits
  `col.like(pattern)` with no function wrapping.
- `_primary_title_match(nq)` reads `Film.title_fold` / `Film.original_title_fold`.
- `_title_match(nq)` — D-1469.4.
- `get_company_search` / `get_collection_search` order by `<Model>.name_fold.asc(), id.asc()`.
- `_normalized_col` is deleted once nothing calls it. `_normalize_query` stays as the
  Python-side twin of the stored expression.
- Response shapes, `total`, pagination, `MIN_QUERY_LEN`, the two-alphanumerics rule, the
  `_LIVE_PERSON` filter, the popularity ordering: all unchanged. The API contract is untouched;
  the frontend half of this ticket does not depend on this half.

## 5. Tests

- **Migration parity** (`tests/integration/test_migrations.py`) passes unmodified: that is the
  proof the seven index definitions render the same from both paths. The parity fixture needs
  `pg_trgm` in the scratch DB — it runs `alembic upgrade head`, so `env.py`'s bootstrap
  provides it; `create_all` relies on `conftest.py`'s.
- **Fold agreement**: a unit test that compiles `_normalized_col`-equivalent SQL from the
  constant and asserts it is the text inside `Film.__table__.c.title_fold.computed.sqltext`
  (or simply that both are built from one constant — assert identity, not resemblance). And a
  DB test that inserts a `Person` named `"Shōgun / Spider-Man"` and reads back
  `name_fold == _normalize_query("Shōgun / Spider-Man") == "shogunspiderman"`.
- **Existing search tests** (`tests/…/test_public_search*.py` or wherever `get_person_search`
  et al. are covered — find them with `grep -rn "people/search" tests`) pass unmodified: same
  matches, same order, diacritic and punctuation cases included.
- **Index use**: one test per endpoint family runs `EXPLAIN` on the compiled search statement
  for a three-character query and asserts the plan text contains `Bitmap Index Scan` on the
  `_trgm` index (or `Index Scan`), and that the plan for a two-character query does **not**
  fail. This pins that the planner sees the index; it is not a timing assertion (timing tests
  flake). Do this only if the plan is stable across the test DB's statistics — if a 3-char probe
  on a tiny test table prefers a seq scan, assert on a table seeded with ~1,000 rows, or drop the
  assertion and rely on the manual timing below.
- **Manual timing on the PR**: re-run the four `curl -w '%{time_total}'` probes from this
  session (`q=spider`, `q=tom hanks`, `q=warner`, `limit=5`) against the migrated dev DB and
  paste the before/after table into the PR description. Expected: every endpoint under 100 ms.

## 6. Acceptance criteria

1. `/people/search?q=tom%20hanks` answers in under 100 ms on the dev database (was 2.8 s);
   `/films/search` under 100 ms (was 0.7 s). Numbers recorded on the PR.
2. `EXPLAIN` for a three-character query on each of the four endpoints shows the `_trgm` index.
3. Result sets and orderings for every existing search test are byte-identical to before.
4. `pg_trgm` is created by `migrations/env.py`, `tests/conftest.py` and
   `scripts/bench_follows.py`; nothing else asks for it.
5. `task test` (including the migration parity test), `task lint`, `task typecheck` green.
6. `CONTEXT.md` **Search fold** entry and ADR-0020 are in the PR; the `link/retrieval/index.py`
   docstring no longer says `pg_trgm` is not installed.

## 7. Out of scope / deferred

- **Ranking changes** (similarity ordering, `word_similarity`, prefix boosting): the index
  accelerates the existing substring match; the ordering rules stay NEU-1350/NEU-1430's.
- **Retrieval's Postgres route** (ADR-0008): the extension being present removes one of that
  ADR's two grounds, not both; reconsidering it is its own ticket, and the retrieval docstring
  says so.
- **Dropping the `total` count for header calls**: with the index both statements are cheap;
  an `include_total` flag would be API surface for nothing.
- **`unaccent`**: not used; the fold stays the hand-built map so Python and SQL agree.
- **Frontend**: the spinner and stale-result behaviour are the frontend half's spec.
