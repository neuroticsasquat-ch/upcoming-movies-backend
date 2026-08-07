"""The recall gate's two fixtures have to agree about which films exist.

Split out from the gate itself because it needs no database: a labeled film with no
catalog entry is unretrievable by construction, which the gate would report as a
retrieval miss. Catching it here says what actually went wrong."""

from tests.fixtures.link_retrieval import about_items, labeled_tmdb_id, retrieval_catalog


def test_the_fixture_catalog_covers_every_labeled_film():
    labeled = {labeled_tmdb_id(item) for item in about_items()}
    catalogued = {entry.tmdb_id for entry in retrieval_catalog()}

    assert not labeled - catalogued, (
        "validation_set.json labels films missing from retrieval_catalog.json: "
        f"{sorted(labeled - catalogued)} — re-run scripts/export_retrieval_catalog.py"
    )


def test_the_fixture_catalog_carries_no_unlabeled_films():
    """The catalog is the labeled films and nothing else — it is not a distractor set.

    A stray film would quietly change what the gate measures: recall stays a per-film
    question, but the candidate cap starts competing over a corpus the floor was never
    measured on."""
    labeled = {labeled_tmdb_id(item) for item in about_items()}
    catalogued = {entry.tmdb_id for entry in retrieval_catalog()}

    assert not catalogued - labeled, (
        "retrieval_catalog.json holds films no validation_set.json row labels: "
        f"{sorted(catalogued - labeled)} — re-run scripts/export_retrieval_catalog.py"
    )
