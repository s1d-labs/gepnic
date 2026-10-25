"""What the s1d-labs pool worker runs for GePNIC (see scrape.json): checks the assignment, then starts crawl.py or count.py.

    PORTAL=wbtenders.gov.in python pool_task.py crawl [extra args]

The worker sets SCRAPE_JOB_INDEX, SCRAPE_TOTAL_JOBS and SCRAPE_MAX_HOURS; the hub's assignment gives PORTAL (written into each
project's settings by pool_settings.py). The scripts derive the hub project (gepnic-<slug>, gepnic-<slug>-plan, or eprocure-* for
eprocure.gov.in) from PORTAL.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = {"crawl": "crawl.py", "count": "count.py"}


def main(argv):
    task = argv[0] if argv else ""
    script = SCRIPTS.get(task)
    if not script:
        print(f"gepnic: unknown task {task!r} (expected one of {sorted(SCRIPTS)})", flush=True)
        return 2
    portal = os.environ.get("PORTAL", "")
    known = {p["portal"]: p for p in json.loads((HERE / "data" / "gepnic_portals.json").read_text())["portals"]}
    if portal not in known:
        print(f"gepnic: portal {portal!r} is not in data/gepnic_portals.json (this checkout may be older than the hub's settings)", flush=True)
        return 2
    if known[portal].get("ui") == "gepnic2":
        print(f"gepnic: {portal} runs GePNIC 2.0, which these scripts do not speak", flush=True)
        return 2
    cmd = [sys.executable, script, "--job-index", os.environ.get("SCRAPE_JOB_INDEX", "0"),
           "--total-jobs", os.environ.get("SCRAPE_TOTAL_JOBS", "1"), "--max-hours", os.environ.get("SCRAPE_MAX_HOURS", "1"), *argv[1:]]
    print(f"gepnic: {task} on {portal}: {' '.join(cmd[1:])}", flush=True)
    os.chdir(HERE)
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
