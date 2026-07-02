"""Backfill news.source_domain by judging every already-resolved publisher domain once.

Fixes NEU-460: the live source-judge stage produced zero rows (a single over-capped judge
call truncated and inserted nothing), so the admin Sources page is empty. Run once after the
judge fix deploys; it is idempotent.

    task shell
    python scripts/backfill_source_domains.py"""

import asyncio

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.source_stage import backfill_source_domains
from upmovies.llm.client import AnthropicClient


async def main() -> None:
    settings = get_settings()
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        judged = await backfill_source_domains(
            session_factory=SessionLocal,
            client=client,
            judge_model=settings.source_judge_model,
        )
    print(f"backfill_source_domains: judged {judged} new domains")


if __name__ == "__main__":
    asyncio.run(main())
