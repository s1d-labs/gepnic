# CLAUDE.md: the GePNIC family (s1d-labs scrape pool)

This repo is the **gepnic** family of the s1d-labs scrape pool: everything specific to NIC's GePNIC tender portals (~45 state, central
and PSU sites). scrape-hub (https://scrape.s1d.me, repo s1d-labs/scrape-hub) is generic and knows nothing about tenders; it hands out
queue items, limits load per site, signs uploads and tracks requests. The pool runner is s1d-labs/runner. The general contract every
family follows (scrape.json, env vars, the HubClient API, metrics, politeness) is in s1d-labs/family-template's CLAUDE.md and on
home.s1d.me → Docs → Scraper guide. This file covers what is particular to GePNIC.

## Layout

| Path | What |
|---|---|
| `scrape.json` | pool manifest: tasks `crawl` and `count`, both run `pool_task.py` |
| `pool_task.py` | pool entry point: checks PORTAL is a known, classic-GePNIC portal, then execs `crawl.py` / `count.py` |
| `portal.py` | which portal (env `PORTAL`, default eprocure.gov.in), its URLs and hub project names (`gepnic-<slug>`, `gepnic-<slug>-plan`; eprocure.gov.in keeps `eprocure-tenders` / `eprocure-plan`) |
| `crawl.py` | claims windows (organisation + date range), lists them, fetches each tender's pages and documents, stores one record per tender |
| `count.py` | count tasks: one portal search per window, stores the count, queues the window for crawling or halves it |
| `plan.py`, `plan_new.py` | seed count tasks: full history (`plan.py`) or since a date (`plan_new.py`, workflow "plan new tenders") |
| `pool_settings.py`, `probe_settings.py` | the pool entries and health checks this family gives the hub (one per portal) |
| `data/gepnic_portals.json` | the portal registry: URL, server IP, UI version, June 2026 counts |
| `app/` | the GePNIC app (FastAPI, `gepnic-app.service` on the hub box, 127.0.0.1:9332): pages under `/gepnic/…` and the crawler's hub extras (`/hub/runs/{id}/known-records`, `window-stored`, `backfill/todo`, `patch-records`), reached through scrape-hub, signed per s1d-labs/scrape-hub `docs/family-apps.md`. DB role `gepnic_app` (reads all, writes only bundles/counters/record patches). Tests: `app/tests` |
| `jobs/`, `deploy/` | GePNIC's own batch jobs (bundles, showcase, quality, capacity ramp, ops scripts) and their units; not installed (owner: nightly rebuilds off) |
| `.github/workflows/gepnic-*.yml` | manual workflows (plan, crawl, consolidate, backfill, portals, plan new tenders) |

## Things to know

- **Organisation ids shift.** The portals renumber organisations when new ones are added. Queue items carry the id and label at
  seed time; `crawl.py` refuses a window whose id now points elsewhere ("reseed needed"). Re-plan from today's dropdown instead of
  patching ids.
- **Skip what is stored.** `crawl.py` asks `hub.window_stored` (whole window complete?) and `hub.known` (which tender ids are stored)
  before fetching; both go through the hub to the GePNIC app. Without them reruns re-download everything.
- **Captchas** are solved by the CNN in `captcha/` (torch CPU wheel, installed by `scrape.json` setup). Sessions expire; `crawl.py`
  re-logs in and counts `sessions_new` / `sessions_renewed`.
- **Politeness** is the hub's: per-portal worker caps, health checks `gepnic:<portal>` (a portal that is down holds its work), and
  the automatic throttle on the crawl projects (`settings.adaptive`). Report `n_http`, `t_http_s`, `http_error`, `http_<code>`.
- **eprocure.gov.in** (CPPP) is the legacy portal: planned from saved yearly counts (`plan.py` without PORTAL), its own project names.
  GePNIC 2.0 portals (`ui: gepnic2`, e.g. Manipur) are not supported by this crawler.
- **Files**: tender pages and documents go to the hub's buckets (`settings.storage` b2/r2 per project; the family keeps the legacy
  buckets) through signed upload links, so the (public) repo needs no storage keys.
- The family owner is sarthak. Changes to crawl/count behaviour affect live crawls; `--max-items`, `--max-tenders` and a single
  portal make quick trials.

## India pool servers (`geo`)

Many Indian government sites (most `.gov.in` / `.nic.in`) refuse requests from outside India. GitHub runners are in the US/EU and the
team boxes are in Germany, so those sites time out or answer 403 there.

- `set_pool(..., geo="IN")` in `setup_family.py` sends that project's work **only** to the hub's pool servers in India (`vpsin`,
  `vpsin2`: 10 workers on 5 Indian IPs); GitHub runners never get it. Projects without `geo` run only on GitHub runners.
  `geo=""` moves a project back. Run the **Setup** workflow after changing it. India capacity is small (10 workers against a few
  hundred GitHub ones), so `geo` suits the sites that need it.
- On a pool server a task runs the same way as on GitHub: a checkout of this repo at `main` (fetched again every 5 minutes) in its own
  venv with `scrape-hub-client`, `setup` from `scrape.json` re-run when the commit changes, the same `SCRAPE_*` env vars, and the
  client signs in with the server's token. No code changes.
- It runs as an unprivileged user without `sudo`: `playwright install --with-deps chromium` runs without `--with-deps` (Chromium's
  libraries are already there); other setup steps that need root won't work there. Per worker: 1.5 GB RAM, 1.5 CPU, a restart every
  6 hours. The servers reach the internet and scrape.s1d.me only.
- Testing: an India-only site times out from a team box, so parsing is tested offline with saved pages (`tests/pages/` +
  `tests/test_parse.py`). After Setup, scrape.s1d.me → Runners → *Working now* shows the India servers on the project, and its runs
  are listed under `server/vpsin` / `server/vpsin2`.
- Full page: home.s1d.me/docs/15-india-servers (in team boxes: `/usr/local/share/team-docs/15-india-servers.md`).

## Proxies (`proxy="IN"`)

The hub has 10 static proxies in Mumbai (10 Indian IPs). Sites that refuse foreign visitors (most `.gov.in` / `.nic.in`) answer
through them. They are the first choice for India-only sites: the work runs on the pool's GitHub runners, which have far more
capacity than the India servers.

- `set_pool(..., proxy="IN")` in `setup_family.py` (then run the **Setup** workflow): GitHub pool workers take that project's work and
  **each worker borrows one proxy** for its assignment (one worker per IP). With no proxy free the project waits; the hub starts no
  more runs than there are free proxies. A family borrows at most 4 at once by default (the hub owner changes it per family).
  `proxy=""` turns it off. `geo="IN"` and `proxy="IN"` together: both the India servers and proxied GitHub workers take the work.
- The task gets the proxy in `HTTPS_PROXY` / `HTTP_PROXY` (requests, httpx, urllib3, curl, pip use them by themselves) and in
  `SCRAPE_PROXY` for code that sets a proxy explicitly; `SCRAPE_PROXY_LABEL` / `SCRAPE_PROXY_EXIT` name it. Calls to scrape.s1d.me
  and GitHub go direct, and the URL is masked in the GitHub log. Playwright takes the login separately:
  ```python
  from urllib.parse import urlsplit, unquote
  u = urlsplit(os.environ["SCRAPE_PROXY"])
  browser = p.chromium.launch(proxy={"server": f"http://{u.hostname}:{u.port}", "username": unquote(u.username), "password": unquote(u.password)})
  ```
- From a team box (in Germany, where India-only sites time out): `myproxy run CMD…` runs one command through a proxy,
  `myproxy curl URL` tests a site, `eval "$(myproxy env)"` sets the variables in the shell, `myproxy release` gives it back
  (2 hours by default, one per member). `myproxy run scrape-check --items 2` checks a scraper end to end through an Indian IP.
- Each proxy IP is one visitor to the site: the hub's speed limits still apply, and 429 / 403 answers slow the project down.
  scrape.s1d.me → Runners → Proxies shows each proxy's health, who has it and its use.
- Full page: home.s1d.me/docs/17-proxies (in team boxes: `/usr/local/share/team-docs/17-proxies.md`).

## Speed (`threads`, `delay_ms`)

Each project's speed is `threads` per worker process (1-32) and `delay_ms` between requests, handed to the task in `hub.settings`.
The family owner chooses it:

- from code: `set_pool(..., threads=8, delay_ms=0)` in `setup_family.py`, then run the **Setup** workflow (running Setup again sets
  it back to what the code says);
- on scrape.s1d.me: the project's **Speed** buttons, or **Exact** (type the threads and the delay, press Enter); runners pick it up
  within about 30 seconds.

The hub's back-off and automatic throttle may lower it for a while when the site answers 429 / 403 / 503 or slows down; it returns by
itself. More threads means more load on the site.

New families: any member adds one on scrape.s1d.me → Runners → Families → Add a family (they become its owner; the hub makes
`s1d-labs/<name>` from the template with them as admin and clones it into their box at `~/work/<name>`).
