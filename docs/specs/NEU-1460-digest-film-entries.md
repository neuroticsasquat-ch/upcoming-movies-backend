# NEU-1460 — Digest film entries: ranked, attributed, dated, sourced, capped

**Status:** approved design (planit session with Tom, 2026-09-24)
**Ticket:** https://linear.app/neuroticsasquatch/issue/NEU-1460 (story NEU-1454, milestone M1)
**Project spec:** `docs/specs/bl-digest-content-project-spec.md` — DC-3 to DC-8, DC-10 (preheader),
DC-12, DC-16, DC-17, §6 M1. This file resolves what that spec leaves open for this ticket and
records the ground truth the implementation must match. Where the two disagree, this file wins;
where this file is silent, the project spec governs.
**Blocked by:** NEU-1459 (`follow_queries.follow_attribution_pairs`). Still in Backlog at the
time of writing — **do not start this ticket until NEU-1459 is merged**; the loader below reads
that builder and nothing here re-implements it.
**Blocks:** NEU-1461 (visual pass), NEU-1462 (slate day + markers), NEU-1463 (`Envelope.headers`).
**Glossary:** `CONTEXT.md` — **Digest**, **Film entry**, **Lead film**, **Slate**, **Slate day**.
Use those words in code, tests and the PR.

---

## 1. What to build and why

The digest sender (`app/services/digest_sender.py`) mails a user's `queued` digest rows grouped
the way the feed pages: by publication day, then film, then event. That shape repeats a film
under every day it had a beat, carries no date, confidence, source or attribution on a beat
line, and picks the subject by counting rows. This ticket replaces the sender's **content
model** with film entries — one entry per film, ranked by the most significant beat, each beat
dated, marked when rumored and sourced, with the follows that put the film in the mail named
and linked — and extracts the render into one function (`render_digest`) that M3's preview and
test-send will call. The **delivery** behaviour (recipients, gate, marking, abort guard,
heartbeat, detail line) is untouched, and the templates keep the current visual style: the
look is NEU-1461's.

Three decisions taken in this session, beyond the project spec:

- **D-1460.1 The gateway gains `deliver(envelope)`.** `send_batch` and `render_digest` share
  one render path in the strict sense: both produce an `Envelope` through the same function,
  and `send_batch` hands that `Envelope` to a new `Mailer.deliver(envelope) -> MessageId`.
  M2 (NEU-1463) then sets `List-Unsubscribe` on the `Envelope` itself, and M3 prefixes a
  test-send's subject with `dataclasses.replace` — neither needs a second render. This
  supersedes the M2 contract line "`Mailer.send(..., headers=)` passes them through": the
  header rides on the `Envelope` and `deliver` carries it; `Mailer.send` keeps its signature
  for the transactional mails. Record this in NEU-1463's PR.
- **D-1460.2 The status line is the theatrical headline release, in bucket form.**
  `catalog.headline_release.headline_releases` is theatrical-only by design, so the project
  spec's "Digital release · 8 October 2026" example is unreachable and is dropped. Forms:
  `upcoming` and `released` → `"{Bucket} release · {D Month YYYY}"` ("Wide release · 14 August
  2026"), tense-free; `primary` → `"{D Month YYYY} (unconfirmed)"`, mirroring the frontend row;
  no headline release → the arc-stage label.
- **D-1460.3 Arc-stage labels are the frontend's four.** `Announced` / `Shooting` / `Wrapped` /
  `Released`, exactly `components/film/labels.ts::ARC_STAGE_LABELS`, unknown stage → `Announced`.
  Used both as the parenthetical's fallback (so the frontend fixtures pass verbatim) and as the
  undated status line. The spec's "In production" was illustrative.

## 2. Ground truth (read before coding)

- `public/arc.py`: `most_significant_event_type(types)` and `event_stage_rank(type)` — the
  ranking. `derive_arc_stage(film.status)` returns only `announced | shooting | wrapped |
  released`.
- `public/service.py`: `_directors_for_films(session, ids) -> {film_id: [name, …]}` (billing
  order), `_production_countries_for_films(session, ids) -> {film_id: [display name, …]}`
  (sorted), `_release_year(date | None)`, `_sources_by_event(session, ids) -> {event_id:
  [Story, …]}` (published_at asc, nulls last, then id) and `cap_sources(stories)` (newest
  distinct outlets first, cap 3). **The first source** in `EventOut.sources` order is
  `cap_sources(_sources_by_event(...)[event_id])[0]`; its name is `outlet_label(story)` and its
  link `source_url(story)` (`public/sources.py`). These three private helpers are imported
  from `public.service` as-is; do not copy them.
- `catalog/headline_release.py`: `headline_releases(session, film_ids, today=) -> {film_id:
  HeadlineRelease(date, kind, country, bucket)}`; films with nothing are absent.
- `public/release.py::RELEASE_BUCKET_LABELS` — "Wide", "Limited", "Digital", "Physical";
  `digest_sender.release_label(release_type)` already spells "Wide release".
- `catalog/ref.py`: `person_ref`, `company_ref`, `collection_ref` → frontend routes
  `/person/:ref`, `/studio/:ref`, `/franchise/:ref`. Follow `entity_type` vocabulary is
  `person | company | franchise | title` (`app.models.FOLLOW_ENTITY_TYPES`); `franchise` rows
  resolve through `catalog.Collection`, `company` through `catalog.ProductionCompany`, `person`
  through `catalog.Person`. `entity_id` arrives as **text** from the pairs builder — cast to
  int for the three catalog tables.
- `follow_queries.follow_attribution_pairs(user_id) -> Select[(entity_type, entity_id,
  event_id)]` (NEU-1459): entity branches + a `('title', film_id, event_id)` arm.
- `mail/`: `templates.render(name, context, *, sender, to) -> Envelope`; `Envelope(sender, to,
  subject, text, html)`, frozen; `Mailer` Protocol = `send(to, template, context)`;
  `Transport.send(envelope)`; `NoopTransport.sent` keeps envelopes. `StrictUndefined`,
  autoescape on `.html` only, `trim_blocks`/`lstrip_blocks` on.
- Frontend `lib/format.ts::filmParenthetical` and its nine fixtures in `lib/format.test.ts`
  (`describe("filmParenthetical")`): countries "/"-joined cap 3 with " +N", `Dir: ` names
  "/"-joined cap 2 with " +N", year, parts joined with ", ", arc-stage label when all three are
  absent. Country strings are display names ("USA", "UK", "South Korea").
- Existing sender facts that stay: `load_recipients` (COALESCEd cadence, weekly `owed`
  clause), the gate (`deliverable`), `mark()` outcomes, `unsendable` reasons, `load_slate`,
  `SLATE_WINDOW_DAYS`, `DIGEST_BEAT_LABELS` / `digest_beat_label`, `day_heading`,
  `release_label`, `film_url` / `poster_url` (`w154`) / `settings_url` from `alert_sender`.

## 3. Sender model (`app/services/digest_sender.py`)

Remove `DigestEvent`, `DigestFilm`, `DigestDay`, `_FilmDay`, `_event_key`, `load_timeline`.
Add:

```python
DIGEST_MAX_ENTRIES = 20

@dataclass(frozen=True)
class DigestSource:
    name: str            # outlet_label(story) for a story card; "TMDB" for a catalog card
    url: str | None      # source_url(story); None for catalog cards ("via TMDB", unlinked)

@dataclass(frozen=True)
class DigestBeat:
    notification_id: UUID
    event_type: str
    day: date            # event.created_at in UTC — the publication day (ADR-0016)
    label: str           # digest_beat_label(event_type)
    confidence: str      # "confirmed" | "rumored"
    summary: str
    source: DigestSource | None

@dataclass(frozen=True)
class DigestFollowing:
    entity_type: str     # person | company | franchise | title
    name: str
    url: str | None      # None for the title row (the entry's own film link is the link)

@dataclass(frozen=True)
class DigestFilm:        # the header's inputs, resolved
    film_id: UUID
    tmdb_id: int
    title: str
    film_url: str
    poster_url: str | None

@dataclass(frozen=True)
class DigestHeader:
    parenthetical: str   # film_parenthetical(...)
    status: str          # status_line(...)

@dataclass(frozen=True)
class DigestEntry:
    film: DigestFilm
    header: DigestHeader
    following: tuple[DigestFollowing, ...]   # every attribution row incl. the title row
    beats: tuple[DigestBeat, ...]            # publication order
    rank_key: tuple[int, str, int]           # (-event_stage_rank(lead type), title.casefold(), tmdb_id)

    @property
    def lead_type(self) -> str: ...          # most_significant_event_type over beats
    @property
    def lead_label(self) -> str: ...         # digest_beat_label(lead_type)
    @property
    def entity_following(self) -> tuple[DigestFollowing, ...]: ...  # entity_type != "title"
    @property
    def credits_justwatch(self) -> bool: ... # any beat.event_type == "now_available"
```

`DigestBatch` becomes:

```python
@dataclass(frozen=True)
class DigestBatch:
    recipient: DigestRecipient
    entries: tuple[DigestEntry, ...]         # ranked, UNCAPPED
    unsendable: tuple[tuple[UUID, str], ...]
    slate: tuple[SlateDay, ...] = ()

    @property
    def rendered_entries(self) -> tuple[DigestEntry, ...]: return self.entries[:DIGEST_MAX_ENTRIES]
    @property
    def overflow(self) -> int: return max(0, len(self.entries) - DIGEST_MAX_ENTRIES)
    @property
    def item_ids(self) -> list[UUID]: ...    # every beat's notification id, capped or not
    @property
    def slate_count(self) -> int: ...        # unchanged
    @property
    def has_content(self) -> bool: ...       # bool(entries) or bool(slate)
```

`SlateItem` / `SlateDay` / `load_slate` are **unchanged** in this ticket (NEU-1462 renames to
`SlateRow` and adds `marker`). `DigestRecipient`, `DigestSendResult`, `DigestOutcome`,
`load_recipients`, `send_digests`, `digest_detail` are unchanged.

### Pure functions (each with its own unit test)

- `rank_entries(entries: Iterable[DigestEntry]) -> tuple[DigestEntry, ...]` — sort by
  `rank_key`: most significant lead beat first (`event_stage_rank` descending), then casefolded
  title, then `tmdb_id`. Pure; the unit test feeds hand-built entries and asserts order,
  including a tie on stage broken by title and a tie on title broken by `tmdb_id`, and an
  `other`/`first_look`-only entry ranking last.
- `beat_order_key(beat) -> (created_at, occurred_at, str(event_id))` — beats within an entry
  in **publication order**: `created_at`, then `occurred_at`, then id. (The current sender puts
  `occurred_at` first; the ticket flips it.)
- `film_parenthetical(*, production_countries, directors, release_year, arc_stage) -> str` —
  mirrors `filmParenthetical` exactly. Unit test ports **all nine** frontend fixtures verbatim
  (including the 14-director "+12" and the nine-country "+6" cases).
- `arc_stage_label(stage: str) -> str` — `ARC_STAGE_LABELS` mirror (D-1460.3).
- `status_line(release: HeadlineRelease | None, arc_stage: str) -> str` — D-1460.2. Date is
  `long_date(d)` = `"{d.day} {Month} {d.year}"` ("14 August 2026", no ordinal, no zero-pad).
- `short_date(d: date, *, today: date) -> str` — `"22 Sep"`, `"22 Sep 2025"` when
  `d.year != today.year`. Month abbreviations are the first three letters of `_MONTHS`
  ("Sep", not "Sept"). "Current year" is the run's `today`, never the wall clock, so tests
  are deterministic.
- `digest_subject(batch) -> str` (DC-7) and `digest_preheader(batch) -> str` (DC-10) — see §5.

## 4. Loader: `load_entries(session, *, user_id, today, settings) -> (entries, unsendable)`

One query for the rows, three batched lookups for the header inputs, one for the sources,
one for the attribution. Nothing per-entry in a loop.

1. **Rows.** As `load_timeline` selects today plus `Event.confidence`, `Event.provenance`,
   `Film.status`, `Film.release_date`. Same `unsendable` rules: not `published` → "the event is
   no longer published"; no summary → "the event has no summary". Group sendable rows by
   `film_id`.
2. **Header inputs**, for the set of film ids: `_directors_for_films`,
   `_production_countries_for_films`, `headline_releases(session, ids, today=today)`;
   `release_year = _release_year(film.release_date)`; `arc_stage = derive_arc_stage(film.status)`.
3. **Sources**, for the set of sendable event ids: `_sources_by_event`; per event
   `cap_sources(stories)[0]` if any → `DigestSource(outlet_label, source_url)`. A `catalog`
   provenance event → `DigestSource("TMDB", None)`. A `story` event with no story rows →
   `source=None` (no source line; do not invent "via TMDB" for it).
4. **Attribution.** `pairs = follow_attribution_pairs(user_id)` filtered to
   `event_id IN (sendable event ids)` (wrap as a subquery/CTE and `WHERE event_id IN (...)`).
   Collect distinct `(entity_type, entity_id)` per film across all its beats. Resolve names:
   `Person.name` / `ProductionCompany.name` / `Collection.name` by int id; the `title` row
   resolves to the film's own title with `url=None`. URLs: `{base}/person/{person_ref(id,
   name)}`, `{base}/studio/{company_ref(id, name)}`, `{base}/franchise/{collection_ref(id,
   name)}`. Order within an entry: person, company, franchise, then by name casefolded; the
   title row last. An entity id the catalog no longer has (unresolvable) is dropped from the
   line, not rendered as a bare id.
5. Build `DigestEntry` per film with beats sorted by `beat_order_key`, then `rank_entries`.

`load_batch(session, *, recipient, cadence, today, settings, include_slate: bool | None =
None) -> DigestBatch` calls `load_entries` and keeps the slate rule as today (`cadence ==
"weekly" and recipient.deliverable`); `include_slate` overrides it when not `None` — this is
how `render_digest` loads a lapsed user's slate for M3 without changing the send path.
NEU-1462 will replace the cadence half of the rule with the slate-day rule.

## 5. Context, subject, preheader, cap (`digest_context`)

`digest_context(batch, *, cadence, today, settings) -> dict[str, object]` stays **the only
place the template dict is built**; templates never compute. Every string the mail shows is a
context value:

```
product_name, display_name, settings_url, cadence, slate_window_days,
slate: [ {heading, entries: [{title, release_label, film_url, poster_url}]} ]   # unchanged shape
subject: str
preheader: str            # "" when nothing to add — the HTML omits the span when falsy
entries: [ {              # batch.rendered_entries only, in rank order
    title, film_url, poster_url, parenthetical, status,
    following: [ {name, url} ],          # entity rows only; [] → no "Following:" line
    beats: [ {date, label, unconfirmed: bool, summary,
              source: {name, url} | None} ],
    credits_justwatch: bool,
} ],
overflow: int             # entries past the cap; 0 → no cap line
timeline_url: str         # f"{public_base_url.rstrip('/')}/"
```

**Subject** (`digest_subject`, DC-7), rendered by `subject.txt` as `{{ subject }}`:

- entries only: `"{lead.title} — {lead.lead_label.lower()}"` + `", + {N} more film{s}"` when
  `N = len(entries) - 1 > 0` (counts entries beyond the lead **including** those past the cap;
  singular "film" when N == 1);
- entries and slate: the above + `" · your slate"`;
- slate only: `"Your slate: {N} upcoming date{s}"` (N = `slate_count`);
- nothing: `render_digest` returns `None` before a subject is ever built; `digest_subject` on
  an empty batch raises `ValueError` so a caller that skips the check fails loudly.

Examples: `"Heat 2 — casting, + 2 more films"`, `"Dune: Part Three — new trailer · your slate"`,
`"Your slate: 1 upcoming date"`. The subject never carries a count of beats.

**Preheader** (`digest_preheader`, DC-10): `"Your slate: {N} dates in the next 30 days."`
(singular "date" when N == 1) when the slate is present; then `"Also: {e2.title} — {e2.lead_label.lower()}; {e3.title} — {e3.lead_label.lower()}"` for up to two entries after the lead; the two joined with a space; `""` when both are absent. HTML only: a first element inside the card, `<span style="display:none;max-height:0;overflow:hidden;">{{ preheader }}</span>`, **omitted** when `preheader` is falsy. No text counterpart.

**Cap** (DC-8): after the last rendered entry, when `overflow > 0`, one line
`"and {overflow} more film{s} on your timeline"` linking `timeline_url` (HTML: the phrase is the
link; text: phrase then the bare URL on the next line). `send_batch` marks **every**
`item_ids` row `sent`, capped or not.

**Text-part spelling** (the parity tests assert these exact strings):

- Entry header: `{title} ({parenthetical})` on one line, `{status}` on the next, then
  `Following: {name} <{url}>, {name} <{url}>` when present.
- Beat line: `{date} · {label} · {summary}` — with `[unconfirmed]` inserted after the label
  when rumored: `22 Sep · Casting [unconfirmed] · Ada is in talks.`
- Source line, indented under its beat: `via {name}` for TMDB, `via {name} — {url}` for a story.
- JustWatch: `Availability from JustWatch` once, after the entry's last beat.
- Then the film URL as a bare line, as today.

**HTML spelling** (current styles; NEU-1461 restyles): title link + `<span>({{ parenthetical }})</span>`, a muted status line, a "Following:" line with each name an `<a>`, beat `<p>`s with `<strong>{{ date }} · {{ label }}</strong>` and, when rumored, `<span style="…amber…">Unconfirmed</span>` after the label, then ` · {{ summary }}`; source as `via <a href>{{ name }}</a>` or plain `via TMDB`; the JustWatch line muted. `DIGEST_BEAT_LABELS` unchanged. Trailer beats are plain lines (DC-16). No clock time anywhere in either part (assert no `:\d\d` in the text part in tests).

The greeting, the slate section and the footer render exactly as today.

## 6. `render_digest` and `send_batch` (D-1460.1)

```python
def render_batch(batch: DigestBatch, *, cadence, today, settings) -> Envelope | None:
    """None when batch.has_content is False; otherwise mail.templates.render(DIGEST_TEMPLATE,
    digest_context(...), sender=settings.mail_from, to=batch.recipient.email)."""

async def render_digest(session, user_id: UUID, cadence: DigestCadence, today: date,
                        settings: Settings) -> Envelope | None:
    """The single render path (M3 calls this and nothing else). Loads the user (email,
    display_name) — raises LookupError for an unknown id — builds a DigestRecipient with
    deliverable=True (the preview ignores the gate), load_batch(..., include_slate=(cadence ==
    "weekly")), render_batch. Marks nothing, commits nothing, sends nothing."""
```

`send_batch` keeps its gate → fail-by-reason → nothing-to-say sequence unchanged, then:
`envelope = render_batch(batch, ...)`; `await mailer.deliver(envelope)` inside the same
`except` set as today; then `mark(... sent)`. `send_digests` passes `today` through (it already
holds it). Both `send_batch` and `render_digest` therefore render through `render_batch` —
one function, one context, one template.

`mail/`: `Mailer` Protocol gains `async def deliver(self, envelope: Envelope) -> MessageId`;
`MailGateway.deliver` = closed-check + `self._resolve().send(envelope)` (no render). `send`
is unchanged and now delegates its transport call to the same `_resolve().send`. Docstring on
`Mailer` says which callers use which: transactional mails render inside `send`; the digest
renders first because a preview and a test-send need the `Envelope` without a send. Any test
stub implementing `Mailer` that the digest tests hand in must implement `deliver`.

## 7. Tests

- **Unit** `tests/unit/app/test_digest_ranking.py` (new): `rank_entries`, `beat_order_key`,
  `short_date`, `long_date`, `status_line` (all three kinds + none), `arc_stage_label`,
  `digest_subject` (the three forms, singular/plural, cap-inclusive N, empty → `ValueError`),
  `digest_preheader` (slate only, entries only, both, one entry → "", singular date).
- **Unit** `tests/unit/app/test_film_parenthetical.py` (new): the nine frontend fixtures.
- **Unit** `tests/unit/mail/test_digest_template.py` — **rewritten** against the new context:
  text/html parity of every title, parenthetical, status, beat date, label, summary, source
  name, following name and URL; text has bare URLs and no `<a `; `Unconfirmed` / `[unconfirmed]`
  only on rumored beats; `via TMDB` unlinked, story source linked; JustWatch line present iff an
  entry has a `now_available` beat and exactly once per such entry; the cap line and its URL
  iff `overflow > 0`; the preheader span present iff `preheader` and absent from the text part;
  subject is `{{ subject }}` verbatim; entries render in list order; slate before entries;
  empty entries + empty slate → `MailError`; markup escapes in HTML not text.
- **Unit** `tests/unit/mail/test_gateway.py` (or where `MailGateway` is tested): `deliver`
  hands the envelope to the transport unrendered and refuses after close.
- **Integration** `tests/integration/app/test_digest_sender.py` — **updated**: the first two
  tests assert entries (ranked, one per film, beats in `created_at` order), the DC-7 subject,
  dates and no day headings; new tests for: ranking across three films with a stage tie broken
  by title; a `rumored` beat marked; a story card's first outlet named and linked and a catalog
  card reading "via TMDB"; the parenthetical and status line rendered from credits, countries,
  release year and a US wide date; a film reached by a director follow **and** a title follow
  names the director and not the film; a film reached only by title has no "Following:" line;
  `now_available` → JustWatch line; 21 films → 20 entries, the cap line "and 1 more film",
  subject "+ 20 more films", and **21 rows marked `sent`**; `render_digest` returns the same
  subject/text as the sent envelope, marks nothing, returns `None` for a user with nothing, and
  raises `LookupError` for an unknown id; a lapsed user still renders through `render_digest`
  (gate ignored) while `send_digests` still suppresses them. Existing slate, cadence, gate,
  failure and abort tests keep passing with their assertions re-pointed at the new shapes.
  Fixtures: `add_event(sources=(...), confidence=, provenance=, created_at=)`,
  `attach_credits`, `attach_countries`, `make_person`, `make_company`, `make_collection`,
  `add_release_date`, plus `Follow` rows for entity follows (see NEU-1459's tests for the
  subject_key shapes each branch matches).
- `test_every_visible_event_type_has_a_digest_label…` stays.

## 8. Acceptance criteria

1. One entry per film, ranked by `most_significant_event_type` then casefolded title then
   `tmdb_id`; beats in `created_at`, `occurred_at`, id order.
2. Header: poster (w154) + linked title, `(parenthetical)` spelled as `filmParenthetical`, and
   the status line per D-1460.2 / D-1460.3.
3. Beat line: short UTC date (year only when ≠ `today.year`), label, `Unconfirmed` /
   `[unconfirmed]` on `rumored`, summary; source line = first source outlet linked, or "via
   TMDB" for catalog cards, none when a story card has no story. No clock times.
4. "Following:" names entity follows only, linked to `/person|/studio|/franchise/:ref`; absent
   when only a title follow reached the entry.
5. "Availability from JustWatch" once under any entry with a `now_available` beat, both parts.
6. Subject and preheader per §5; N counts entries past the cap.
7. `DIGEST_MAX_ENTRIES = 20`: 20 rendered, "and N more films on your timeline" → `PUBLIC_BASE_URL/`,
   every queued row `sent`.
8. `render_digest(session, user_id, cadence, today, settings) -> Envelope | None` exists,
   marks nothing, and is the function `send_batch` renders through (via `render_batch`).
   `Mailer.deliver(envelope)` exists on the Protocol and `MailGateway`.
9. Text and HTML carry the same content; text has bare URLs and no `<a `.
10. `task format`, `task test`, `task lint`, `task typecheck` pass.

## 9. Out of scope / deferred

- Visual restyle, wordmark, lead card at w185/92px, alert restyle — **NEU-1461**.
- `SLATE_WEEKDAY`, the daily slate day, `SlateRow.marker`, `load_recipients` changes — **NEU-1462**.
- `Envelope.headers`, `List-Unsubscribe`, the unsubscribe token/routes — **NEU-1463** (which now
  sets the headers on the `Envelope` before `deliver`, per D-1460.1).
- The admin preview/test-send routes — **NEU-1464** (calls `render_digest`).
- Per-beat deep links, trailer thumbnails, per-user time zones, section toggles.
- `CONTEXT.md`: no new term; the glossary already defines every word used here.
