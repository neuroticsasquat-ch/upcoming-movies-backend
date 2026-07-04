"""One-off: retroactively drop stories/events from admin-blocked source domains.

The live source-quality gate only hard-drops blocked stories on the next ingest run, and only
while they are still unclustered; stories already clustered into an event before their domain
was blocked are never revisited. Run this after blocking domains in the admin Sources page to
clean up what is already on the site. Dry-run by default — pass --apply to commit.

    task shell
    python scripts/cleanup_blocked_sources.py            # report only, changes nothing
    python scripts/cleanup_blocked_sources.py --apply     # commit the cleanup
"""

import argparse
import asyncio

from upmovies.db import SessionLocal
from upmovies.link.blocked_cleanup import cleanup_blocked_sources


async def main(*, apply: bool) -> None:
    async with SessionLocal() as session:
        report = await cleanup_blocked_sources(session, apply=apply)
        if apply:
            await session.commit()

    mode = "APPLIED" if apply else "DRY-RUN (no changes)"
    print(f"cleanup_blocked_sources [{mode}]")
    print(f"  blocked domains among linked stories: {len(report.blocked_domains)}")
    for domain in report.blocked_domains:
        print(f"    - {domain}")
    print(f"  stories rejected:      {report.stories_rejected}")
    print(f"  events deleted:        {report.events_deleted}")
    print(f"  events re-summarized:  {report.events_resummarized}")
    if not apply:
        print("  (re-run with --apply to commit)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit changes (default: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
