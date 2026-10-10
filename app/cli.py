"""Command line entry points, for cron-based hosting and local testing.

    python -m app.cli collect daily              # see what the collectors find (no API calls)
    python -m app.cli run daily --no-send        # build an issue, don't email it
    python -m app.cli run weekly                 # build and send
    python -m app.cli send 3 --to me@example.com # send edition 3 to one address
    python -m app.cli preview 3                  # write edition 3 to outbox/ for viewing
    python -m app.cli add me@example.com --cadence daily
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from .categories import CATEGORY_KEYS
from .config import settings
from .db import Edition, Subscriber, init_db, session_scope, utcnow
from .pipeline import WINDOW_HOURS, build_edition, deliver_edition, run_issue
from .render import render_issue
from .sources.collect import collect_candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="run the collectors only and print candidates")
    p.add_argument("cadence", choices=["daily", "weekly"])

    p = sub.add_parser("run", help="build (and by default send) today's issue")
    p.add_argument("cadence", choices=["daily", "weekly"])
    p.add_argument("--no-send", action="store_true")
    p.add_argument("--force", action="store_true", help="rebuild even if today's issue exists")

    p = sub.add_parser("send", help="send an existing edition")
    p.add_argument("edition_id", type=int)
    p.add_argument("--to", help="send only to this address (test send)")

    p = sub.add_parser("preview", help="write an edition's HTML and text to outbox/")
    p.add_argument("edition_id", type=int)

    p = sub.add_parser("add", help="add a confirmed subscriber (for testing)")
    p.add_argument("email")
    p.add_argument("--cadence", choices=["daily", "weekly"], default="daily")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()

    if args.cmd == "collect":
        candidates, stats = collect_candidates(WINDOW_HOURS[args.cadence])
        for c in candidates:
            extra = f" (+{', '.join(sorted(set(c.also_covered_by)))})" if c.also_covered_by else ""
            print(f"{c.id:>5} {c.rank_score:5.2f} {c.source:<22} {c.title[:90]}{extra}")
        print(stats)
    elif args.cmd == "run":
        if args.no_send:
            edition_id = build_edition(args.cadence, force=args.force)
        else:
            edition_id = run_issue(args.cadence, force=args.force)
        print(f"edition id: {edition_id}" if edition_id else "nothing to do (issue already exists; use --force)")
        if edition_id:
            _preview(edition_id)
    elif args.cmd == "send":
        if args.to:
            with session_scope() as s:
                if s.scalar(select(Subscriber).where(Subscriber.email == args.to.lower())) is None:
                    s.add(Subscriber(email=args.to.lower(), confirmed=False, active=False))
        print(f"sent {deliver_edition(args.edition_id, only_email=args.to.lower() if args.to else None)}")
    elif args.cmd == "preview":
        _preview(args.edition_id)
    elif args.cmd == "add":
        with session_scope() as s:
            existing = s.scalar(select(Subscriber).where(Subscriber.email == args.email.lower()))
            sub_row = existing or Subscriber(email=args.email.lower(), categories=list(CATEGORY_KEYS))
            sub_row.cadence, sub_row.confirmed, sub_row.active, sub_row.confirmed_at = args.cadence, True, True, utcnow()
            s.add(sub_row)
        print(f"{args.email} subscribed ({args.cadence})")
    return 0


def _preview(edition_id: int) -> None:
    with session_scope() as s:
        e = s.get(Edition, edition_id)
        if e is None or e.digest_json is None:
            print("no digest for that edition")
            return
        _, html, text = render_issue(e.digest, cadence=e.cadence, issue_date=e.issue_date, issue_number=e.issue_number)
    settings.outbox_dir.mkdir(exist_ok=True)
    base = settings.outbox_dir / f"edition-{edition_id}"
    base.with_suffix(".html").write_text(html, encoding="utf-8")
    base.with_suffix(".txt").write_text(text, encoding="utf-8")
    print(f"preview written to {base}.html and {base}.txt")


if __name__ == "__main__":
    sys.exit(main())
