"""One-off: retroactively drop the Google-News backlog after NEU-717 paused Google ingestion.

Turning Google off (`NEWS_GOOGLE_ENABLED=false`) stops new Google stories, but earlier ones
remain in the DB: `linked` ones still show on the site, and `pending` ones would be clustered
by the next link run. This rejects both (stamping `link_note = "google-paused"`), deletes
events left with no surviving source, and re-summarizes mixed events. Dry-run by default —
pass --apply to commit.

    task shell
    python scripts/cleanup_google_sources.py            # report only, changes nothing
    python scripts/cleanup_google_sources.py --apply     # commit the cleanup
"""

import argparse
import asyncio

from upmovies.db import SessionLocal
from upmovies.link.google_cleanup import cleanup_google_sources


async def main(*, apply: bool) -> None:
    async with SessionLocal() as session:
        report = await cleanup_google_sources(session, apply=apply)
        if apply:
            await session.commit()

    mode = "APPLIED" if apply else "DRY-RUN (no changes)"
    print(f"cleanup_google_sources [{mode}]")
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
