"""The GePNIC app: portals, tenders, data quality, bundles and the throttle's story, under /gepnic/ (served through scrape-hub).

Moved out of scrape-hub on 2026-10-25 (the hub is generic now). Every request arrives through the hub, signed (hubauth.py):
pages and APIs need a signed-in person; viewers only read; members may make bundles; deleting one takes the family owner.
The public /bundle/<slug>.zip links ask for that bundle's own password. Runner extras of the GePNIC crawler (known-records,
window-stored, backfill/todo, patch-records) arrive at /hub/runs/{run_id}/... after the hub checked the run.
"""
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import bundles, config, db, fleet, hubauth, s3, scrapes, storage

WEB = Path(__file__).resolve().parent / "web"
HUB_NAV = [("/", "Overview"), ("/runners", "Runners"), ("/runs", "Runs"), ("/explore", "Rate explorer"), ("/gepnic", "GePNIC app")]
GNAV = [("/gepnic", "GePNIC"), ("/gepnic/portals", "Portals"), ("/gepnic/data", "Data"), ("/gepnic/tender", "Tender lookup"),
        ("/gepnic/bundles", "Bundles"), ("/gepnic/throttle", "Throttle"), ("/gepnic/journal", "Journal")]


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _counts_loop():
    """Keep record_counts fresh (the overview's per-project record counts): one worker at a time, at most every few minutes."""
    while True:
        try:
            with db.conn() as c:
                scrapes.refresh_counts(c)
        except Exception as e:  # noqa: BLE001 - keep trying; the page falls back to the last counts
            print(f"record counts: {type(e).__name__}: {e}", flush=True)
        time.sleep(60)


@asynccontextmanager
async def lifespan(_app):
    db.init_schema()
    if not os.environ.get("GEPNIC_NO_COUNTER"):     # tests switch the background recount off (it would race their TRUNCATE)
        threading.Thread(target=_counts_loop, daemon=True).start()
    yield
    db.close()


app = FastAPI(title="gepnic-app", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def with_gnav(html: str, current: str = "") -> str:
    """Links back to the hub, then this app's own menu, in place of the page's <!--NAV--> marker."""
    a = lambda items, cur: "".join(f'<a href="{h}"{' aria-current="page"' if h == cur else ''}>{l}</a>' for h, l in items)
    return html.replace("<!--NAV-->", f'<nav class="nav">{a(HUB_NAV, "/gepnic")}<a href="/logout" class="out">Sign out</a></nav>'
                                      f'<nav class="nav sub">{a(GNAV, current)}</nav>')


def gpage(name: str, current: str) -> str:
    return with_gnav((WEB / name).read_text(), current)


@app.get("/healthz")
def healthz():
    with db.conn() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/gepnic", response_class=HTMLResponse)
def gepnic_overview(request: Request):
    """Every GePNIC source: counted, crawled, left, speed, storage and where it is heading."""
    hubauth.need_user(request)
    return gpage("overview.html", "/gepnic")


@app.get("/gepnic/throttle", response_class=HTMLResponse)
def throttle_page(request: Request):
    hubauth.need_user(request)
    return gpage("throttle.html", "/gepnic/throttle")


@app.get("/gepnic/api/scrapes", dependencies=[Depends(hubauth.need_user)])
def scrapes_api():
    """GePNIC's scrapes as families of sources (scrapes.py), with the hub machine's own limits."""
    with db.conn() as c:
        return JSONResponse(json.loads(json.dumps(scrapes.cached(c), default=_iso)))


@app.get("/gepnic/api/traffic", dependencies=[Depends(hubauth.need_user)])
def traffic_api(minutes: int = 0):
    """Requests, documents, logins and data our runners send to and pull from the portals, per minute (see scrapes.traffic*)."""
    with db.conn() as c:
        data = scrapes.traffic_range(c, minutes) if minutes else scrapes.traffic(c)
    return JSONResponse(json.loads(json.dumps(data, default=_iso)))


@app.get("/gepnic/api/throttle", dependencies=[Depends(hubauth.need_user)])
def throttle_api(hours: int = 24):
    """The throttle's decisions per source over the last `hours`: windows and threads after each decision, cap changes, outages."""
    with db.conn() as c:
        return JSONResponse(json.loads(json.dumps(scrapes.throttle_history(c, hours), default=_iso)))


@app.get("/gepnic/api/adjustments", dependencies=[Depends(hubauth.need_user)])
def adjustments_api(limit: int = 60, caps: bool = False):
    """The throttle's latest decisions on GePNIC projects, newest first; caps=1 keeps only cap changes."""
    with db.conn() as c:
        return JSONResponse(json.loads(json.dumps({"items": scrapes.adjustments(c, max(1, min(limit, 300)), caps)}, default=_iso)))


_storage_cache = {"at": 0.0, "data": None}


@app.get("/gepnic/api/storage", dependencies=[Depends(hubauth.need_user)])
def storage_api():
    """Database and object storage: size now, growth, cost, and where they are heading with the records still to save (cached a minute)."""
    if not _storage_cache["data"] or time.time() - _storage_cache["at"] > 60:
        with db.conn() as c:
            left = {}
            for s in scrapes.cached(c)["sources"]:
                if s["status"] not in ("parked", "done"):
                    left[s["family"]] = left.get(s["family"], 0) + s["left"]
            _storage_cache["data"] = storage.report(c, left)
            _storage_cache["at"] = time.time()
    return JSONResponse(json.loads(json.dumps(_storage_cache["data"], default=_iso)))


REPORTS = Path(config.REPORTS_DIR)


@app.get("/gepnic/journal", response_class=HTMLResponse)
def gepnic_journal(request: Request):
    """The journal of the GePNIC crawl (Oct 2026): a hand-written page kept on the server only (web/reports/gepnic.html, not in git, because it
    quotes the operator's prompts), served behind the dashboard login with the shared navigation."""
    hubauth.need_user(request)
    if not (REPORTS / "gepnic.html").is_file():
        raise HTTPException(404, "no journal on this server")
    return with_gnav((REPORTS / "gepnic.html").read_text(), "/gepnic/journal")


@app.get("/reports/{name}", dependencies=[Depends(hubauth.need_user)])
def report_file(name: str):
    """Charts and exports saved by hand under web/reports/, served behind the dashboard login."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}\.(png|csv|html)", name) or not (REPORTS / name).is_file():
        raise HTTPException(404, "no such report")
    return FileResponse(REPORTS / name)


@app.get("/report")
def report_latest(request: Request):
    """Short link to the current report (the Punjab data QA), behind the dashboard login."""
    hubauth.need_user(request)
    return RedirectResponse("/reports/punjab-qa.html", 303)

@app.get("/gepnic/tender", response_class=HTMLResponse)
def tender_page(request: Request):
    hubauth.need_user(request)
    return gpage("tender.html", "/gepnic/tender")


@app.get("/gepnic/api/tender", dependencies=[Depends(hubauth.need_user)])
def tender_lookup(q: str = "", project: str = ""):
    """Find a stored tender by the start of its id (e.g. 2025_DRDO_875304) and return what the hub holds about it: the parsed
    fields, the document lists parsed from the details page, and the status of every document file that was downloaded."""
    q = q.strip()[:80]
    if len(q) < 4:
        return JSONResponse({"matches": [], "tender": None})
    with db.conn() as c:
        if not project:
            row = c.execute("SELECT name FROM projects WHERE name LIKE %s ORDER BY created_at LIMIT 1", ("%tenders",)).fetchone()
            project = row["name"] if row else ""
        like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        ids = [r["record_id"] for r in c.execute("SELECT record_id FROM records WHERE project=%s AND record_id LIKE %s ORDER BY record_id LIMIT 20", (project, like)).fetchall()]
        tender = None
        if ids:
            rid = q if q in ids else ids[0]
            row = c.execute("SELECT record_id, data, scraped_at FROM records WHERE project=%s AND record_id=%s", (project, rid)).fetchone()
            d = row["data"] or {}
            det = d.get("details") or {}
            f = det.get("fields") or {}
            keep = ("tender_title", "tender_ref_no", "tender_type", "tender_category", "published_date", "bid_opening_date", "tender_value", "emd_amount", "stage", "location")
            tender = {"record_id": row["record_id"], "org_name": d.get("org_name"), "scraped_at": row["scraped_at"], "pages_key": det.get("pages_key"),
                      "fields": {k: f.get(k) for k in keep if f.get(k) not in (None, "")}, "sections": det.get("sections"),
                      "documents": [{k: x.get(k) for k in ("name", "kind", "status", "bytes", "content_type", "reason", "key")} for x in (det.get("documents") or [])]}
    return JSONResponse(json.loads(json.dumps({"project": project, "matches": ids, "tender": tender}, default=_iso)))

IST_NIGHT = "extract(hour FROM (at AT TIME ZONE 'Asia/Kolkata')) < 7"     # 00:00-07:00 in India: the portals' quiet hours


@app.get("/gepnic/portals", response_class=HTMLResponse)
def portals_page(request: Request):
    hubauth.need_user(request)
    return gpage("portals.html", "/gepnic/portals")


@app.get("/gepnic/data", response_class=HTMLResponse)
def data_page(request: Request):
    hubauth.need_user(request)
    return gpage("data.html", "/gepnic/data")


project_slug = fleet.project_slug


def portal_progress(prog, name):
    """Counting and scraping progress of one portal from its two hub projects (see eprocure-scrape's portal.py for the names)."""
    if name == "eprocure.gov.in":
        return None
    plan, ten = prog.get(project_slug(name) + "-plan", {}), prog.get(project_slug(name), {})
    if not plan and not ten:
        return None
    n = lambda d, st: d.get(st, {}).get("n", 0)
    return {"count_done": n(plan, "done"), "count_open": n(plan, "pending") + n(plan, "claimed"), "count_failed": n(plan, "failed"),
            "counted": plan.get("done", {}).get("fetched", 0),
            "windows_done": n(ten, "done"), "windows_open": n(ten, "pending") + n(ten, "claimed"), "windows_failed": n(ten, "failed"),
            "tenders_expected": sum(v["expected"] for v in ten.values()), "tenders_fetched": ten.get("done", {}).get("fetched", 0)}


@app.get("/gepnic/api/fleet", dependencies=[Depends(hubauth.need_user)])
def fleet_api(hours: int = 24):
    """Every GePNIC portal with its server, its share of all tenders, how it answers now, its uptime and answer times per hour, and the
    latest capacity ramp for its server in the Indian night and in the Indian daytime."""
    hours = max(1, min(hours, 168))
    portals = fleet.load()
    total = sum(p["tenders_june2026"] for p in portals) or 1
    names = [fleet.TARGET_PREFIX + p["portal"] for p in portals]
    with db.conn() as c:
        last = {r["target"]: r for r in c.execute(
            """SELECT DISTINCT ON (target) target, ok, ms, detail, at FROM probes
               WHERE target = ANY(%s) AND at > now() - interval '30 minutes' ORDER BY target, at DESC""", (names,)).fetchall()}
        hist = {}
        for r in c.execute(
                """SELECT target, extract(epoch FROM date_trunc('hour', at))::bigint AS h, count(*)::int AS n, count(*) FILTER (WHERE NOT ok)::int AS failed,
                          (percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) FILTER (WHERE ok))::int AS ms
                   FROM probes WHERE target = ANY(%s) AND at > now() - make_interval(hours => %s) GROUP BY 1, 2 ORDER BY 2""", (names, hours)).fetchall():
            hist.setdefault(r["target"], []).append([r["h"], r["n"], r["failed"], r["ms"]])
        # since when each portal has been in its current state: per portal, the newest check with the other result, then the first
        # check after it (two small index lookups per portal; one pass over every check grew slower every minute)
        since = {r["target"]: r["at"] for r in c.execute(
            """WITH last AS (SELECT DISTINCT ON (target) target, ok FROM probes WHERE target = ANY(%s) AND at > now() - interval '30 minutes'
                             ORDER BY target, at DESC),
                    flip AS (SELECT l.target, (SELECT max(q.at) FROM probes q WHERE q.target = l.target AND q.ok <> l.ok
                                                AND q.at > now() - interval '7 days') AS t FROM last l)
               SELECT f.target, (SELECT min(p.at) FROM probes p WHERE p.target = f.target AND p.at > coalesce(f.t, now() - interval '7 days')) AS at
               FROM flip f""", (names,)).fetchall()}
        cap = {}
        for r in c.execute(
                f"""SELECT DISTINCT ON (server_ip, {IST_NIGHT}) server_ip, {IST_NIGHT} AS night, at, safe, tested_max, base_ms, steps, note
                    FROM capacity ORDER BY server_ip, {IST_NIGHT}, at DESC""").fetchall():
            cap.setdefault(r["server_ip"], {})["night" if r["night"] else "day"] = {k: r[k] for k in ("at", "safe", "tested_max", "base_ms", "steps", "note")}
        scraped = c.execute("SELECT count(*) AS n FROM eprocure.tender_index WHERE is_real").fetchone()["n"] \
            if c.execute("SELECT to_regclass('eprocure.tender_index') AS t").fetchone()["t"] else None
        # scraping progress per portal project (gepnic-<slug> and gepnic-<slug>-plan): one pass over the queue table
        prog = {}
        for r in c.execute("""SELECT project, status, count(*)::int AS n, coalesce(sum(fetched), 0)::bigint AS fetched,
                                     coalesce(sum((payload->>'expected')::bigint), 0)::bigint AS expected
                              FROM queue WHERE project LIKE 'gepnic-%' GROUP BY 1, 2""").fetchall():
            prog.setdefault(r["project"], {})[r["status"]] = {"n": r["n"], "fetched": r["fetched"], "expected": r["expected"]}
        # the throttle's current settings and the last change it made, per portal tenders project
        throttle = {r["name"]: {"threads": r["t"], "windows": r["w"], "cap": r["cap"], "phase": r["ph"], "storage": r["st"] or "r2"}
                    for r in c.execute("""SELECT name, (settings->>'threads')::int AS t, (settings->>'max_claimed')::int AS w,
                                                 (settings->'adaptive'->>'max_windows')::int AS cap, settings->>'adaptive_phase' AS ph, settings->>'storage' AS st
                                          FROM projects WHERE name LIKE 'gepnic-%' AND name NOT LIKE '%-plan'""").fetchall()}
        for r in c.execute("""SELECT DISTINCT ON (project) project, at, threads_from, threads_to, claimed_from, claimed_to, reason FROM adjustments
                              WHERE project LIKE 'gepnic-%' AND project NOT LIKE '%-plan' AND at > now() - interval '1 day' ORDER BY project, at DESC""").fetchall():
            if r["project"] in throttle:
                throttle[r["project"]]["last"] = {k: r[k] for k in ("at", "threads_from", "threads_to", "claimed_from", "claimed_to", "reason")}
        rates = {r["project"]: float(r["n"]) / 10 for r in c.execute(
            """SELECT project, sum(records) AS n FROM batches WHERE project LIKE 'gepnic-%' AND project NOT LIKE '%-plan'
               AND at > now() - interval '10 minutes' GROUP BY 1""").fetchall()}
        # how each portal answers our own runners right now (both projects of a portal together): requests and seconds per request
        load = {}
        for r in c.execute("""SELECT regexp_replace(project, '-plan$', '') AS p, sum(value) FILTER (WHERE name='n_http') AS n,
                                     sum(value) FILTER (WHERE name='t_http_s') AS t
                              FROM metrics WHERE project LIKE 'gepnic-%' AND name IN ('n_http', 't_http_s') AND at > now() - interval '15 minutes'
                              GROUP BY 1""").fetchall():
            if r["n"]:
                load[r["p"]] = {"req_per_min": r["n"] / 15, "s_per_req": (r["t"] or 0) / r["n"]}
    out_servers = []
    for ip, ps in fleet.servers(portals).items():
        rows = []
        for p in ps:
            t = fleet.TARGET_PREFIX + p["portal"]
            h = hist.get(t, [])
            n, f = sum(x[1] for x in h), sum(x[2] for x in h)
            lr = last.get(t)
            rows.append({**{k: p[k] for k in ("portal", "title", "kind", "ui", "tomcat", "organisations", "app_url")},
                         "tenders": p["tenders_june2026"], "share": p["tenders_june2026"] / total,
                         "ok": lr["ok"] if lr else None, "ms": lr["ms"] if lr else None, "detail": lr["detail"] if lr else "",
                         "checked": lr["at"] if lr else None, "since": since.get(t), "uptime": (1 - f / n) if n else None, "checks": n, "hours": h,
                         "scraped": scraped if p["portal"] == "eprocure.gov.in" else None,
                         "progress": portal_progress(prog, p["portal"]), "load": load.get(project_slug(p["portal"])),
                         "throttle": throttle.get(project_slug(p["portal"])), "rate_10m": rates.get(project_slug(p["portal"]), 0.0)})
        tenders = sum(p["tenders_june2026"] for p in ps)
        out_servers.append({"ip": ip, "portals": rows, "tenders": tenders, "share": tenders / total, "capacity": cap.get(ip, {})})
    return JSONResponse(json.loads(json.dumps({"servers": out_servers, "total_tenders": total, "hours": hours, "source": fleet.source_note(),
                                               "steps": list(fleet.RAMP_STEPS)}, default=_iso)))


@app.get("/gepnic/api/fleet/capacity", dependencies=[Depends(hubauth.need_user)])
def fleet_capacity(server_ip: str, days: int = 14):
    """Every capacity ramp of one server in the last `days`, oldest first."""
    days = max(1, min(days, 90))
    with db.conn() as c:
        rows = c.execute(f"""SELECT at, {IST_NIGHT} AS night, safe, tested_max, base_ms, steps, note FROM capacity
                             WHERE server_ip=%s AND at > now() - make_interval(days => %s) ORDER BY at""", (server_ip, days)).fetchall()
    return JSONResponse(json.loads(json.dumps({"server_ip": server_ip, "ramps": rows}, default=_iso)))


@app.get("/gepnic/api/showcase", dependencies=[Depends(hubauth.need_user)])
def showcase_api(project: str = "eprocure-tenders"):
    with db.conn() as c:
        row = c.execute("SELECT built_at, data FROM showcase WHERE project=%s", (project,)).fetchone()
    if not row:
        raise HTTPException(404, "not built yet: run scripts/build_showcase.py")
    return JSONResponse(json.loads(json.dumps({"project": project, "built_at": row["built_at"], **row["data"]}, default=_iso)))


@app.get("/gepnic/data/quality", response_class=HTMLResponse)
def quality_page(request: Request):
    hubauth.need_user(request)
    return gpage("quality.html", "/gepnic/data")


@app.get("/gepnic/api/quality", dependencies=[Depends(hubauth.need_user)])
def quality_api():
    """The data-quality checks over every stored tender (scripts/build_quality.py, from quality.facts), as built last."""
    with db.conn() as c:
        row = c.execute("SELECT built_at, data FROM showcase WHERE project='quality'").fetchone()
    if not row:
        raise HTTPException(404, "not built yet: run scripts/ops/quality_facts.sql, then scripts/build_quality.py")
    return JSONResponse(json.loads(json.dumps({"built_at": row["built_at"], **row["data"]}, default=_iso)))

# ----------------------------------------------------------------------------- tender bundles (scrape_hub/bundles.py)
# /bundle is our page (dashboard login); /bundle/<slug>.zip is public and asks for that bundle's own password, which opens only that zip.
# Every answer under /bundle says no-store: Cloudflare caches .zip paths by default, and a cached password page or redirect would leak.

NO_STORE = {"Cache-Control": "no-store, private", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}
ZIP_NAME = re.compile(r"^([a-z0-9][a-z0-9-]{1,62})\.zip$")


class BundleIn(BaseModel):
    slug: str = Field(min_length=2, max_length=67)
    tenders: str = Field(max_length=1_000_000)
    title: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=4000)
    public_note: str = Field(default="", max_length=2000)
    include: dict = {}
    password: str = Field(default="", max_length=200)
    public: bool = True


class BundleEdit(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    note: Optional[str] = Field(default=None, max_length=4000)
    public_note: Optional[str] = Field(default=None, max_length=2000)
    tenders: Optional[str] = Field(default=None, max_length=1_000_000)
    include: Optional[dict] = None
    public: Optional[bool] = None


class BundlePassword(BaseModel):
    password: str = Field(default="", max_length=200)
    generate: bool = False


class TenderText(BaseModel):
    tenders: str = Field(max_length=1_000_000)


def bundle_json(b):
    return json.loads(json.dumps(b, default=_iso))


@app.get("/gepnic/bundles", response_class=HTMLResponse)
def bundles_page(request: Request):
    hubauth.need_user(request)
    return HTMLResponse(gpage("bundles.html", "/gepnic/bundles"), headers=NO_STORE)


@app.get("/gepnic/api/bundles", dependencies=[Depends(hubauth.need_user)])
def bundles_list():
    with db.conn() as c:
        rows = bundles.list_all(c)
    for r in rows:
        r.pop("tenders", None)
    return JSONResponse(bundle_json({"bundles": rows}), headers=NO_STORE)


@app.post("/gepnic/api/bundles/preview", dependencies=[Depends(hubauth.need_member)])
def bundles_preview(body: TenderText):
    entries = bundles.parse_tenders(body.tenders)
    if len(entries) > bundles.MAX_TENDERS:
        raise HTTPException(422, f"{len(entries)} tenders; at most {bundles.MAX_TENDERS} per bundle")
    with db.conn() as c:
        return JSONResponse(bundle_json({"entries": entries, **bundles.preview(c, entries)}), headers=NO_STORE)


@app.post("/gepnic/api/bundles", dependencies=[Depends(hubauth.need_member)])
def bundles_create(body: BundleIn):
    try:
        with db.conn() as c:
            b = bundles.create(c, body.slug, body.tenders, body.title, body.note, body.public_note, body.include, body.password,
                               body.public, created_by="dashboard")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return JSONResponse(bundle_json(b), headers=NO_STORE)


@app.get("/gepnic/api/bundles/{slug}", dependencies=[Depends(hubauth.need_user)])
def bundles_get(slug: str):
    with db.conn() as c:
        b = bundles.get(c, slug)
    if not b:
        raise HTTPException(404, "no such bundle")
    return JSONResponse(bundle_json(b), headers=NO_STORE)


@app.post("/gepnic/api/bundles/{slug}", dependencies=[Depends(hubauth.need_member)])
def bundles_edit(slug: str, body: BundleEdit):
    try:
        with db.conn() as c:
            b = bundles.update(c, slug, body.title, body.note, body.public_note, body.tenders, body.include, body.public)
    except KeyError:
        raise HTTPException(404, "no such bundle")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return JSONResponse(bundle_json(b), headers=NO_STORE)


@app.post("/gepnic/api/bundles/{slug}/password", dependencies=[Depends(hubauth.need_member)])
def bundles_password(slug: str, body: BundlePassword):
    """Set (or with an empty password, remove) the bundle's password. generate=true makes one up and returns it, once."""
    pw = bundles.new_password() if body.generate else body.password
    if pw and len(pw) < 6:
        raise HTTPException(422, "use at least 6 characters")
    with db.conn() as c:
        if not bundles.get(c, slug):
            raise HTTPException(404, "no such bundle")
        bundles.set_password(c, slug, pw)
    return JSONResponse({"ok": True, "password": pw if body.generate else None, "has_password": bool(pw)}, headers=NO_STORE)


@app.post("/gepnic/api/bundles/{slug}/rebuild", dependencies=[Depends(hubauth.need_member)])
def bundles_rebuild(slug: str):
    with db.conn() as c:
        if not bundles.get(c, slug):
            raise HTTPException(404, "no such bundle")
        bundles.queue_rebuild(c, slug)
    return JSONResponse({"ok": True}, headers=NO_STORE)


@app.delete("/gepnic/api/bundles/{slug}", dependencies=[Depends(hubauth.need_steer)])
def bundles_delete(slug: str):
    """Deletes the row and the zip in object storage; the public link stops working at once."""
    with db.conn() as c:
        b = bundles.get(c, slug)
        if not b:
            raise HTTPException(404, "no such bundle")
        if b["object_key"] and s3.ready("r2"):
            client, bucket = s3.client("r2")
            client.delete_object(Bucket=bucket, Key=b["object_key"])
        c.execute("DELETE FROM bundles WHERE slug=%s", (slug,))
        c.execute("DELETE FROM bundle_attempts WHERE slug=%s", (slug,))
    return JSONResponse({"ok": True}, headers=NO_STORE)


@app.get("/gepnic/api/bundles/{slug}/download", dependencies=[Depends(hubauth.need_user)])
def bundles_admin_download(slug: str):
    with db.conn() as c:
        b = bundles.get(c, slug)
    if not b or not b["object_key"]:
        raise HTTPException(404, "not built yet")
    client, bucket = s3.client("r2")
    return RedirectResponse(bundles.download_url(client, bucket, b), 303, headers=NO_STORE)


def bundle_public_page(b, slug: str, msg: str = "", status: int = 200) -> HTMLResponse:
    e = lambda v: html_escape(str(v if v is not None else ""))
    if b:
        size = f"{b['bytes'] / 1e6:,.1f} MB" if b["bytes"] and b["bytes"] >= 1e6 else f"{(b['bytes'] or 0) / 1e3:,.0f} KB"
        n = (b["progress"] or {}).get("done")
        facts = f"{e(size)}{f' &middot; {n:,} tenders' if n else ''} &middot; updated {e(b['built_at'].strftime('%d %b %Y'))}"
        body = (f'<p class="facts">{facts}</p>' + (f'<p class="note">{e(b["public_note"])}</p>' if b["public_note"] else "")
                + (f'<p class="err" role="alert">{e(msg)}</p>' if msg else "")
                + f'''<form method="post" action="/bundle/{e(slug)}.zip"><label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
<button>Download</button></form>
<p class="small">The password opens this file only. The download link it gives you works for an hour.</p>''')
    else:
        body = f'<p class="err">{e(msg or "This bundle does not exist or is not shared.")}</p>'
    html = (WEB / "bundle_public.html").read_text().replace("__NAME__", e(f"{slug}.zip")).replace("__BODY__", body)
    return HTMLResponse(html, status, headers=NO_STORE)


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def shared_bundle(c, slug: str):
    """The bundle if its public link is open: built at least once, switched on, and with a password."""
    b = bundles.get(c, slug)
    return b if b and b["public"] and b["has_password"] and b["object_key"] else None


@app.get("/bundle/{name}", response_class=HTMLResponse)
def bundle_public(name: str, request: Request):
    hubauth.visitor(request)                 # public, but only through the hub
    m = ZIP_NAME.match(name)
    if not m:
        return bundle_public_page(None, name.removesuffix(".zip")[:63], status=404)
    with db.conn() as c:
        b = shared_bundle(c, m.group(1))
    return bundle_public_page(b, m.group(1), status=200 if b else 404)


@app.post("/bundle/{name}")
async def bundle_unlock(name: str, request: Request):
    hubauth.visitor(request)
    m = ZIP_NAME.match(name)
    if not m:
        return bundle_public_page(None, name.removesuffix(".zip")[:63], status=404)
    slug, ip = m.group(1), hubauth.client_ip(request)
    pw = parse_qs((await request.body()).decode("utf-8", "replace")).get("password", [""])[0][:200]
    with db.conn() as c:
        b = shared_bundle(c, slug)
        if not b:
            return bundle_public_page(None, slug, status=404)
        if bundles.locked_out(c, slug, ip):
            return bundle_public_page(b, slug, "Too many wrong passwords. Try again later.", 429)
        ok = bundles.check_password(pw, bundles.password_record(c, slug))
        bundles.note_attempt(c, slug, ip, ok)
    if not ok:
        return bundle_public_page(b, slug, "Wrong password.", 401)
    client, bucket = s3.client("r2")
    return RedirectResponse(bundles.download_url(client, bucket, b), 303, headers=NO_STORE)


# ----------------------------------------------------------------------------- runner extras of the GePNIC crawler
# The crawler calls these on the hub (POST /api/v1/runs/{run_id}/<ext>); the hub checks the runner's token and that the run is its own,
# then forwards here with the run signed (hubauth.run_caller). A request naming another project (`target`) may only name a GePNIC one.

# A record counts as stored (so runners skip it) only when it is a real capture: a title, covers on its details page (an empty one is the
# portal's 'session expired' page), and its documents fetched. Records from a run without documents (status not_stored) are fetched again.
STORED_SQL = """coalesce(data->'details'->'fields'->>'tender_title', '') <> ''
    AND (NOT (data->'details' ? 'sections') OR jsonb_array_length(coalesce(data->'details'->'sections'->'covers', '[]'::jsonb)) > 0)
    AND NOT coalesce(data->'details'->'documents', '[]'::jsonb) @> '[{"status": "not_stored"}]'::jsonb"""


def gepnic_project(c, name: str):
    """403 unless `name` is an existing project of the GePNIC family."""
    if not name.startswith(("gepnic-", "eprocure-")) or not c.execute("SELECT 1 FROM projects WHERE name=%s", (name,)).fetchone():
        raise HTTPException(403, f"project {name} is not a GePNIC project")


class KnownReq(BaseModel):
    ids: list[str] = Field(max_length=2000)


class WindowReq(BaseModel):
    org_id: str = Field(max_length=40)
    from_date: str = Field(max_length=20)
    to_date: str = Field(max_length=20)


class BackfillTodo(BaseModel):
    target: str = Field(max_length=64)
    org_id: str = Field(max_length=40)
    year: str = Field(pattern=r"^\d{4}$")
    field: str = Field(default="sections", pattern=r"^[a-z_]{1,32}$")
    limit: int = Field(default=3000, ge=1, le=5000)


class PatchItem(BaseModel):
    record_id: str = Field(max_length=200)
    value: dict


class PatchRecords(BaseModel):
    target: str = Field(max_length=64)
    field: str = Field(default="sections", pattern=r"^[a-z_]{1,32}$")
    patches: list[PatchItem] = Field(min_length=1, max_length=500)


@app.post("/hub/runs/{run_id}/known-records")
def known_records(run_id: int, body: KnownReq, request: Request):
    """Which of these record ids are already stored as a real tender (an empty title, or a details page with no covers, is a failed
    capture, e.g. the portal's 'session expired' page, and is fetched again), so a scraper resuming a window can skip them."""
    run = hubauth.run_caller(request, run_id, "known-records")
    with db.conn() as c:
        gepnic_project(c, run["project"])
        rows = c.execute("SELECT record_id FROM records WHERE project=%s AND record_id = ANY(%s) AND " + STORED_SQL,
                         (run["project"], body.ids)).fetchall()
    return {"known": [r["record_id"] for r in rows]}


@app.post("/hub/runs/{run_id}/window-stored")
def window_stored(run_id: int, body: WindowReq, request: Request):
    """How many tenders of one (organisation, date window) are already stored with a real title, so a runner can skip a window that is
    already complete without listing it (listing is the slow part: one results page of 10 tenders at a time)."""
    run = hubauth.run_caller(request, run_id, "window-stored")
    with db.conn() as c:
        gepnic_project(c, run["project"])
        n = c.execute("""SELECT count(*) AS n FROM records WHERE project=%s AND data->>'org_id'=%s AND data->'window'->>0=%s
                         AND data->'window'->>1=%s AND """ + STORED_SQL,
                      (run["project"], body.org_id, body.from_date, body.to_date)).fetchone()["n"]
    return {"stored": n}


@app.post("/hub/runs/{run_id}/backfill/todo")
def backfill_todo(run_id: int, body: BackfillTodo, request: Request):
    """Records of one organisation and year in `target` that have no `details.<field>` yet, with the key of their saved pages."""
    run = hubauth.run_caller(request, run_id, "backfill/todo")
    with db.conn() as c:
        gepnic_project(c, run["project"])
        gepnic_project(c, body.target)
        rows = c.execute("""SELECT record_id, data->'details'->>'pages_key' AS pages_key FROM records
                            WHERE project=%s AND data->>'org_id'=%s AND record_id LIKE %s
                              AND NOT (coalesce(data->'details', '{}'::jsonb) ? %s) AND data->'details' ? 'pages_key'
                            ORDER BY record_id LIMIT %s""",
                         (body.target, body.org_id, body.year + "\\_%", body.field, body.limit)).fetchall()
    return {"records": rows}


@app.post("/hub/runs/{run_id}/patch-records")
def patch_records(run_id: int, body: PatchRecords, request: Request):
    """Set `details.<field>` on existing records of `target`, leaving everything else in them untouched. Counted as this run's output
    so a backfill shows up on the hub like any other run."""
    run = hubauth.run_caller(request, run_id, "patch-records")
    with db.conn() as c:
        gepnic_project(c, run["project"])
        gepnic_project(c, body.target)
        with c.cursor() as cur:
            cur.executemany(
                """UPDATE records SET data = data || jsonb_build_object('details', coalesce(data->'details', '{}'::jsonb) || jsonb_build_object(%s::text, %s::jsonb))
                   WHERE project=%s AND record_id=%s""",
                [(body.field, json.dumps(p.value), body.target, p.record_id) for p in body.patches])
        n = len(body.patches)
        c.execute("INSERT INTO batches (run_id, project, records) VALUES (%s,%s,%s)", (run_id, run["project"], n))
        c.execute("UPDATE runs SET records=records+%s, batches=batches+1, last_seen=now() WHERE id=%s", (n, run_id))
    return {"patched": n}
