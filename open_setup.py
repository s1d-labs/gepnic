#!/usr/bin/env python3
"""Open road tenders (open_tenders.py): register each portal's gepnic-<slug>-open project, put it in the pool, and queue today's poll.

    python open_setup.py [--portals a.gov.in,b.gov.in | --portals all] [--setup] [--dry-run]

Without --portals: the portals in ENABLED (the daily workflow). "all": every classic GePNIC state and UT portal in
data/gepnic_portals.json. --setup also registers the projects and writes
their pool settings (task "open", the portal's own target and health check, 2 workers, 1 thread, 2 s between requests); without it only
"poll-<today>" is queued. Queue items that exist are kept, so a second run on the same day does nothing. The hub creates a project on its
first queue item, so a portal joins the daily run by being added to ENABLED after its --setup run.
"""
import argparse
import datetime
import json
import os
import re
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
KINDS = ("state", "ut")
ENABLED = ["uktenders.gov.in", "hptenders.gov.in"]      # pilot 2026-11-13


def slug(name):
    s = re.sub(r"\.(gov|nic|co)\.in", "", name.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def portals(want):
    reg = json.load(open(os.path.join(HERE, "data", "gepnic_portals.json")))["portals"]
    shared = Counter(p.get("server_ip") for p in reg)
    for p in reg:
        if p.get("ui") == "gepnic2" or p["portal"].startswith("eprocure.gov.in"):
            continue
        if (want and p["portal"] not in want) or (not want and p.get("kind") not in KINDS):
            continue
        target = p["server_ip"] if p.get("server_ip") and shared[p["server_ip"]] > 1 else p["portal"]
        yield p, f"gepnic-{slug(p['portal'])}-open", target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--portals", default="", help="comma-separated portals; empty = ENABLED; all = every state and UT portal")
    ap.add_argument("--setup", action="store_true", help="also register the projects and write their pool settings")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    want = set() if a.portals.strip() == "all" else {x.strip() for x in (a.portals or ",".join(ENABLED)).split(",") if x.strip()}
    today = datetime.date.today().isoformat()
    if not a.dry_run:
        from scrape_hub_client import register_project, seed_queue, set_pool
    for p, project, target in portals(want):
        if a.dry_run:
            print(project, target, f"poll-{today}")
            continue
        if a.setup:
            register_project(project, title=f"{p['title']}: open road tenders (pages, NIT where open)", unit_label="poll or tender",
                             record_label="tender")
            set_pool(project, "gepnic", "open", target=target, env={"PORTAL": p["portal"]}, probe=f"gepnic:{p['portal']}", max_workers=2,
                     threads=1, delay_ms=2000)
        print(project, seed_queue(project, [{"key": f"poll-{today}", "payload": {}}]), flush=True)


if __name__ == "__main__":
    main()
