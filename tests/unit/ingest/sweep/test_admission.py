"""The admission gate: the master switch, the three flags that ramp admission by seed grade
(spec §7.4) and the `followed` tranche beside them (D-50). Every flag is off by default, which
is what makes a sweep that admits nothing the *configured* state rather than a code state.
"""

import pytest

from upmovies.config import Settings
from upmovies.ingest.sweep import AdmissionTranches

ALL_ROLES = {"director", "writer", "cast", "followed"}

DIRECTORS_ONLY = AdmissionTranches(enabled=True, directors=True)
WRITERS_ONLY = AdmissionTranches(enabled=True, writers=True)
CAST_ONLY = AdmissionTranches(enabled=True, cast=True)
FOLLOWED_ONLY = AdmissionTranches(enabled=True, followed=True)


def test_nothing_is_admitted_by_default():
    assert AdmissionTranches().admits(ALL_ROLES) is False


@pytest.mark.parametrize("roles", [{"director"}, {"writer"}, {"cast"}, {"followed"}, ALL_ROLES])
def test_the_master_switch_overrides_every_open_tranche(roles):
    """`SWEEP_ENABLED=false` is the one-move rollback (§7.3): the tranche flags keep their
    settings and admit nothing regardless."""
    tranches = AdmissionTranches(
        enabled=False, directors=True, writers=True, cast=True, followed=True
    )

    assert tranches.admits(roles) is False


@pytest.mark.parametrize(
    ("tranches", "role"),
    [(DIRECTORS_ONLY, "director"), (WRITERS_ONLY, "writer"), (CAST_ONLY, "cast")],
)
def test_an_open_tranche_admits_its_own_seed_grade(tranches, role):
    assert tranches.admits({role}) is True


@pytest.mark.parametrize(
    ("tranches", "role"),
    [
        (DIRECTORS_ONLY, "writer"),
        (DIRECTORS_ONLY, "cast"),
        (WRITERS_ONLY, "director"),
        (WRITERS_ONLY, "cast"),
        (CAST_ONLY, "director"),
        (CAST_ONLY, "writer"),
    ],
)
def test_an_open_tranche_admits_nothing_else(tranches, role):
    """The ramp is per grade, so no tranche may carry another in with it — otherwise the
    retrieval-health guard sees one cliff instead of three steps, and a precision drop can
    no longer be attributed to the grade that caused it."""
    assert tranches.admits({role}) is False


@pytest.mark.parametrize(
    ("roles", "admitted"),
    [(set(), False), ({"director"}, True), ({"cast"}, False), ({"director", "cast"}, True)],
)
def test_opening_writers_changes_no_verdict_that_did_not_involve_a_writer(roles, admitted):
    """The ramp's second step (NEU-1089) has to be *additive*: a candidate no writer reached
    is admitted or withheld exactly as it was before the flip. Otherwise the before/after
    precision comparison §7.4 asks for carries two variables instead of one."""
    assert DIRECTORS_ONLY.admits(roles) is admitted
    assert AdmissionTranches(enabled=True, directors=True, writers=True).admits(roles) is admitted


@pytest.mark.parametrize(
    ("roles", "admitted"),
    [(set(), False), ({"director"}, True), ({"writer"}, True), ({"director", "writer"}, True)],
)
def test_opening_cast_changes_no_verdict_that_did_not_involve_cast(roles, admitted):
    """The ramp's last move (NEU-1090), and the largest — it roughly doubles the catalog, so
    it is the one most likely to be blamed for a precision drop it did not cause. Additivity
    is what keeps that attributable."""
    before = AdmissionTranches(enabled=True, directors=True, writers=True)

    assert before.admits(roles) is admitted
    assert (
        AdmissionTranches(enabled=True, directors=True, writers=True, cast=True).admits(roles)
        is admitted
    )


def test_one_open_tranche_is_enough():
    """A candidate reached at several grades is admitted as soon as any one of them is
    open — the film is the unit of admission, not the credit."""
    assert DIRECTORS_ONLY.admits({"cast", "director"}) is True


def test_no_seed_grade_role_is_never_admitted():
    assert (
        AdmissionTranches(enabled=True, directors=True, writers=True, cast=True).admits(set())
        is False
    )


def test_from_settings_reads_the_master_and_the_three_tranches(monkeypatch):
    """The env flags are the gate. Mapping them here rather than at the entrypoint keeps
    the wiring in one place and testable before there is an entrypoint to test it through."""
    monkeypatch.setenv("SWEEP_ENABLED", "true")
    monkeypatch.setenv("SWEEP_ADMIT_WRITERS", "true")
    settings = Settings()  # type: ignore[call-arg]

    assert AdmissionTranches.from_settings(settings) == AdmissionTranches(
        enabled=True, directors=False, writers=True, cast=False
    )


def test_from_settings_is_closed_on_a_default_deploy(monkeypatch):
    monkeypatch.delenv("SWEEP_ENABLED", raising=False)

    assert AdmissionTranches.from_settings(Settings()).admits(ALL_ROLES) is False  # type: ignore[call-arg]


# --- the followed tranche (D-50) ------------------------------------------------------------


def test_a_followed_attachment_needs_its_own_tranche():
    """A candidate reached *only* through a followed person's non-seed credit is withheld by
    the three seed-grade flags however wide open they are: `followed` is not a fourth grade of
    the same evidence, so nothing else can stand in for it."""
    seed_grades = AdmissionTranches(enabled=True, directors=True, writers=True, cast=True)

    assert seed_grades.admits({"followed"}) is False
    assert FOLLOWED_ONLY.admits({"followed"}) is True


def test_a_seed_grade_role_admits_whatever_the_followed_flag_says():
    """The film is the unit of admission: a followed person who reaches a candidate *as its
    director* is admitted by `directors`, because the candidate was reached at a seed grade
    like any other. `followed` only ever decides candidates nothing else reached."""
    assert DIRECTORS_ONLY.admits({"director", "followed"}) is True
    assert FOLLOWED_ONLY.admits({"director"}) is False


def test_the_followed_flag_comes_from_its_own_setting(monkeypatch):
    monkeypatch.setenv("SWEEP_ENABLED", "true")
    monkeypatch.setenv("SWEEP_ADMIT_FOLLOWED", "true")

    assert AdmissionTranches.from_settings(Settings()) == AdmissionTranches(  # type: ignore[call-arg]
        enabled=True, followed=True
    )
