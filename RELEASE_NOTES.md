# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
## [0.3.0] - 2026-08-11

### Features

- Add the read-only undated-candidate probe ([#225](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/225))
- **ingest:** Add the sweep ingest run kind ([#226](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/226))
- **catalog:** Make quiescent undated films go dormant ([#228](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/228))
- **sweep:** Add the enumerate phase behind closed admission tranches ([#229](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/229))
- **sweep:** Add the reachability-scoped refresh phase (NEU-1078) ([#230](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/230))
- **sweep:** Add the sweep entrypoint and its own schedule slot ([#231](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/231))
- Add Event.provenance and the deterministic EventSummary writer (NEU-1080) ([#232](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/232))
- **sweep:** Catalog events from film_field_change — release date and status (NEU-1081) ([#233](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/233))
- **catalog:** Record seed-grade credit history, baselining first observation ([#234](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/234))
- **sweep:** Catalog events from credit changes (casting, crew_attached) ([#235](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/235))
- **public:** Carry arc_stage on the grouped feed row ([#236](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/236))
- **sweep:** Wire the admission bar to config with a corroboration threshold ([#237](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/237))

### Bug Fixes

- Identify the prod database by content unique to this app ([#223](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/223))

### Documentation

- Record the undated-film discovery design of record (NEU-285) ([#222](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/222))

### Testing

- **tmdb:** Cover the remaining person_movie_credits cases ([#227](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/227))

### Miscellaneous

- Log cluster-stage attach decisions ([#224](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/224))

## [0.2.0] - 2026-08-08

### Features

- Add TMDB_MIN_RUNTIME setting for shorts filter
- Add classify_skip ingest filter for shorts
- Skip shorts during TMDB ingest
- Add director, stars, and genres to calendar API
- Add admin delink + delete-event endpoints
- Guard linker against franchise-generic casting traps
- Resolve Google News story URLs to publisher URLs
- **link:** Guard linker against aspirational/wishlist casting (NEU-443)
- Add nullable region column to news.event
- Extract per-event region from cluster classifier
- Persist region on new release_date events
- Quiet non-primary-country release_date events on public surfaces
- **events:** Add first_look event type (NEU-447)
- **synthesize:** Url-resolution covers all displayed events (NEU-455)
- **link:** Source-quality gate with domain judge and confidence downgrade (NEU-454)
- **link,synthesize:** Thread run_date into prompts for temporal reasoning (NEU-457)
- Add retroactive cleanup for admin-blocked source domains
- **catalog:** Skip processing released and canceled films (NEU-286)
- **catalog:** Track film column value changes via film_field_change trigger (NEU-493)
- **link:** Guard release-date restatements in cluster stage (NEU-494)
- **link:** Deterministic per-performer casting dedup (NEU-492)
- Admin edit + reset-to-AI for event summaries
- **calendar:** Require minimum popularity score for release calendar
- **calendar:** Require minimum runtime for release calendar
- Pause Google News, rely on curated trade feeds on a trial basis
- Gate release-date events on TMDB release_date changes
- Run ingestion as in-process Coolify scheduled tasks
- Make film search cover the whole catalog
- **observability:** Auto-instrument the API with OpenTelemetry → SigNoz
- Add the ingest.llm_call per-call telemetry table (NEU-974)
- Record per-call LLM telemetry on the Anthropic path ([#180](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/180))
- Add title normalization and the squash-fold for candidate retrieval ([#183](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/183))
- Build the in-memory token→film candidate index (NEU-991) ([#184](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/184))
- Score and select retrieval candidates with a threshold and cap ([#185](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/185))
- Add the ingest.link_retrieval_probe and run_retrieval_health tables ([#187](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/187))
- Add the three-state LINK_RETRIEVAL_MODE setting ([#188](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/188))
- Run candidate retrieval in shadow beside the roster path ([#189](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/189))
- Surface retrieval health and its trend on /admin/runs ([#190](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/190))
- Build the per-story candidate link request ([#191](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/191))
- Apply retrieval decisions and keep zero-candidate rejects out of processed ([#192](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/192))
- Port the offline eval harness to the retrieval path (NEU-1000) ([#193](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/193))
- Tune the retrieval threshold, cap and batch size ([#194](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/194))
- Guard retrieval health with a hard-breach run failure (NEU-1002) ([#196](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/196))
- Restate cutover gate #3 in whole items and record the cutover evidence ([#199](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/199))
- Enlarge the link validation fixture and re-score gate #3 (NEU-1012) ([#200](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/200))
- **llm:** Add the OpenAI-compatible adapter and a shared retry policy ([#211](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/211))
- **llm:** Verify provider capabilities empirically and pin fixtures to real bodies ([#212](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/212))
- **llm:** Resolve a provider per stage through a Gateway (NEU-980) ([#213](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/213))
- **llm:** Validate every stage's provider routing at startup ([#214](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/214))
- **llm:** Move production to DeepInfra, with truncation telemetry first (NEU-1015) ([#216](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/216))
- **llm:** Make the offline harnesses declare which provider they scored

### Bug Fixes

- **link:** Treat fresh production_wrap on wrapped films as stale-stage (NEU-444)
- Classify events by dominant beat, not incidental cast mentions
- **link:** Distinguish animated character-design reveals from live-action costume stills in cluster prompt (NEU-445)
- **arc:** Derive film arc stage from TMDB status only (NEU-452)
- **link,cluster,synthesize:** Reject cross-film stories and fix beat-date attribution (NEU-453)
- **sources_admin:** Stop CSRF-guarding the GET list endpoint
- **link:** Drop stale-stage events for films past their release date (NEU-449)
- **link:** Drop restatement and roundup release-date events (NEU-451)
- **link:** Add windowed dedup guardrail for trailer and first-look events (NEU-448)
- **link:** Reject sibling-franchise release dates as off-topic (NEU-450)
- Chunk source-domain judge calls and add backfill helper (NEU-460)
- **link:** Prevent parent/original film misattribution in linker and clusterer (NEU-461)
- **news:** Tighten LINK/SYNTHESIZE prompts for casting, release-date, and summary quality (NEU-483)
- Allow PATCH in CORS allowed methods
- **calendar:** Correct broken runtime filter and popularity test gap
- Register all models on db import so standalone scripts resolve cross-schema FKs
- Set release date text fixtures as relative to today rather than hardcoded
- Fail ingest runs when a whole Anthropic batch fails
- Recompute event occurred_at when repair removes member stories
- Fail ingest runs when a stage produces nothing at all ([#176](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/176))
- Require a denominator before failing self-healing ingest stages ([#177](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/177))
- Repair three mislabeled rows in the link validation fixture ([#182](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/182))
- Collapse dotted and slashed initialisms before tokenizing ([#195](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/195))
- Pin the link validation fixture to a catalog date ([#197](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/197))
- Stop scoring unlabelable picks and tell the linker its candidates are a prefilter ([#198](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/198))
- **llm:** Let summarize run on an OpenAI-compatible provider

### Refactor

- Make event summaries write-once, drop auto-regeneration
- Remove the Anthropic Message Batches path ([#175](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/175))
- Name both halves of the total-failure rule with StageKind ([#178](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/178))
- Delete the roster link path and port its dependent scripts (NEU-1004) ([#201](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/201))
- Replace Anthropic content blocks with a neutral Prompt DTO ([#208](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/208))
- **llm:** Key pricing on (provider, model) with per-entry cache rates ([#210](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/210))

### Documentation

- Record ADR 0004 and name attach/split-beat/over-merge
- Record ADR-0005 removing the Message Batches path
- Define "Stage" in the domain glossary
- Record the entity-linking candidate-retrieval decisions ([#181](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/181))
- Add ADR-0006 on the stable-prefix-first caching contract ([#209](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/209))
- Record why the gateway is a first-party adapter, not LiteLLM ([#215](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/215))

### Testing

- Add Angry Birds first-look gold event for clustering validation
- Cover origin_country=NULL edge case in region surfacing
- Gate retrieval recall with a zero-cost oracle test ([#186](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/186))

### Build System

- **deps:** Update sentry-sdk[fastapi] requirement ([#113](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/113))
- **deps:** Update fastapi requirement from >=0.136.1 to >=0.139.0 ([#114](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/114))
- **deps:** Update alembic requirement from >=1.18.4 to >=1.18.5 ([#115](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/115))
- **deps:** Update uvicorn[standard] requirement ([#116](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/116))
- **deps:** Update httpx requirement from >=0.28 to >=0.28.1 ([#117](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/117))
- **deps:** Update sqlalchemy[asyncio] requirement ([#132](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/132))
- **deps:** Update ruff requirement from >=0.9 to >=0.15.21 ([#133](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/133))
- **deps:** Update email-validator requirement from >=2.0.0 to >=2.3.0 ([#134](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/134))
- **deps:** Update tldextract requirement from >=5.1 to >=5.3.1 ([#135](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/135))
- **deps:** Update asyncpg requirement from >=0.30 to >=0.31.0 ([#136](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/136))
- **deps:** Update fastapi requirement from >=0.139.0 to >=0.139.2
- **deps:** Update python-slugify requirement from >=8.0 to >=8.0.4
- **deps:** Update types-python-slugify requirement ([#143](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/143))
- **deps:** Update sentry-sdk[fastapi] requirement ([#144](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/144))
- **deps:** Update uvicorn[standard] requirement ([#145](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/145))
- **deps:** Update fastapi requirement from >=0.139.2 to >=0.140.0
- **deps:** Update pytest-cov requirement from >=6.0 to >=7.1.0
- **deps:** Update ruff requirement from >=0.15.21 to >=0.16.0
- **deps:** Update feedparser requirement from >=6.0.11 to >=6.0.12
- **deps:** Update pydantic requirement from >=2.10 to >=2.13.4
- **deps:** Update uvicorn[standard] requirement ([#164](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/164))
- **deps:** Update sentry-sdk[fastapi] requirement ([#165](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/165))
- **deps:** Update anthropic requirement from >=0.49 to >=0.120.2 ([#166](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/166))
- **deps:** Update feedparser requirement from >=6.0.12 to >=6.0.13 ([#167](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/167))
- **deps:** Update fastapi requirement from >=0.140.0 to >=0.141.1 ([#168](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/168))
- **deps:** Update uvicorn[standard] requirement ([#203](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/203))
- **deps:** Update alembic requirement from >=1.18.5 to >=1.19.0 ([#204](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/204))
- **deps:** Update feedparser requirement from >=6.0.13 to >=6.0.14 ([#205](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/205))
- **deps:** Update ruff requirement from >=0.16.0 to >=0.16.1 ([#206](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/206))

### CI

- **deps:** Bump actions/checkout from 6 to 7 ([#131](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/131))
- **deps:** Bump actions/setup-python from 6 to 7

### Miscellaneous

- Scaffold agent-skills config (Linear tracker, triage labels, domain docs)
- Ruff format
- **lint:** Exclude Markdown from ruff format
- Skip hidden event types in synthesis

### Other

- Ruff format fix

## [0.1.1] - 2026-06-27

### Features

- Return full crew in film-detail API ([#82](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/82))
- Expose imdb_id and tmdb_id in film-detail API ([#83](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/83))

### Build System

- **deps:** Update pytest requirement from >=9.0.3 to >=9.1.1 ([#74](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/74))
- **deps:** Update pytest-asyncio requirement from >=0.25 to >=1.4.0 ([#75](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/75))
- **deps:** Update respx requirement from >=0.22 to >=0.23.1 ([#76](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/76))
- **deps:** Update pyright requirement from >=1.1 to >=1.1.411 ([#77](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/77))
- **deps:** Update pydantic-settings requirement from >=2.7 to >=2.14.2 ([#78](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/78))

### CI

- Match tvbf dependency-automation setup (commit prefixes + merge workflow)
- **deps:** Bump actions/setup-python from 5 to 6 ([#72](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/72))
- **deps:** Bump actions/checkout from 6 to 7 ([#73](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/73))
- Add Sentry release tag and ingestion cron monitors
- Remove Sentry cron-monitor check-ins (exceeds free quota)

## [0.1.0] - 2026-06-27

### Features

- Add backend config and db base (NEU-257)
- Add auth models and films/stories schema seam (NEU-257)
- Add passwords, tokens, errors, auth DTOs (NEU-257)
- Add auth repos (user, session, login_attempt, invite) (NEU-257)
- Add account and invite services (NEU-257)
- Add deps, auth/health/me/invite routers, app factory (NEU-257)
- Add docker, compose, taskfile, env for local dev (NEU-257)
- Add alembic config and migrations harness (NEU-257)
- Add initial app/catalog/news migration; create schemas in alembic env (NEU-257)
- Add ingest schema, ingest_run model, run helpers, migration (NEU-263)
- Add httpx-based TMDB API client with DTOs (NEU-264) ([#2](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/2))
- Add TMDB discover ingestion service + catalog.film upsert (NEU-265) ([#3](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/3))
- Add feed fetcher + source config + news.story upsert (NEU-266) ([#4](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/4))
- Add admin ingest trigger endpoints + orchestration (NEU-267) ([#5](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/5))
- Add is_admin flag + session admin dependency + run read endpoints (NEU-268) ([#6](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/6))
- Restrict TMDB ingest to pre-release films (floor 1.0, exclude Released/Canceled) ([#9](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/9))
- Capture full TMDB object with normalized catalog tables and drop en/US filter ([#10](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/10))
- Add story link-state columns and link ingest kind ([#12](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/12))
- Add Anthropic client wrapper and entity-linking config ([#13](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/13))
- Add film roster builder and LLM link service with confidence floor ([#14](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/14))
- Add link ingestion pipeline, trigger endpoint, and cron step ([#15](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/15))
- Add news.event and news.event_story schema and migration ([#16](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/16))
- Add event clustering/classification as link pipeline stage 2 ([#17](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/17))
- Add linking accuracy metrics harness and baseline runner ([#19](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/19))
- Add feed recency hygiene — when:Nd Google filter + published_at gate (NEU-283) ([#20](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/20))
- Add per-film Google News fetching alongside broad queries (hybrid) ([#21](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/21))
- **ingest:** Add per_film query param override to feeds trigger ([#23](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/23))
- Retune feed/link recency windows to 3d/4d (NEU-293) ([#24](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/24))
- **llm:** Add complete_batch surface to AnthropicClient ([#26](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/26))
- **link:** Add link_use_batches flag for batch API Stage-1 path ([#27](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/27))
- **link:** Add Usage surface and link cost measurement harness (NEU-297) ([#28](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/28))
- Extend use_batches flag to Stage-2 clustering via Batches API ([#29](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/29))
- **news:** Add EventSummary model, migration, and integration tests ([#31](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/31))
- **synthesize:** Add Haiku 4.5 batched-paraphrase service ([#32](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/32))
- **catalog:** Add slug field to Film with collision-safe backfill ([#33](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/33))
- Add public film index, detail, and sitemap endpoints ([#34](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/34))
- Add public GET /feed endpoint for global summarized event feed ([#35](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/35))
- **synthesize:** Add synthesize ingest pipeline ([#36](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/36))
- **link:** Add not-news filtering and production-news validation axis (NEU-358) ([#37](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/37))
- **synthesize:** Trim summary prompt to 1–2 sentences, bump prompt version (NEU-359) ([#38](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/38))
- **news:** Resolve Google News outlet from RSS source element and title suffix (NEU-360) ([#39](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/39))
- **public:** Cap per-event source citations at 3 distinct outlets (NEU-361) ([#40](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/40))
- **public:** Add per-film-per-day grouped feed endpoint (NEU-364) ([#41](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/41))
- **link:** Reject stale-stage events and hide other events from public feed (NEU-367) ([#43](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/43))
- Tighten summary prompt; bump prompt version to 3 ([#44](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/44))
- **link:** Widen existing-event attach lookback for cross-day dedup ([#46](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/46))
- **ingest:** Capture and persist per-stage LLM token usage and cost (NEU-375) ([#47](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/47))
- **link:** Add independent cluster_use_batches flag for stage-2 batch control (NEU-378) ([#49](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/49))
- Add cluster-diff eval harness for the Haiku-vs-Sonnet clustering spike (NEU-380) ([#53](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/53))
- **scripts:** Add per-source ROI audit script (NEU-381) ([#54](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/54))
- **news:** Drop off-topic per-film stories at fetch time (NEU-382) ([#55](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/55))
- **link:** Add Stage-2 cluster-purity baseline harness (NEU-300) ([#57](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/57))
- **ingest:** Add TMDB release dates ingestion and public API (NEU-404) ([#62](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/62))
- **public:** Order and expose film-detail events by created_at ([#63](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/63))
- **public:** Expose film metadata on film-detail DTO (NEU-396) ([#64](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/64))
- **public:** Add GET /films/search?q= endpoint (NEU-400) ([#65](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/65))
- **catalog:** Ingest TMDB alternative titles and match them in search (NEU-406) ([#66](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/66))
- **ingest:** Ingest TMDB credits and expose cast/directors on film detail (NEU-402) ([#67](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/67))
- **public:** Add GET /calendar endpoint (NEU-408) ([#68](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/68))
- UX improvements

### Bug Fixes

- Restore Empire feed via new URL and identifying User-Agent ([#11](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/11))
- **link:** Replace UUID story refs with positional indices in cluster prompt (NEU-365) ([#42](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/42))
- **link:** Remove cache_control from cluster prompt blocks below token floor (NEU-377) ([#48](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/48))
- **link:** Revert LINK_BATCH_SIZE default to 15 ([#51](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/51))
- **synthesize:** Recover summary value from malformed JSON envelopes (NEU-366) ([#56](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/56))
- **link:** Surface Stage-2 cluster parse failures instead of silently dropping the film ([#59](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/59))

### Performance

- **link:** Halve roster overview cap and double batch size (NEU-379) ([#50](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/50))

### Refactor

- Order within-day grouped feed by film popularity ([#45](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/45))

### Documentation

- **link:** Record verified cache prefix size in build_link_request ([#52](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/52))
- Record Linear initiative/team in CLAUDE.md ([#60](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/60))

### Testing

- Add pytest harness for app/catalog/news schemas (NEU-257)
- Add auth/me/health/invite integration tests (NEU-257)
- Add link/cluster validation fixture and export helper (NEU-278) ([#18](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/18))
- Add M3 linking validation set and review/diagnostic tooling ([#30](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/30))

### CI

- Add backend test workflow and dependabot config (NEU-257)
- Add daily ingestion cron workflow (NEU-269) ([#7](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/7))
- Split daily cron into hourly-feeds + daily-pipeline workflows (NEU-294) ([#25](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/25))

### Miscellaneous

- Scaffold backend project metadata (NEU-257)
- Add repo CLAUDE.md guide ([#8](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/8))
- **news:** Prune zero-value trade RSS feeds (NEU-383) ([#58](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/58))
- **public:** Thread PUBLIC_BASE_URL into prod compose for backlotter.com sitemap
- **deploy:** Complete prod compose for Coolify deployment ([#69](https://github.com/neuroticsasquat-ch/upcoming-movies-frontend/pull/69))

### Other

- Fix(feeds): use film-category feeds for news sources

