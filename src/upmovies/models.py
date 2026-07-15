"""Aggregate import of every mapped model, so a single import fully populates
`Base.metadata`. Imported by `upmovies.db`, which nearly all DB code goes through, so metadata
is complete — and cross-schema foreign keys resolve at flush time (e.g.
`news.event_summary.edited_by` -> `app.user`) — even in standalone entrypoints (scripts,
ad-hoc `python -`) that never load the full app graph.

This is the single source of truth for "every model module": register new model modules here."""

import upmovies.app.models  # noqa: F401
import upmovies.catalog.models  # noqa: F401
import upmovies.ingest.models  # noqa: F401
import upmovies.news.models  # noqa: F401
