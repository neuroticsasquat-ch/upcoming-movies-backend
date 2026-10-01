"""split credit_removed into cast_removed and crew_removed (NEU-1518)

NR-12 of `docs/specs/bl-not-yet-reported-by-type-project-spec.md`. The sweep now cards a credit
detachment per role class (NR-9, NR-10), so every existing `credit_removed` card is migrated
onto the two new types and the old type is retired, in one revision so that no revision exists
where a `credit_removed` card can no longer be written or no longer be read:

1. `cast_removed` and `crew_removed` join `ck_event_type`.
2. Each `credit_removed` card is classed by the `catalog.film_credit_change` rows it was carded
   from. Not via `carded_by_event_id` — no removal card has ever set it — but by the key
   `group_detachments` grouped on: the card's `film_id`, `change = 'removed'`, `changed_at =
   occurred_at`, restricted to the people the card names (`normalize_name(person.name)` on its
   `subject_key`), because the prior-attachment gate drops people and an unfiltered read
   over-counts. Each row's class is `recorded_role(credit_type, job)`, `cast` or not.
   - **Single class:** retyped in place. Id, summary, timestamps and every reference stay.
   - **Mixed:** the original keeps its id and becomes the cast half; a new event is inserted as
     the crew half with the original's film, provenance, confidence, region, timestamps and
     status. Both halves' summaries are re-rendered from their class's credits with the sweep's
     own deterministic renderer, `model` and `prompt_version` — imported, not copied, so the
     wording cannot drift from what the sweep writes today (accepted in NR-12). The crew-class
     attachment cards the original superseded are re-pointed at the crew half, and the
     original's `app.notification` rows are copied to it with the same state, so a sent digest
     stays sent and a queued one carries both halves. An edited summary is re-rendered anyway
     and its event id logged.
3. `credit_removed` leaves `ck_event_type`.

A name on a card with no matching credit row **fails the migration**, naming the card: the
history table is the only record of who was which class, and parsing the summary instead would
break on a name with a period in it. Fix the data and re-run.

A mixed card whose unedited deterministic summary does not re-render from its matched rows
also fails, naming the card. The name filter cannot see the old flap gate, which dropped
credits per (person, role), so a person whose director credit flapped back while their cast
exit stuck would otherwise get a crew half saying they left a chair they still hold.

The downgrade is **lossy for split pairs.** It retypes both types back to `credit_removed`,
but `uq_event_catalog_change` refuses two `credit_removed` cards at one timestamp, so each
crew half sharing its (film, occurred_at) with a cast half is folded into that cast half first:
its references are re-pointed there, its names appended to the cast half's `subject_key`, and
it is deleted. The cast half's summary keeps only the cast wording; the crew half's summary is
lost.

Hand-written: Alembic's autogenerate does not emit a change to an existing table's
`CheckConstraint`, and `tests/integration/test_migrations.py` compares `ck_event_type` with the
model by name and definition.

Revision ID: feb127488dae
Revises: b3e1c5a7d905
Create Date: 2026-10-01 22:00:00.000000+00:00

"""

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Connection

from upmovies.catalog.seed_grade import recorded_role
from upmovies.news.subject_key import normalize_name
from upmovies.synthesize.deterministic import (
    DETERMINISTIC_MODEL,
    TEMPLATE_VERSION,
    CreditDetached,
    CreditsDetached,
    render_summary,
)

revision = "feb127488dae"
down_revision = "b3e1c5a7d905"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_HEAD = "'announced', 'canceled', "
_TAIL = (
    "'now_available', 'production_start', 'production_wrap', 'release_date', "
    "'trailer', 'first_look', 'other'"
)
_OLD = (
    _HEAD + "'casting', 'collection_attached', 'collection_removed', "
    "'company_attached', 'company_removed', 'credit_removed', 'crew_attached', " + _TAIL
)
_BOTH = (
    _HEAD + "'cast_removed', 'casting', 'collection_attached', 'collection_removed', "
    "'company_attached', 'company_removed', 'credit_removed', 'crew_attached', "
    "'crew_removed', " + _TAIL
)
_NEW = (
    _HEAD + "'cast_removed', 'casting', 'collection_attached', 'collection_removed', "
    "'company_attached', 'company_removed', 'crew_attached', 'crew_removed', " + _TAIL
)

_CAST = "cast_removed"
_CREW = "crew_removed"

_SUBJECT_KEY = sa.bindparam("subject_key", type_=ARRAY(sa.Text))

# Every reference to a `news.event` row the downgrade can move without a key clash, as
# (table, column). `app.notification` is the exception, handled on its own: its unique
# `(user_id, event_id, kind, channel)` can clash when one user was queued for both halves.
# `news.event_summary` is not moved — the crew half's summary is the downgrade's loss.
_EVENT_REFERENCES = (
    ("news.event", "superseded_by"),
    ("news.event_story", "event_id"),
    ("catalog.film_credit_change", "carded_by_event_id"),
    ("catalog.film_field_change", "carded_by_event_id"),
    ("catalog.film_company_change", "carded_by_event_id"),
)


def _set_constraint(values: str) -> None:
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(
        f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({values}))"
    )


def _classed_credits(conn: Connection, card: sa.Row) -> list[tuple[str, CreditDetached]]:
    """The `(normalized name, credit)` pairs the card was carded from, oldest row first — the
    order `load_detachment_backlog` reads them in, so a re-rendered body names people in the
    order the sweep would have. Raises naming the card if any name on it has no row."""
    rows = conn.execute(
        sa.text(
            "SELECT p.name, c.credit_type, c.job "
            "FROM catalog.film_credit_change c JOIN catalog.person p ON p.id = c.person_id "
            "WHERE c.film_id = :film_id AND c.change = 'removed' "
            "AND c.changed_at = :occurred_at ORDER BY c.id"
        ),
        {"film_id": card.film_id, "occurred_at": card.occurred_at},
    ).all()
    names = list(card.subject_key or [])
    credits = [
        (
            normalize_name(row.name),
            CreditDetached(role=recorded_role(row.credit_type, row.job), name=row.name),
        )
        for row in rows
        if normalize_name(row.name) in names
    ]
    matched = {norm for norm, _ in credits}
    missing = [name for name in names if name not in matched]
    if not names or missing:
        raise RuntimeError(
            f"credit_removed card {card.id} (film {card.film_id}, occurred_at "
            f"{card.occurred_at.isoformat()}) names {missing or 'nobody'} with no "
            "catalog.film_credit_change row at (film_id, 'removed', changed_at = occurred_at); "
            "fix the data and re-run the migration"
        )
    return credits


def _render(credits: list[CreditDetached]) -> str:
    return render_summary(CreditsDetached(credits=tuple(credits)))


def _split(conn: Connection, card: sa.Row, credits: list[tuple[str, CreditDetached]]) -> None:
    """Split a mixed card: the original becomes the cast half, a new event the crew half."""
    cast = [credit for _, credit in credits if credit.role == "cast"]
    crew = [credit for _, credit in credits if credit.role != "cast"]
    cast_names = {norm for norm, credit in credits if credit.role == "cast"}
    crew_names = {norm for norm, credit in credits if credit.role != "cast"}
    names = list(card.subject_key)

    summary = conn.execute(
        sa.text(
            "SELECT summary, model, source_updated_at, edited_at FROM news.event_summary "
            "WHERE event_id = :id"
        ),
        {"id": card.id},
    ).one_or_none()
    # The name filter cannot see the old flap gate, which dropped credits per (person, role):
    # an actor-director whose director credit flapped back while their cast exit stuck is on
    # the card, so both rows match and the card reads as mixed. The body the sweep wrote is the
    # record of which credits it actually carded, so a split that would re-render something
    # the card never said stops here, naming the card, on the same terms as a missing name.
    if (
        summary is not None
        and summary.model == DETERMINISTIC_MODEL
        and summary.edited_at is None
        and summary.summary != _render([credit for _, credit in credits])
    ):
        raise RuntimeError(
            f"credit_removed card {card.id} reads as mixed from its credit rows, but its "
            f"summary {summary.summary!r} does not render from them; a flapped credit may "
            "share a name with a real departure. Fix the data and re-run the migration"
        )
    if summary is not None and summary.edited_at is not None:
        log.warning(
            "credit_removed card %s had an edited summary; re-rendered from its credits", card.id
        )
    source_updated_at = summary.source_updated_at if summary is not None else card.updated_at

    conn.execute(
        sa.text(
            "UPDATE news.event SET event_type = :event_type, subject_key = :subject_key "
            "WHERE id = :id"
        ).bindparams(_SUBJECT_KEY),
        {"event_type": _CAST, "subject_key": [n for n in names if n in cast_names], "id": card.id},
    )
    crew_id = conn.execute(
        sa.text(
            "INSERT INTO news.event (film_id, event_type, confidence, provenance, status, "
            "region, subject_key, occurred_at, created_at, updated_at) VALUES (:film_id, "
            ":event_type, :confidence, :provenance, :status, :region, :subject_key, "
            ":occurred_at, :created_at, :updated_at) RETURNING id"
        ).bindparams(_SUBJECT_KEY),
        {
            "film_id": card.film_id,
            "event_type": _CREW,
            "confidence": card.confidence,
            "provenance": card.provenance,
            "status": card.status,
            "region": card.region,
            "subject_key": [n for n in names if n in crew_names],
            "occurred_at": card.occurred_at,
            "created_at": card.created_at,
            "updated_at": card.updated_at,
        },
    ).scalar_one()

    write_summary = sa.text(
        "INSERT INTO news.event_summary (event_id, summary, model, prompt_version, "
        "source_updated_at) VALUES (:event_id, :summary, :model, :prompt_version, "
        ":source_updated_at) ON CONFLICT (event_id) DO UPDATE SET summary = excluded.summary, "
        "model = excluded.model, prompt_version = excluded.prompt_version, "
        "source_updated_at = excluded.source_updated_at, generated_at = now(), "
        "edited_at = NULL, edited_by = NULL"
    )
    for event_id, half in ((card.id, cast), (crew_id, crew)):
        conn.execute(
            write_summary,
            {
                "event_id": event_id,
                "summary": _render(half),
                "model": DETERMINISTIC_MODEL,
                "prompt_version": TEMPLATE_VERSION,
                "source_updated_at": source_updated_at,
            },
        )

    conn.execute(
        sa.text(
            "UPDATE news.event SET superseded_by = :crew_id "
            "WHERE superseded_by = :id AND event_type = 'crew_attached'"
        ),
        {"crew_id": crew_id, "id": card.id},
    )
    conn.execute(
        sa.text(
            "INSERT INTO app.notification "
            "(user_id, event_id, kind, channel, status, created_at, sent_at, error) "
            "SELECT user_id, :crew_id, kind, channel, status, created_at, sent_at, error "
            "FROM app.notification WHERE event_id = :id"
        ),
        {"crew_id": crew_id, "id": card.id},
    )


def upgrade() -> None:
    _set_constraint(_BOTH)
    conn = op.get_bind()
    cards = conn.execute(
        sa.text(
            "SELECT id, film_id, confidence, provenance, status, region, subject_key, "
            "occurred_at, created_at, updated_at FROM news.event "
            "WHERE event_type = 'credit_removed' ORDER BY occurred_at, id"
        )
    ).all()
    split = 0
    for card in cards:
        credits = _classed_credits(conn, card)
        roles = {credit.role == "cast" for _, credit in credits}
        if roles == {True, False}:
            _split(conn, card, credits)
            split += 1
            continue
        conn.execute(
            sa.text("UPDATE news.event SET event_type = :event_type WHERE id = :id"),
            {"event_type": _CAST if roles == {True} else _CREW, "id": card.id},
        )
    log.info("credit_removed: %d cards migrated, %d of them split", len(cards), split)
    _set_constraint(_NEW)


def downgrade() -> None:
    _set_constraint(_BOTH)
    conn = op.get_bind()
    pairs = conn.execute(
        sa.text(
            "SELECT c.id AS cast_id, c.subject_key AS cast_names, w.id AS crew_id, "
            "w.subject_key AS crew_names FROM news.event w JOIN news.event c "
            "ON c.film_id = w.film_id AND c.occurred_at = w.occurred_at "
            "AND c.event_type = 'cast_removed' AND c.provenance = 'catalog' "
            "WHERE w.event_type = 'crew_removed' AND w.provenance = 'catalog'"
        )
    ).all()
    for pair in pairs:
        ids = {"cast_id": pair.cast_id, "crew_id": pair.crew_id}
        for table, column in _EVENT_REFERENCES:
            conn.execute(
                sa.text(f"UPDATE {table} SET {column} = :cast_id WHERE {column} = :crew_id"), ids
            )
        # A user queued for both halves keeps the cast half's row; the crew half's goes with it.
        conn.execute(
            sa.text(
                "UPDATE app.notification n SET event_id = :cast_id WHERE n.event_id = :crew_id "
                "AND NOT EXISTS (SELECT 1 FROM app.notification o WHERE o.event_id = :cast_id "
                "AND o.user_id = n.user_id AND o.kind = n.kind AND o.channel = n.channel)"
            ),
            ids,
        )
        cast_names = list(pair.cast_names or [])
        merged = cast_names + [n for n in pair.crew_names or [] if n not in cast_names]
        conn.execute(
            sa.text(
                "UPDATE news.event SET subject_key = :subject_key WHERE id = :cast_id"
            ).bindparams(_SUBJECT_KEY),
            {"subject_key": merged, "cast_id": pair.cast_id},
        )
        conn.execute(sa.text("DELETE FROM news.event WHERE id = :crew_id"), ids)
    op.execute(
        "UPDATE news.event SET event_type = 'credit_removed' "
        "WHERE event_type IN ('cast_removed', 'crew_removed')"
    )
    _set_constraint(_OLD)
