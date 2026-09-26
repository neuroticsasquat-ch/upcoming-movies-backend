"""The `mail` package is self-contained in the same way the `llm` one is: nothing under it
imports `db` or `sqlalchemy`. That separation is what lets `Envelope` be a plain dataclass a
caller turns into a notification row, and what keeps the adapter unit-testable without a
database. Asserted here rather than left to review, because the pull the other way — "just
mark the notification sent where the send happens" — will be constant once D-31's queue lands."""

import ast
import pathlib

_MAIL_PACKAGE = pathlib.Path(__file__).resolve().parents[3] / "src" / "upmovies" / "mail"
_FORBIDDEN = ("sqlalchemy", "upmovies.db", "upmovies.app", "upmovies.routers")


def _imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
    return modules


def test_the_mail_package_imports_no_database_machinery():
    modules = sorted(_MAIL_PACKAGE.glob("*.py"))
    assert modules, "expected to find the mail package's modules"
    offenders = {
        path.name: sorted(
            m for m in _imported_modules(path.read_text()) if m.startswith(_FORBIDDEN)
        )
        for path in modules
    }
    assert {name: found for name, found in offenders.items() if found} == {}
