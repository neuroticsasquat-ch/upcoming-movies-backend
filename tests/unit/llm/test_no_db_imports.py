"""The `llm` package is deliberately self-contained: nothing under it imports `db` or
`sqlalchemy` (design §4). That separation is what lets `CallResult`/`CallLog` be plain
dataclasses a stage turns into rows, and what keeps the adapters unit-testable without a
database. Asserted here rather than left to review, because the pull the other way — "just
write the row where the call happens" — is constant."""

import ast
import pathlib

_LLM_PACKAGE = pathlib.Path(__file__).resolve().parents[3] / "src" / "upmovies" / "llm"
_FORBIDDEN = ("sqlalchemy", "upmovies.db", "upmovies.ingest")


def _imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
    return modules


def test_the_llm_package_imports_no_database_machinery():
    modules = sorted(_LLM_PACKAGE.glob("*.py"))
    assert modules, "expected to find the llm package's modules"
    offenders = {
        path.name: sorted(
            m for m in _imported_modules(path.read_text()) if m.startswith(_FORBIDDEN)
        )
        for path in modules
    }
    assert {name: found for name, found in offenders.items() if found} == {}
