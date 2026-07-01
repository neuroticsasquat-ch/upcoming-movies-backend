"""Source-quality helpers: normalize publisher domains, resolve an effective trust tier
(admin override wins over the LLM verdict), and downgrade event confidence when every
source is low-trust. Pure functions here have no DB or LLM dependency; the judge and DB
I/O live lower in this module. The caller owns any transaction."""

import json
from collections.abc import Collection, Iterable
from datetime import datetime
from typing import Any

import tldextract
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.link.linker import Completer
from upmovies.llm.client import Usage
from upmovies.news.models import SourceDomain
from upmovies.news.resolve import is_google_news_url

# Disable the network suffix-list fetch; rely on tldextract's bundled snapshot so this is
# deterministic and never hits the network (test rule + offline container safety).
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

TIER_RANK: dict[str, int] = {"low": 0, "acceptable": 1, "trusted": 2}
VALID_TIERS: frozenset[str] = frozenset({"trusted", "acceptable", "low"})
VALID_OVERRIDES: frozenset[str] = frozenset({"none", "block", "allow", "trust"})
_OVERRIDE_TIER: dict[str, str] = {"block": "blocked", "trust": "trusted", "allow": "acceptable"}


def normalize_domain(value: str | None) -> str | None:
    """Registrable domain (lowercase) for a URL, e.g. `m.mshale.com/amp` -> `mshale.com`,
    `news.bbc.co.uk` -> `bbc.co.uk`. None when there is no registrable domain."""
    if not value:
        return None
    registered = _EXTRACT(value).top_domain_under_public_suffix
    return registered.lower() or None


def domain_for_story(*, url: str, resolved_url: str | None) -> str | None:
    """The publisher domain to gate on. Prefer the resolved publisher URL; an UNRESOLVED
    Google-News redirect returns None (treated as the neutral default tier — we won't tag
    it as `google.com`)."""
    target = resolved_url or url
    if is_google_news_url(target):
        return None
    return normalize_domain(target)


def effective_tier(*, llm_tier: str | None, admin_override: str, unresolved_default: str) -> str:
    """The tier the gate uses. Admin override wins; otherwise the cached LLM tier; otherwise
    the neutral default. Returns one of `blocked` / `trusted` / `acceptable` / `low`."""
    if admin_override in _OVERRIDE_TIER:
        return _OVERRIDE_TIER[admin_override]
    return llm_tier or unresolved_default


def best_tier(tiers: Iterable[str], *, default: str) -> str:
    """The most-trusted tier present, ignoring `blocked` (blocked stories are dropped before
    they reach here). `default` when no rankable tier is present."""
    ranked = [t for t in tiers if t in TIER_RANK]
    if not ranked:
        return default
    return max(ranked, key=lambda t: TIER_RANK[t])


def downgrade_confidence(confidence: str, best_tier: str) -> str:
    """Force `rumored` when the best available source tier is `low`; otherwise keep the
    LLM's confidence verdict."""
    return "rumored" if best_tier == "low" else confidence


async def get_source_domains(
    session: AsyncSession, domains: Collection[str]
) -> dict[str, SourceDomain]:
    """Look up existing rows for the given domains, keyed by domain. Missing domains are
    simply absent from the result."""
    domain_list = [d for d in {d for d in domains} if d]
    if not domain_list:
        return {}
    rows = (
        (await session.execute(select(SourceDomain).where(SourceDomain.domain.in_(domain_list))))
        .scalars()
        .all()
    )
    return {row.domain: row for row in rows}


async def upsert_judgements(
    session: AsyncSession,
    verdicts: dict[str, tuple[str, str]],
    *,
    model: str,
    now: datetime,
) -> int:
    """Insert judge verdicts for previously-unknown domains: `{domain: (tier, reason)}`.
    Existing rows are left untouched (domains are judged once). Returns rows inserted.
    Caller commits."""
    if not verdicts:
        return 0
    existing = await get_source_domains(session, list(verdicts))
    inserted = 0
    for domain, (tier, reason) in verdicts.items():
        if domain in existing:
            continue
        session.add(
            SourceDomain(
                domain=domain,
                llm_tier=tier,
                llm_reason=reason,
                llm_model=model,
                admin_override="none",
                first_seen_at=now,
                judged_at=now,
                updated_at=now,
            )
        )
        inserted += 1
    await session.flush()
    return inserted


async def list_source_domains(session: AsyncSession) -> list[SourceDomain]:
    """All known source domains, most-recently-updated first (for the admin UI)."""
    return list(
        (await session.execute(select(SourceDomain).order_by(SourceDomain.updated_at.desc())))
        .scalars()
        .all()
    )


async def set_override(
    session: AsyncSession, *, domain: str, override: str, now: datetime
) -> SourceDomain:
    """Set (or clear, via `none`) the admin override for a domain, creating the row if the
    domain has never been seen. The domain is lowercased. Caller commits."""
    key = domain.strip().lower()
    row = await session.get(SourceDomain, key)
    if row is None:
        row = SourceDomain(domain=key, admin_override=override, first_seen_at=now, updated_at=now)
        session.add(row)
    else:
        row.admin_override = override
        row.updated_at = now
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# LLM domain judge
# ---------------------------------------------------------------------------

_JUDGE_MAX_TOKENS = 1024
_JUDGE_INSTRUCTIONS = """You are a source-quality rater for an upcoming-movies news tracker. \
You are given a JSON array of news source domains, each with one sample headline seen from \
that domain. Rate each domain's reliability for *movie/entertainment production news* into \
exactly one tier:

- "trusted": established trade or major outlet with editorial standards (e.g. variety.com, \
deadline.com, hollywoodreporter.com, apnews.com).
- "acceptable": a real outlet or reputable enthusiast site that is not a top-tier trade but \
is generally reliable for entertainment news.
- "low": content farms, SEO aggregators, auto-reposters, machine-translated scrapers, \
link-spam, or sites with no discernible editorial standard.

Judge by the domain's reputation; the sample headline is only context. Return ONLY a JSON \
array — no prose, no markdown — one object per input domain:
[{"domain": "<domain>", "tier": "trusted" | "acceptable" | "low", "reason": "<short reason>"}]"""


def _extract_json_array(text: str) -> str:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def build_judge_request(
    items: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """System + user blocks for the domain judge. Plain (uncached) system block: the
    instructions are well under every model's cache floor."""
    system = [{"type": "text", "text": _JUDGE_INSTRUCTIONS}]
    messages = [{"role": "user", "content": json.dumps(items)}]
    return system, messages


def parse_judge_verdicts(raw: str, *, domains: set[str]) -> dict[str, tuple[str, str]]:
    """Parse the judge's JSON array into `{domain: (tier, reason)}`. Keeps only entries whose
    domain was actually asked about and whose tier is valid; last write wins on duplicates.
    Returns `{}` on unparseable output."""
    try:
        decisions = json.loads(_extract_json_array(raw))
    except json.JSONDecodeError:
        return {}
    if not isinstance(decisions, list):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for d in decisions:
        if not isinstance(d, dict):
            continue
        domain = d.get("domain")
        tier = d.get("tier")
        if domain in domains and tier in VALID_TIERS:
            out[domain] = (tier, str(d.get("reason") or ""))
    return out


async def judge_domains(
    *, client: Completer, model: str, items: list[dict[str, str]]
) -> tuple[dict[str, tuple[str, str]], Usage]:
    """Judge a batch of unknown domains in one LLM call. Returns `{domain: (tier, reason)}`
    plus token usage. No-op (`{}`, empty Usage) for empty input."""
    if not items:
        return {}, Usage()
    system, messages = build_judge_request(items)
    raw, usage = await client.complete_with_usage(
        model=model, system=system, messages=messages, max_tokens=_JUDGE_MAX_TOKENS
    )
    domains = {item["domain"] for item in items}
    return parse_judge_verdicts(raw, domains=domains), usage
