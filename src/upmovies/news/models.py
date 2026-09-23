from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from upmovies.db import Base


class Story(Base):
    """Raw ingested news item. `film_id` is the entity-linking attach point that
    Entity-Linking & Clustering populates later (nullable until linked)."""

    __tablename__ = "story"
    __table_args__ = (
        Index("ix_story_film_id", "film_id"),
        Index("ix_story_link_status", "link_status"),
        CheckConstraint(
            "link_status IN ('pending', 'linked', 'rejected')",
            name="ck_story_link_status",
        ),
        CheckConstraint(
            "resolve_state IN ('none', 'pending', 'resolved', 'failed')",
            name="ck_story_resolve_state",
        ),
        {"schema": "news"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    outlet: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    film_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("catalog.film.id", ondelete="SET NULL"), nullable=True
    )
    link_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    link_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    link_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    resolved_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolve_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'none'"))
    resolve_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# Where an event came from. `story` is the ADR-0002 path — a trade story reported it and TMDB's
# change history corroborated it. `catalog` is the ADR-0014 path — a TMDB field or credit change
# created the event with no story behind it, so it carries a deterministic summary and attributes
# to TMDB rather than to outlets. Stories may still cluster onto a `catalog` event later; the
# provenance records how it was *born* and does not change when they do.
class Event(Base):
    """A distinct per-film news event (a real beat: casting, trailer, release-date change,
    production milestone, …), grouping the stories that report it. The contract Synthesis
    writes summaries against — no summary column lives here."""

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('announced', 'canceled', 'casting', 'collection_attached', "
            "'collection_removed', 'company_attached', 'company_removed', "
            "'credit_removed', 'crew_attached', "
            "'now_available', 'production_start', 'production_wrap', 'release_date', "
            "'trailer', 'first_look', 'other')",
            name="ck_event_type",
        ),
        CheckConstraint("confidence IN ('confirmed', 'rumored')", name="ck_event_confidence"),
        CheckConstraint(
            "provenance IN ('story', 'catalog')",
            name="ck_event_provenance",
        ),
        CheckConstraint(
            "status IN ('published', 'superseded')",
            name="ck_event_status",
        ),
        Index("ix_event_film_id", "film_id"),
        Index("ix_event_superseded_by", "superseded_by"),
        # One catalog change, one event — structurally, not by convention. The field-change
        # reader sets `occurred_at` to the change's own `changed_at`, so this triple is that
        # change's natural key, and the reader re-reads a rolling window of changes every run
        # (ADR-0014). Its own skip check is the fast path; this is the backstop that makes a
        # double card impossible rather than merely unlikely. Partial, so the story path —
        # where two events on one film may legitimately share a timestamp — is untouched.
        Index(
            "uq_event_catalog_change",
            "film_id",
            "event_type",
            "occurred_at",
            unique=True,
            postgresql_where=text("provenance = 'catalog'"),
        ),
        {"schema": "news"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("catalog.film.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'story'"))
    # Publication state, not visibility: a `superseded` card is still rendered everywhere a
    # `published` one is, marked and linked to the event that corrected it (ADR-0017, D-2).
    # Nothing is ever hidden or deleted by supersession.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'published'"))
    # The event that supersedes this one — an attachment card pointing forward at the
    # `credit_removed` card that corrected it. SET NULL rather than CASCADE: losing the
    # correction must not take the original claim out of the ledger with it.
    # `name=` matches the hand-named constraint in migration 65ba376f1b57; the parity test
    # (tests/integration/test_migrations.py) compares constraint names, so the two must agree.
    superseded_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("news.event.id", ondelete="SET NULL", name="fk_event_superseded_by_event"),
        nullable=True,
    )
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_key: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EventStory(Base):
    """Join row: which stories belong to an event. The unique `story_id` enforces the
    one-event-per-story rule (a story attaches to its single dominant beat)."""

    __tablename__ = "event_story"
    __table_args__ = (
        UniqueConstraint("story_id", name="uq_event_story_story_id"),
        {"schema": "news"},
    )

    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("news.event.id", ondelete="CASCADE"), primary_key=True
    )
    story_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("news.story.id", ondelete="CASCADE"), primary_key=True
    )


class EventSummary(Base):
    """AI-generated summary of an event. One summary per event (event_id is the PK)."""

    __tablename__ = "event_summary"
    __table_args__ = ({"schema": "news"},)

    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("news.event.id", ondelete="CASCADE"),
        primary_key=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Human-edit marker: NULL edited_at = machine-generated; non-NULL = an admin edited the text
    # (edited_by is the editing user, nulled if that account is deleted). Barely a selection
    # guard — write-once (_select_pending) is what keeps summaries frozen, and edited_at only
    # narrows the one case write-once does not cover (a deterministic body being superseded,
    # ADR-0014). Mainly these drive the summary_edited DTO flag and gate the reset-to-AI action.
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `name=` matches migration 8db18de6d77c, for the same reason as `Event.superseded_by`.
    edited_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="SET NULL", name="fk_event_summary_edited_by_user"),
        nullable=True,
    )


class SourceDomain(Base):
    """Per-publisher-domain trust tier for the source-quality gate. `llm_tier` is assigned
    once by the LLM judge on first sighting and cached; `admin_override` (set from the admin
    UI) always wins over it. Keyed by the normalized registrable domain (e.g. `mshale.com`)."""

    __tablename__ = "source_domain"
    __table_args__ = (
        CheckConstraint(
            "llm_tier IS NULL OR llm_tier IN ('trusted', 'acceptable', 'low')",
            name="ck_source_domain_tier",
        ),
        CheckConstraint(
            "admin_override IN ('none', 'block', 'allow', 'trust')",
            name="ck_source_domain_override",
        ),
        {"schema": "news"},
    )

    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    llm_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_override: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'none'"))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    judged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class StoryPerson(Base):
    """One person the cluster stage's extraction pass found named in a story — the raw mention,
    before anything has decided who it is (D-20, D-24).

    Written at clustering from the model's own tuples, which carry names only and never ids
    (INV-5): whatever resolution eventually decides, it decides in Python from these rows.
    Until M4's resolver lands, every row arrives with `person_id`, `confidence`, `path` and
    `resolved_at` NULL — an unresolved mention, which is a valid resting state and not a
    failure. `person_id` NULL with `path='not_in_tmdb'` stays valid afterwards (INV-8).

    `features` and `candidates` are the resolver's working notes, kept so a decision can be
    read back rather than re-derived (D-25's `/admin/resolution` page reads them). `features`
    starts out holding the extraction-time context the model reported alongside the mention —
    `title_mentioned` (another film the article names) and `event_type` (the beat the person is
    named in connection with) — neither of which is a column of its own, and both of which the
    scorer needs: filmography overlap with other titles named in the article is one of D-21's
    features.

    **The resolver merges into `features`; it must not replace it.** Those two keys are the
    only record of what the extraction pass saw, written at clustering and never regenerated —
    the story is clustered by then, so no later run re-reads it. A resolver that assigns a
    fresh dict of computed scores destroys the very inputs D-21 says to score on.

    `prompt_version` is the version of the cluster instructions that produced the mention, for
    the same reason `event_summary` carries one: an extraction prompt that changes meaning has
    to be distinguishable from the rows written before it, or a re-extraction cannot tell what
    it is replacing."""

    __tablename__ = "story_person"
    __table_args__ = (
        CheckConstraint(
            "path IS NULL OR path IN ('accepted', 'tiebreak', 'unlinked', 'not_in_tmdb')",
            name="ck_story_person_path",
        ),
        Index("ix_story_person_story_id", "story_id"),
        Index("ix_story_person_person_id", "person_id"),
        {"schema": "news"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    story_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("news.story.id", ondelete="CASCADE"), nullable=False
    )
    # SET NULL rather than CASCADE: TMDB deleting a person must not delete the record that a
    # trade story named them — the mention is ours, the person row is TMDB's (cf. `Person.
    # tmdb_missing_at`). `name=` is carried into the migration, which the parity test compares.
    person_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("catalog.person.id", ondelete="SET NULL", name="fk_story_person_person"),
        nullable=True,
    )
    name_as_written: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    department: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    candidates: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# The two `story_person.path` values that name somebody a follow can match (D-25) — beside the
# constraint that spells the whole vocabulary, rather than in the resolver that writes it, so the
# timeline's filter (`app.follow_queries.first_association_clause`) can read the rule without
# importing the scoring pass and, with it, the TMDB client onto a public read path.
#
# A resolved mention no longer puts its event on a follower's timeline by itself (EF-3): it does
# so only as the mentioned entity's first association with the film, or its first detachment
# from it (EF-13). The paths are still the cut that decides whether a mention names anybody at
# all.
RESOLVED_MENTION_PATHS = ("accepted", "tiebreak")


PERSON_KIND = "person"
"""The `kind` a person mention is keyed under in `news.resolution_cache` and filtered by on
`/admin/resolution`. People have no `kind` column of their own — they have their own table —
so this is the one place the word is spelled (EF-12)."""

ORGANISATION_KINDS = ("company", "collection")
"""The two `news.story_entity.kind` values (EF-12) — catalog spelling, not follow spelling.

`collection` is what TMDB and `catalog.collection` call a franchise, and it stays that way on
this side of the line: these rows record what the extraction pass read out of an article
against the catalog it is resolved into. The mapping to a follow's `entity_type` (`franchise`)
belongs to the query builder that joins the two (EF-13, NEU-1446), not to the extraction
vocabulary.

`person` is deliberately absent: people are `story_person`, which keeps its own table and its
person-specific `role` / `department` columns.
"""


class StoryEntity(Base):
    """One organisation — a studio or a franchise — the cluster stage's extraction pass found
    named in a story (EF-12). `story_person`'s shape, for the two kinds that are not people.

    A separate table rather than a `kind` column on `story_person`, because that table's
    `role`, `department` and FK-backed `person_id` are person facts a company row would carry
    as three permanent NULLs and a lie about what `entity_id` references.

    Written at clustering from the model's own tuples, which carry names only and never ids
    (INV-5). Every row arrives unresolved — `entity_id`, `confidence`, `path` and `resolved_at`
    NULL — and is picked up by `link.resolve.org_pipeline` on a later pass. `entity_id` NULL
    with `path='not_in_tmdb'` stays valid afterwards (INV-8), and INV-6 caps `confidence` at
    the story's own link confidence.

    `features` carries the extraction-time context that has no column of its own —
    `title_mentioned` and `event_type` — and the resolver **merges** its own working notes into
    it under a `resolution` key rather than replacing it, for the reason spelled out in full on
    `StoryPerson`. `features->>'event_type'` is where NEU-1446's first-association predicate
    reads the beat this organisation was named in connection with, exactly as it does for a
    person.

    **`entity_id` carries no foreign key**, unlike `story_person.person_id`: it points at
    `catalog.production_company` or `catalog.collection` depending on `kind`, and one column
    cannot reference two tables. Neither of those is ever deleted by ingest — both are upserted
    reference data — so the `ON DELETE SET NULL` the person side needs has nothing to guard
    against here.
    """

    __tablename__ = "story_entity"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('company', 'collection')",
            name="ck_story_entity_kind",
        ),
        CheckConstraint(
            "path IS NULL OR path IN ('accepted', 'tiebreak', 'unlinked', 'not_in_tmdb')",
            name="ck_story_entity_path",
        ),
        Index("ix_story_entity_story_id", "story_id"),
        Index("ix_story_entity_kind_entity_id", "kind", "entity_id"),
        {"schema": "news"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    story_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("news.story.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name_as_written: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    candidates: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ResolutionCache(Base):
    """A name-to-entity decision, remembered per (source domain, name as written, film, kind)
    so the same trade naming the same person or studio on the same film is not re-resolved
    every run (D-24, EF-12).

    The quadruple is the primary key, which is the unique key the cache needs: the same name
    means different people on different films, one outlet's house style for a name ("Chris
    Evans" vs "Christopher Evans") is not another's, and a name can be both a person and a
    studio ("Blumhouse" is not, but "A24" and "Amblin" name people too). A cached row with no
    id is a cached *negative* — nobody in TMDB matches — and is as much an answer as a hit is
    (INV-8).

    **Two id columns, because only one kind has a table to reference.** `person_id` keeps its
    FK into `catalog.person` with `ON DELETE SET NULL`, which is load-bearing: TMDB tombstones
    people, and a cached id surviving that deletion would be handed to `story_person.person_id`
    — which *does* carry the FK — and fail the write. `entity_id` is the plain column the two
    organisation kinds use; it points at `catalog.production_company` or `catalog.collection`
    by `kind`, one column cannot reference two tables, and neither of those is ever deleted by
    ingest. Exactly one of the two is ever set, and `ck_resolution_cache_id_matches_kind` is
    what makes that true rather than merely intended."""

    __tablename__ = "resolution_cache"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('person', 'company', 'collection')",
            name="ck_resolution_cache_kind",
        ),
        # The two id columns are exclusive by `kind`, structurally rather than by convention:
        # the docstring above says which column a row's kind means, and without this a person
        # row could carry an `entity_id` nothing would ever read back.
        CheckConstraint(
            "CASE WHEN kind = 'person' THEN entity_id IS NULL ELSE person_id IS NULL END",
            name="ck_resolution_cache_id_matches_kind",
        ),
        {"schema": "news"},
    )

    source_domain: Mapped[str] = mapped_column(Text, primary_key=True)
    name_as_written: Mapped[str] = mapped_column(Text, primary_key=True)
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Last in the key and server-defaulted to `person`, so every row written before EF-12
    # keeps the meaning it was written with and the person pass reads its own cache unchanged.
    kind: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("'person'"))
    person_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("catalog.person.id", ondelete="SET NULL", name="fk_resolution_cache_person"),
        nullable=True,
    )
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
