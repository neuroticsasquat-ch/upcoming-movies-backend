"""What makes a `catalog.film_credit` row **seed grade**: director, writer
(`Writer`/`Screenplay`), or top-5 billed cast (spec §3.2).

This lives in `catalog` rather than next to either consumer because there are two of them and
they must not drift apart. The sweep asks it *whose filmography is worth a request* and *which
role on a candidate film counts* (`ingest.sweep.seeds`); the credit history asks it *which
credit changes are worth recording* (`ingest.tmdb.credit_history`). A history recording a
different definition of seed grade than the sweep enumerates is a history of nothing — and the
two cannot share a module directly, because `ingest.sweep` already imports `ingest.tmdb`.

Producers are deliberately not seed grade. An EP credit travels far and says little about
whether a project is real, and the 2,191 extra people it would add are the weakest of the
four attachments.
"""

DIRECTOR_JOB = "Director"
WRITER_JOBS = frozenset({"Writer", "Screenplay"})
# "Top-5 billed" is TMDB's `order`, which is 0-indexed.
#
# **Measured at NEU-1090; do not tighten this to 3.** Cutting to top-3 is the obvious volume
# lever on the largest tranche, so it was re-run against the same frozen pre-expansion
# snapshot rather than argued (rows with the project's measurements, beside the design spec).
# It makes the surviving pool *more* speculative, not less: the `Rumored` share rises
# 7.4% -> 8.5%. Of the ~670 cast-only films it drops, 70% are `In Production` or `Post
# Production` and 5% are `Rumored` — discarding roughly 470 films demonstrably shooting or cut
# to remove roughly 33 rumored ones, a 14:1 trade against NEU-1087's 1.5:1. (The counts are
# approximate on purpose: 665 is the *net* change in cast-only adds, 2,341 -> 1,676, while
# counting the individually dropped films gives 678. The shares, and the ratio that decides
# this, are unaffected.)
#
# The mechanism is that **billing depth proxies production maturity, not realness**: a film in
# post has a complete ordered cast so slots 4 and 5 are filled, while a rumored project has
# one or two names and never reaches a fourth billing. Cutting depth cuts the mature end.
#
# It would also degrade the *live* directors tranche, which is the part that makes this a
# one-way mistake rather than a tuning choice. Seed grade gates who is enumerated at all, so
# someone qualifying only through a 4th-billed role stops being a seed and undated films they
# later **direct** are never reached: director-reached falls 639 -> 597, 6.6% of a tranche
# whose own rule did not change. And because the credit-history writer shares this cut,
# 4th- and 5th-billed casting announcements would stop carding as `casting` events too.
#
# If cast-tranche volume needs controlling, use a lever that correlates with realness or
# newsworthiness — a popularity floor, or corroboration — not billing depth.
TOP_BILLED_ORDER = 5
# Strongest attachment first, so a rendered role list reads the way §3.2 lists the seed
# grades and groups stably. `crew` and `followed` are the two non-seed roles (D-49, D-50) and
# sit last: `crew` is any crew job that is neither directing nor writing, `followed` is the
# sweep's tranche for a non-seed credit held by a person somebody follows at `any`. Neither is
# a seed grade, and their position here says so.
ROLE_ORDER = ("director", "writer", "cast", "crew", "followed")


def crew_role(job: str | None) -> str | None:
    """The seed grade a crew job carries, or None when it carries none."""
    if job == DIRECTOR_JOB:
        return "director"
    if job in WRITER_JOBS:
        return "writer"
    return None


def credit_role(credit_type: str, job: str | None) -> str | None:
    """The seed-grade role one `(credit_type, job)` pair carries, or None when it carries none.

    `crew_role` widened to cover cast, which carries its grade in billing rather than in a job
    title. It lives here beside the predicate it reads for the reason the module exists: the
    sweep derives a role from a `film_credit_change` row, the short-circuit derives one from a
    live `film_credit` row, and a role the two spelled differently would be a credit one of
    them could not match to the other.
    """
    if credit_type == "cast":
        return "cast"
    if credit_type == "crew":
        return crew_role(job)
    return None


def recorded_role(credit_type: str, job: str | None) -> str:
    """The role one credit is *recorded* under (D-49) — `credit_role` made total.

    Seed grade is a cut; **recorded grade** is not. Once the credit history records every
    credit of a person somebody follows, a third-unit gaffer and a 40th-billed extra both
    reach the carding phases, and `credit_role` answers None for the gaffer — the value those
    phases use to mean "not a beat" and skip the row. So they read this instead: `cast` for any
    cast credit whatever its billing, `crew` for a crew job that is neither directing nor
    writing, and the seed-grade role otherwise.

    It is total, and that is the point: every row in `catalog.film_credit_change` is there
    because something chose to record it, so there is no longer such a thing as a recorded
    credit with no role. `credit_role` keeps its None for the callers that are still asking
    the *seed-grade* question. Totality rests on `credit_type` being one of two values, which
    `ingest.tmdb.upsert._upsert_credits` is the only writer of and spells literally; anything
    else reads as cast, the wider of the two.

    `crew` cards as `crew_attached`, beside director and writer
    (`news.catalog_events.CREDIT_ROLE_EVENT_TYPES`) — one beat, "somebody joined the crew",
    at a grade the body names.
    """
    role = credit_role(credit_type, job)
    if role is not None:
        return role
    return "crew" if credit_type == "crew" else "cast"


def role_match_key(role: str, job: str | None) -> tuple[str, str | None]:
    """The identity two recorded credits are the *same* credit under (D-49).

    A role alone used to be that identity, and for the seed grades it still is: `director`
    and `writer` each name the job that produced them, and `cast` deliberately ignores
    billing so a performer slipping from 2nd to 4th is not a detachment.

    `crew` is the one role that does not name its job — it is every job that is neither
    directing nor writing, folded together so they card as one beat — so it carries the job
    beside it here. Without that, "is this credit still there" would answer yes for a
    reverted `Gaffer` credit on the strength of an unrelated `Best Boy` one, which is exactly
    the edit the quarantine gate exists to suppress.

    Beside `recorded_role` rather than inside it because the two answer different questions:
    the role decides what a credit *cards as* (`CREDIT_ROLE_EVENT_TYPES`, the summary
    templates, `ROLE_ORDER`), and folding the job into it there would mint an event type per
    crew job.
    """
    return (role, job) if role == "crew" else (role, None)


def recorded_credit_key(credit_type: str, job: str | None) -> tuple[str, str | None]:
    """`role_match_key` from the two columns `catalog.film_credit` and `film_credit_change`
    both hold — the form every caller reading raw rows wants."""
    return role_match_key(recorded_role(credit_type, job), job)


def is_top_billed(credit_order: int | None) -> bool:
    """Whether a cast billing position is top-5. An unbilled entry (`order` absent) is not —
    TMDB leaves it off the long tail, which is exactly what the cut is meant to exclude."""
    return credit_order is not None and credit_order < TOP_BILLED_ORDER


def is_seed_grade(credit_type: str, job: str | None, credit_order: int | None) -> bool:
    """Whether one credit is seed grade, from the three fields that decide it.

    The single predicate both sides of the credit-history diff run — one over a TMDB payload,
    one over stored `catalog.film_credit` rows. Two encodings of this cut would be two
    definitions of seed grade, and a history recording a different one than the sweep
    enumerates is a history of nothing.
    """
    if credit_type == "cast":
        return is_top_billed(credit_order)
    if credit_type == "crew":
        return crew_role(job) is not None
    return False
