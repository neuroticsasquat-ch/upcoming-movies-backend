# Upcoming Movies — Domain Context

The backend ingests upcoming-film metadata (TMDB) and entertainment news, links stories to
films, clusters them into events, and summarizes those events for the tracker.

## Language

### News sources

**Trade feed**:
A curated RSS/Atom source from an established entertainment outlet (e.g. Variety, Deadline),
hard-coded in `news/feeds.py`. The canonical, always-on spine of news ingestion.
_Avoid_: source (too vague), publisher (that's the resolved outlet, see below).

**Google source**:
A story ingested via a Google News search rather than a trade feed — either a broad topic
**query** (casting / release date / trailer / greenlight) or a **per-film search** keyed on a
tracked film's title. Paused on a trial basis behind `NEWS_GOOGLE_ENABLED`.
_Avoid_: aggregator feed.

**Outlet**:
The real publisher behind a story. For a trade feed it's the feed itself; for a Google source
it must be *resolved* by decoding the Google redirect URL to a publisher domain.
_Avoid_: publisher, site.

### Trust & gating

**Source-quality gate**:
The link-stage sub-stage that, per story, resolves the outlet domain, LLM-judges the trust
tier of any unknown domain, and hard-drops admin-blocked stories before they can form or join
an event.
_Avoid_: source filter.

**Effective tier**:
The trust tier the gate actually acts on for a domain: `blocked`, `trusted`, `acceptable`, or
`low`. Precedence is admin override, then the cached LLM verdict, then a neutral default.
_Avoid_: rating, score.

**Admin override**:
A manual per-domain trust decision (`none` / `block` / `allow` / `trust`) set from the admin
Sources page. Wins over the LLM verdict.
_Avoid_: manual rating.

### Story lifecycle

**Story**:
A single ingested news item (unique by URL). Carries a `link_status` of `pending`, `linked`,
or `rejected`.
_Avoid_: article, item, post.

**Event**:
A cluster of stories about the same real-world development for a film. What the tracker
summarizes and displays.
_Avoid_: cluster (that's the act of forming an event), group.
