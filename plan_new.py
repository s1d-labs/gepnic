#!/usr/bin/env python3
"""Queue counts for the tenders published since a portal's last crawl: one count task per organisation in today's dropdown, over
[--since, today]. count.py then counts each window and queues it for crawling (halving it until it fits); the crawler skips tenders
the hub already holds, so the overlap with the last crawl costs a check, not a download. Using today's dropdown also re-keys
organisations whose ids shifted since the last plan.

  PORTAL=wbtenders.gov.in python plan_new.py [--since 17/10/2026] [--to DD/MM/YYYY] [--dry-run]

Safe to re-run: existing queue items are kept. Skips eprocure.gov.in (planned from saved yearly counts: plan.py).
"""
import argparse
import datetime

import portal

if portal.LEGACY:
    raise SystemExit(f"{portal.NAME}: planned with plan.py (saved yearly counts), not here")

from scrape_hub_client import register_project, seed_queue  # noqa: E402

import orgs  # noqa: E402
import windows  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="17/10/2026", help="DD/MM/YYYY: start of the window (default: two days before the October crawl)")
    ap.add_argument("--to", default=datetime.date.today().strftime("%d/%m/%Y"), help="DD/MM/YYYY, default today")
    ap.add_argument("--chunk-max", type=int, default=1200)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    live = orgs.live_options()
    items = [windows.count_item(oid, label, a.since, a.to, a.chunk_max) for oid, label in sorted(live.items(), key=lambda kv: int(kv[0]))]
    print(f"{portal.NAME}: {len(live)} organisations, {len(items)} count tasks over {a.since} - {a.to}")
    if a.dry_run:
        return
    register_project(portal.PLAN, title=f"{portal.TITLE} count plan", unit_label="count task", record_label="count")
    print(portal.NAME, "count queue:", seed_queue(portal.PLAN, items))


if __name__ == "__main__":
    main()
