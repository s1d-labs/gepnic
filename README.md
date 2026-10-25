# gepnic: the GePNIC family of s1d-labs

Everything specific to GePNIC portals lives here: the crawler, its pool settings, and (soon) its own web app and jobs. The hub
([s1d-labs/scrape-hub](https://github.com/s1d-labs/scrape-hub)) is generic: it runs the pool, signs uploads, tracks runners, IPs,
requests and hosts, and knows nothing about tenders.

- `scrape.json`: how the pool runs this family (setup steps and one command per task). The generic worker in
  [s1d-labs/runner](https://github.com/s1d-labs/runner) checks this repo out at `main` for every pooled run.
- `pool_task.py`: the pool's entry point (checks the portal, then starts `crawl.py` or `count.py`).
- `pool_settings.py`: each project's pool settings (task, target, env, probe) for the hub.
- `probe_settings.py`: the health checks the hub runs for GePNIC (`POST /api/families/gepnic/probes`).
- `app/`: the GePNIC app (`gepnic_app`, FastAPI): the pages under `/gepnic/` (overview, portals, data and quality, tender lookup,
  bundles, throttle, journal), the public `/bundle/<slug>.zip` links, and the crawler's runner extras (`known-records`,
  `window-stored`, `backfill/todo`, `patch-records`). It runs as its own service behind scrape-hub, which forwards to it and signs who
  is asking (s1d-labs/scrape-hub `docs/family-apps.md`). `app/schema.sql` = GePNIC's own tables in the scrape_hub database.
  Tests: `cd app && python -m pytest tests` (needs `pgserver`). Deployed on dev-vps-2 by `~/setup/79-gepnic-app.sh` (127.0.0.1:9332).
- `jobs/`: GePNIC's batch jobs: tender bundles (`bundler.py`, `bundlectl.py`, `bundle`), the data and quality numbers
  (`build_showcase.py`, `build_quality.py`, `ops/*.sql`), the bid index (`ops/bid_index.py`), the portal capacity ramp (`ramp.py`),
  queue balancing (`bucket_queue.py`) and one-off ops scripts (`ops/`).
- `deploy/`: units for those jobs (`gepnic-*.service/.timer`, not installed: nightly rebuilds are off) and the B2 relay worker.
- `data/gepnic_portals.json`: the portal registry (one copy, used by the crawler, the app and the jobs).
- `.github/workflows/gepnic-*.yml`: the manual workflows (plan, crawl, backfill, consolidate, portals).

Owner: sarthak (the gepnic family owner on scrape.s1d.me).

---

## History: eprocure-scrape

Scrapes tenders from [eprocure.gov.in](https://eprocure.gov.in) (GePNIC), 2011 to 2026, in GitHub Actions and reports to
[scrape-hub](https://github.com/Sarthak-Sidhant/scrape-hub), which stores the records in Postgres and shows the live dashboard
at https://scrape.s1d.me. This repo holds only the scraping code; the hub never contains scraper logic.

## The idea: plan by window, then scrape

Scraping one organisation as a single unit leaves the biggest ones (one org-year has 36,000 tenders) pinned to one runner. So the
work is split into **windows** (organisation + date range) first.

1. **Plan** (`plan.py`, then the *Plan* workflow). `data/org_year_counts.json` holds tenders per organisation per year (894,668
   tenders, 2011 to 2026, 733 organisations). An org-year with at most 600 tenders becomes one scrape window. A bigger one gets
   **12 count tasks, one per month**. A count task is one portal search (the results footer says "Total records: N"); if the
   month is still over 1,200 tenders it is **halved** until every window fits. Every count is stored as a record in the hub project
   `eprocure-plan`, which is the tenders-per-window table.
2. **Scrape** (`crawl.py`, the *Scrape* workflow). Each runner claims windows from `eprocure-tenders`, lists the window, fetches each
   tender's status, details and summary pages, and uploads one record per tender (the record carries its window). A runner that
   runs out of its own share takes windows from other slices, so no runner idles while others still have work.

The portal's organisation ids shift when organisations are added, so ids are re-keyed by name against the live dropdown when seeding,
and a window whose organisation label no longer matches is failed rather than scraped.

## Run it

Actions tab:
1. **Plan eprocure windows**: counts the tenders and builds the scrape queue. Smoke test: `max_items=5`. Watch `eprocure-plan` on the
   dashboard; when it finishes, `eprocure-tenders` shows exactly how many windows and tenders will be scraped.
2. **Scrape eprocure windows**: the actual scrape. Smoke test: `total_jobs=2`, `max_items=2`, `max_tenders=5`.

Speed, pause and auto-backoff are set on the dashboard, not here.

## What is stored, and where

Per tender, in the hub's Postgres (`records`): the search-result row, the window it came from, the bids table, the list of its
documents (name, kind, size the portal states, and whether it was stored, refused or failed), and the path of its raw pages.

Per tender, in object storage (Cloudflare R2 bucket `eprocure-docs`, folder `eprocure-tenders/`):
- `pages/<year>/<org>/<tender_id>.json.gz`: the status, details and summary pages as received, gzipped in one file. Every field
  can be parsed from these later without scraping again.
- `docs/<year>/<org>/<tender_id>/<file>`: each document the portal serves (evaluation summaries, corrigenda, the BOQ chart as
  `.xlsx`). Files are identified by their real bytes, not their names, and checked against the size the portal states.

Runners never hold storage keys. They ask the hub for a one-time upload link per file; the hub only agrees to runners it has
verified through GitHub OIDC, and only under this project's folder. Some tenders' documents are refused by the portal (it returns
a "session expired" page); those are recorded as `refused`, not stored.

The summary page also links a certificate icon per document (a digital-signature pop-up). That is not a document and is skipped.

## Add another scraper

Make a new repo with its own workflow that has `permissions: id-token: write`, install `scrape-hub-client`, then:

```python
from scrape_hub_client import HubClient, register_project, seed_queue
register_project("my-project", title="...", unit_label="page", record_label="row")
seed_queue("my-project", [{"key": "1", "payload": {"expected": 100}}])
hub = HubClient("my-project", job_index=0, total_jobs=1)
for item in hub.claim(1, steal=True):
    ...                                  # scrape
    hub.record("row-id", {...})
    hub.metric("captcha_ok")
    hub.complete([item["key"]], "done", fetched=100)
hub.close()
```

The first repo to use a project name owns it; other repos get a 403 for it.

## Develop

`pip install -r requirements.txt pytest && pytest` (no network in the tests).
