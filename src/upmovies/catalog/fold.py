"""The search fold (CONTEXT.md **Search fold**, ADR-0020): lowercase, Latin diacritics mapped
to their base letter, every non-alphanumeric dropped — so 'Spider-Man' / 'Shōgun' compare as
'spiderman' / 'shogun'.

It exists twice on purpose. `fold_sql` is the SQL text Postgres stores in each `<col>_fold`
generated column; `public/service.py::_normalize_query` folds the user's query the same way
in Python. Both are built from the translate() strings below, and a test pins that each
model's `Computed` is exactly `fold_sql` of its source column. Changing the fold means
changing both sides and a migration that rewrites every fold column.
"""

import unicodedata

from sqlalchemy import Computed, column, func, literal_column
from sqlalchemy.dialects import postgresql


def _build_diacritic_maps() -> tuple[str, str]:
    """Build translate() from/to strings mapping each lowercase Latin letter that carries a
    diacritic to its base ASCII letter (é→e, ō→o, ñ→n, …). Covers the precomposed singles in
    Latin-1 Supplement + Latin Extended-A/B; multi-char folds (æ, ß) are left untouched."""
    frm: list[str] = []
    to: list[str] = []
    seen: set[str] = set()
    for cp in range(0x00C0, 0x0250):
        ch = chr(cp)
        if not ch.isalpha():
            continue
        base = unicodedata.normalize("NFD", ch)[0]
        if not (base.isascii() and base.isalpha()) or base == ch:
            continue
        low = ch.lower()
        if len(low) != 1 or low in seen:
            continue
        seen.add(low)
        frm.append(low)
        to.append(base.lower())
    return "".join(frm), "".join(to)


DIACRITIC_FROM, DIACRITIC_TO = _build_diacritic_maps()


def fold_sql(column_name: str) -> str:
    """The fold of `column_name` as SQL text, for a `GENERATED ALWAYS AS (…) STORED` column.
    `[:alnum:]` is Unicode-aware in the UTF-8 DB, so non-Latin titles (e.g. '기생충') survive."""
    expr = func.regexp_replace(
        func.translate(func.lower(column(column_name)), DIACRITIC_FROM, DIACRITIC_TO),
        "[^[:alnum:]]",
        "",
        "g",
    )
    return str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def fold_computed(column_name: str) -> Computed:
    """The stored generated-column clause for `column_name`'s fold. Wrapped in
    `literal_column` because a plain string would be parsed as `text()`, which reads the
    `:alnum` inside `[:alnum:]` as a bind parameter."""
    return Computed(literal_column(fold_sql(column_name)), persisted=True)
