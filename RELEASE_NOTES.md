# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
## [0.1.1] - 2026-06-27

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

