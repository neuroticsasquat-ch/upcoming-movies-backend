import subprocess
import sys
import textwrap


def test_importing_db_alone_registers_all_models_and_resolves_fks():
    # Reproduces the standalone-script scenario: a fresh process that imports only
    # `upmovies.db` (as scripts do via `SessionLocal`) must end up with complete metadata, so
    # cross-schema FKs resolve at flush time instead of raising NoReferencedTableError. Runs in
    # a subprocess because this test process already has every model imported via conftest.
    code = textwrap.dedent(
        """
        from upmovies.db import Base
        tables = set(Base.metadata.tables)
        required = {"app.user", "catalog.film", "news.event_summary", "news.story",
                    "ingest.ingest_run"}
        missing = required - tables
        assert not missing, f"missing tables: {missing}"
        # Resolving each FK's target column raises NoReferencedTableError if the referenced
        # table was never registered — the exact failure this fix prevents.
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                assert fk.column is not None
        print("OK")
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_event_summary_edited_by_fk_resolves_to_app_user():
    # The specific cross-schema FK that failed the cleanup script's flush before this fix.
    import upmovies.models  # noqa: F401  -- register every model
    from upmovies.db import Base

    event_summary = Base.metadata.tables["news.event_summary"]
    fk = next(fk for fk in event_summary.foreign_keys if fk.parent.name == "edited_by")
    assert fk.column.table.fullname == "app.user"  # resolves without NoReferencedTableError
