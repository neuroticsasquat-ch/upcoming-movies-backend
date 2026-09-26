# backlotter — Consumer Pivot Spec

**Status:** design agreed, not yet implemented
**Purpose of this document:** source material for `/projectit` to generate a Linear project, milestones, and tickets.

---

## 0. How to use this document

Milestones in §4 are dependency-ordered. Each has a goal, scope, and acceptance
criteria, and is intended to become a Linear milestone with its scope bullets as
tickets.

**Read §2 before writing any code.** Those are invariants, not preferences. Several
of them look like bugs or inefficiencies from inside a single file and will get
"fixed" by anyone who hasn't read the reasoning. The reasoning is included for
exactly that reason — do not strip it.

---

## 1. Product context

### 1.1 Audience

backlotter was built as a production-news tracker aimed at industry professionals.
**It is pivoting to consumers.** Rationale: existing paid trade services already serve
insiders well and are not realistically matchable by a solo developer.

### 1.2 What the product becomes

A follow-based film tracker: users follow **people, companies, franchises, and titles**,
and receive a personalized timeline covering a film's whole public lifecycle —
announcement → casting → production → theatrical date → **home release**.

Mental model: MusicHarbor for movies. The defining property of MusicHarbor is not
that it tracks releases; it is that it tells you about a release you had *never heard
of*, because you follow the artist. Search-driven and watchlist-driven products
require the user to already know the thing exists.

### 1.3 Competitive position

Watchlist availability alerts already exist:

- **JustWatch** — free, with a ~$2.50/mo Pro tier; alerts when a watchlist title becomes streamable.
- **Letterboxd** — paid members get once-daily email/push when a watchlist film hits a favorite service, split into buy / rent / stream tiers. Powered by JustWatch data, lagging up to ~24h.

**Implication:** home-release alerting is table stakes. Build it for retention; do not
position or price on it. Both competitors are *reactive* and *require prior knowledge
of the title*. The differentiator is the follow graph auto-seeding the watchlist, plus
forward-looking announced digital dates, which neither competitor surfaces.

### 1.4 Explicit non-goals

- Ongoing service-to-service churn ("now on Hulu, now on Peacock"). Only **first**
  availability per monetization type is tracked. See §4.5.
- Leaving-service alerts. The upstream data does not support advance warning.
- Industry-professional features: slate tracking, competitive intelligence, deal flow.
- Native mobile app (v1 is web + email + push + iCal).

---

## 2. Invariants

These are load-bearing. Each includes why, because each is locally counterintuitive.

### INV-1 — State mirrors live; events are quarantined

**State** (the cast/crew list shown on a movie page) is a mirror of TMDB and publishes
immediately with no delay. If it is wrong, TMDB is wrong, and it self-corrects on the
next sync leaving no residue.

**Events** ("Actor X joined the cast," timestamped, in a feed) are append-only in the
user's perception. A retraction does not cancel the original row; it produces two wrong
rows instead of zero. Events therefore pass a persistence threshold before publishing.

Never unify these two paths into one "update" concept. They have opposite correctness
requirements.

### INV-2 — Publish date sorts; detect date is metadata

The feed is read as *"what's new since I last looked."* That makes it a **publication
queue**, not a timeline of the world. The only date that can safely sort it is the date
the item became visible in the app.

Backdating a released-from-quarantine event to its detection date inserts it *below* the
user's read watermark, where it is silently never seen. That is the worst available
failure mode, because to the user it is indistinguishable from nothing having happened.

`first_detected_at` is still stored on every record — it is needed for §4.4 window
tuning, for "who reported first" comparisons against the trades, and for a small
"first seen on <date>" line on detail views. It must never drive feed ordering.

### INV-3 — Quarantine suppresses data errors, not real-world churn

An actor genuinely joining in March and exiting in June is **two legitimate events** and
both must publish. The threshold exists only to suppress edits that were never true:
vandalism, misfiles, mistaken entries. Do not add logic that collapses or suppresses
genuine sequential changes.

### INV-4 — Trade confirmation short-circuits the quarantine timer

If a Tier-A trade publishes a story matching a credit currently in quarantine, release
the credit immediately and **merge** it into the news event. It must not surface
separately two days later as a stale duplicate.

### INV-5 — The LLM emits names and relations, never IDs

Any prompt asking a model for a TMDB ID returns a plausible, well-formed, wrong integer.
Extraction is probabilistic; resolution is deterministic. See §4.3.

### INV-6 — Child confidence cannot exceed parent confidence

If the article→movie link was low-confidence, the article→person link derived within
that context cannot be rated higher. Confidence propagates down the chain, never up.

### INV-7 — Follows produce timeline rows; only watchlist produces pushes

Reliability and volume are separate problems. A perfectly accurate feed still bombards.
Following a prolific actor must not become a push firehose.

### INV-8 — "Not in TMDB" is a valid resolution outcome

First-time directors and unknown actors legitimately have no TMDB person record. The
resolver must be able to return "person exists, no TMDB record" rather than being forced
into a wrong match.

---

## 3. Data model

Sketch, not final DDL. Names are suggestions; the shape is the point.

### 3.1 Claims

Everything ingested — trade and TMDB alike — lands here. Alerting is a **separate
decision over this table**, never a side effect of ingest.

```
claim
  id
  subject_type, subject_id        -- person / company / franchise
  predicate                       -- joined_cast, exited_cast, directing, dated, ...
  object_type, object_id          -- title
  source_type                     -- trade | tmdb_change
  source_id                       -- article id or TMDB change id
  confidence                      -- 0..1
  first_detected_at
  published_at                    -- NULL while quarantined
  status                          -- pending | published | superseded | discarded
  superseded_by                   -- claim id
  evidence_span                   -- verbatim text for trade claims
```

Storing claims rather than facts gives retraction for free: a later "exits project" item
supersedes rather than corrupts.

### 3.2 Follows

```
follow
  user_id
  entity_type                     -- person | company | franchise | title
  entity_id                       -- TMDB id
  created_at
  source                          -- manual | letterboxd_import | tmdb_import | derived
```

Personalized feed = join of `follow` against existing article↔entity links. No new
ingest, no new LLM spend.

### 3.3 Watchlist

```
watchlist_item
  user_id, title_id, created_at
  source                          -- manual | derived_from_follow
  alert_prefs                     -- buy | rent | stream (subset)
```

### 3.4 Availability

```
availability_first_seen
  title_id
  provider_id
  monetization_type               -- flatrate | rent | buy
  first_seen_at                   -- insert-only
```

Event emitted on **first insert only**. Row exists thereafter; the title goes quiet
forever. This is the mechanism that implements §1.4's no-churn rule.

### 3.5 Resolution cache

```
resolution_cache
  source_domain
  name_as_written
  title_id
  person_id                       -- nullable (see INV-8)
  confidence
  resolved_at
```

Trades recycle phrasing constantly; most lookups after the first month should be cache
hits. Meaningfully cuts LLM spend.

---

## 4. Milestones

### 4.1 — Claim store and the event/state split

**Goal:** the foundation everything else sits on.

Scope:
- `claim` table, ingest for both trade and TMDB-change sources.
- Separate the live-state mirror (cast/crew list) from the event log. Enforce INV-1.
- Movie page restructure: **current cast/crew on top, event log below it**, with
  confidence visible in the styling. A flat chronological log giving a Deadline report
  and an anonymous TMDB edit equal weight is the single thing most likely to make a
  civilian distrust the app.
- Supersession handling: retraction inside the window → never publishes; retraction after
  publishing → marked superseded, **not** silently deleted. Silent deletion is worse than
  the original error.

Acceptance:
- A credit added and reverted within the window produces zero feed rows and a correct
  live cast list throughout.
- A credit added and removed weeks apart produces two rows, the second marked superseding.

### 4.2 — Follow graph and import

**Goal:** turn a firehose about everything into a feed about things the user cares about.

Scope:
- `follow` table over person / company / franchise / title.
- Personalized feed query.
- **Letterboxd CSV import** — watchlist plus ratings; derive followed people from
  directors/cast of highly-rated films.
- **TMDB account import** — watchlist and favorites (API key already held).
- Onboarding flow targeting civilians, not insiders.

Cold start is the make-or-break here. An empty follow graph is an empty room, and
manual following of 200 people will not happen.

Acceptance:
- A user can go from signup to a populated timeline via one CSV upload.

### 4.3 — Entity resolution pipeline

**Goal:** link a trade story's named person to a TMDB person ID. Trade RSS is reliable
about the *fact* and unreliable about the *entity link*; the TMDB change stream is
reliable about the *entity* and unreliable about the *fact*. Use each on its strong axis.

Three stages:

**Stage 1 — Extraction (LLM).** From article text, emit tuples: name as written, role,
department, title mentioned, event type, verbatim evidence span. Extends the existing
LLM pass. Enforce INV-5.

**Stage 2 — Candidate generation (deterministic).** Union of:
- `/search/person` on the extracted name
- current cast/crew of the already-linked movie
- anyone in the TMDB change stream for that title in the last ~14 days

That third source is strong: a trade reporting a casting plus a credit edit on the same
film within days is close to a match on its own. **This inverts the slop — the change
stream is a poor fact source and an excellent candidate generator.** Cap at 5–10
candidates.

**Stage 3 — Scoring, with LLM only on ties.** Deterministic features:
- name match quality (exact / normalized / initials / suffix)
- candidate already credited on this film, or in its recent change stream
- department matches extracted role
- overlap between candidate filmography and *other* titles named in the article
- age plausibility vs. role; alive at reported date
- popularity prior — **tiebreaker only, never primary**

Wide margin over runner-up → accept. Nothing scores well → unlinked queue. Only the
narrow ambiguous band goes to an LLM, as **closed-set multiple choice**: article text
plus shortlist with known-for credits, pick one or say none. Target ≤10% of items.

Do not train a model. There are no labels yet. The scoring function is the labeling
apparatus — log every decision with its features and corrections.

Scope:
- Extraction prompt and schema
- Candidate generator
- Scoring function with logged features
- LLM tiebreak path
- Unlinked queue — items may appear in a general feed but **must never trigger a
  personalized alert**. The failure mode is pinging someone about the wrong Chris Evans.
- `resolution_cache`

Acceptance:
- Decisions are individually inspectable: which candidates, which feature scores, which path.

### 4.4 — Quarantine and publication queue

**Goal:** publish every reliable item, suppress slop, never bury a released item.

Scope:
- Quarantine window for credit-derived events, 48–72h starting point.
- **Tune the window from real data**: log every credit add and delete for a month, plot
  survival-time-to-deletion, set the window at the knee of the curve. Do not guess.
- Variable bar by signal strength: longer quarantine for low-billing cast, churn-prone
  crew departments, and known defacement-magnet titles. A first-billed lead is a much
  stronger claim than an unbilled bit part.
- Sanity checks: person credited on 20 projects in a day; credits inconsistent with
  birth/death dates; add→remove→re-add churn.
- Publication queue ordered by `published_at` (INV-2).
- **Intra-day ordering by significance** — billing order, event type. All quarantined
  events for a title pop the same day, so detection order is meaningless within a day.
- **Burst collapsing.** Six credits added across four days that clear the window together
  must publish as one expandable "six cast members added" event, not six rows reading as
  "huge casting news today." Existing clustering is roughly the right shape.
- Tier-A confirmation short-circuit (INV-4).
- Promotion, not duplication: when a trade confirms an already-published unconfirmed
  event, **upgrade that row** to the news tier. This is also the case where TMDB
  legitimately scoops the trades — preserve the ability to show you had it first.

Tier-A sources: Deadline, Variety, THR, TheWrap, Screen Daily, studio PR. These may fire
alone *if* entity resolution cleared threshold. **A TMDB-only change never fires an
alert** — it is unconfirmed evidence that can publish to the feed after quarantine.

Acceptance:
- No published event is ever retracted silently.
- No released-from-quarantine event sorts below a user's read watermark.

### 4.5 — Home release tracking

**Goal:** tell users when they can watch a film at home. Two distinct mechanisms.

**Forward-looking:** TMDB `/movie/{id}/release_dates`, type 4 (Digital) and type 5
(Physical), region-filtered to US. Sparse and sometimes wrong, but the only source that
supports "arrives October 14" *before* it happens — precisely what JustWatch and
Letterboxd cannot do.

**Detection:** poll `/watch/providers`, catch absent→present transitions. Same JustWatch
pipe Letterboxd uses; expect comparable ~24h lag.

Scope:
- Release-dates ingest for types 4 and 5.
- Provider polling with a **scoped poll set**: titles whose theatrical date is ~14–200
  days old, plus anything any user follows or watchlists. Everything else is dead weight
  and turns this into its own cost problem.
- `availability_first_seen` with insert-only event emission (§3.4).
- US only for v1; schema must not assume single-region.

Acceptance:
- A title moving between flatrate services after first availability produces zero events.

### 4.6 — Notifications, digest, and calendar

**Goal:** deliver without bombarding.

Scope:
- Follows → timeline rows only. Watchlist → pushes only (INV-7).
- Push whitelist, short: got a date, date moved, digital release, trailer. **Date slips
  are the standout** — films move constantly and nothing tracks it well for civilians.
- Everything else batches into daily or weekly digest.
- Confidence labeling in UI. Civilians tolerate rumor fine when it is labeled
  "unconfirmed"; they will not tolerate being woken up by it.
- Weekly "your slate" email.
- **Per-user iCal subscription URL.** Roughly 50 lines, no app required, and it puts the
  product permanently in the user's calendar.
- TMDB `/watch/providers` links on title pages (the "play on Spotify" equivalent).
- TMDB `/videos` for trailers — a new trailer is the movie analog of a lead single.

---

## 5. Open questions

- Quarantine window length — deliberately unresolved pending the survival analysis in §4.4.
- Whether franchise follows resolve via TMDB collections, keywords, or a hand-curated
  mapping. Collections are incomplete; keywords are noisy.
- Pricing for the consumer tier. The prior $4/mo was set against an insider audience and
  should not be assumed to carry over.
- Whether the unlinked queue needs a manual review UI or can stay log-only for v1.
