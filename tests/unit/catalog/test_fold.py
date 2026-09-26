import pytest
from sqlalchemy import Column

from upmovies.catalog.fold import fold_sql
from upmovies.catalog.models import (
    Collection,
    Film,
    FilmAlternativeTitle,
    Person,
    ProductionCompany,
)


@pytest.mark.parametrize(
    ("fold_col", "source"),
    [
        (Film.__table__.c.title_fold, "title"),
        (Film.__table__.c.original_title_fold, "original_title"),
        (FilmAlternativeTitle.__table__.c.title_fold, "title"),
        (Person.__table__.c.name_fold, "name"),
        (Person.__table__.c.original_name_fold, "original_name"),
        (ProductionCompany.__table__.c.name_fold, "name"),
        (Collection.__table__.c.name_fold, "name"),
    ],
)
def test_each_fold_column_stores_fold_sql_of_its_source(fold_col: Column, source: str):
    """One constant, not a resemblance: the parity test does not compare generation
    expressions, so this is what keeps every stored fold on the query's fold (ADR-0020)."""
    computed = fold_col.computed
    assert computed is not None
    assert computed.persisted is True
    assert str(computed.sqltext) == fold_sql(source)


def test_fold_sql_carries_no_bind_parameters():
    """`[:alnum:]` must reach the DDL verbatim; a `text()` wrapping would bind `:alnu`."""
    computed = Film.__table__.c.title_fold.computed
    assert computed is not None
    assert "[^[:alnum:]]" in str(computed.sqltext)
    assert not getattr(computed.sqltext, "_bindparams", {})
