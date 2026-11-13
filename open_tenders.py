#!/usr/bin/env python3
"""Open road tenders on a GePNIC portal, captured while they are open (task "open", projects gepnic-<slug>-open).

A portal removes a tender's NIT and work-item documents when the tender closes, so the status crawl (crawl.py) never sees them. This
task reads the public "Tenders by Organisation" pages, which list every active tender and need no captcha:

  poll-<date>   list every organisation's active tenders, keep the road-like ones (ROAD below), queue "t-<tender id>" for each one
                not queued before (the hub keeps existing queue items, so a tender is fetched once)
  t-<tender id> open the tender's page (FrontEndViewTender, no captcha), parse its fields (parsing.parse_tender), list its NIT and
                work-item documents, and store the page in the bucket (tenders/<year>/<org>/<id>__<time>.tar, member pages.json.gz)

Documents: on the portals checked on 2026-11-13 the NIT link and "Download as zip file" open a captcha page, and work-item documents
(BOQ, bid document) have no public link. This task never answers a captcha. It asks for the NIT once per process: a PDF is stored
(and the next tenders' NITs too); a captcha page sets documents to status "captcha" for the rest of the run and counts doc_captcha_wall.

    PORTAL=uktenders.gov.in python open_tenders.py [--max-items N]
    PORTAL=uktenders.gov.in python open_tenders.py --local   # one poll of one organisation and its first road tender, no hub, nothing stored
"""
import argparse
import datetime
import os
import re
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import requests  # noqa: E402

import documents  # noqa: E402
import parsing  # noqa: E402
import portal  # noqa: E402

PROJECT = os.environ.get("SCRAPE_POOL_PROJECT") or portal.OPEN
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
# Road-like titles: the same words as road-site/export_national.py in sarthak's investigations repo (road, marg, highway, bridge, ...).
ROAD = re.compile(r"\b(road|roads|marg|highway|flyover|fly over|bridge|footpath|pavement|resurfac\w*|re-?carpet\w*|pothole|nh-?\d+|sh-?\d+|"
                  r"bypass|culvert|interlocking|bituminous|black ?top\w*|cc road|wbm|kharanja|sadak|rasta|path)\b", re.I)
CAPTCHA_MARKS = (b"Please fill the Captcha", b'id="captchaText"', b"frmCaptcha")
stop = False


def on_signal(*_):
    global stop
    stop = True


def text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).replace("&amp;", "&").replace("&nbsp;", " ").strip()


# ---- pages -> data (pure, tested on saved pages in tests/fixtures/open)

def org_links(html):
    """'Tenders by Organisation': [{"org", "count", "url"}] for every organisation with active tenders."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S):
        m = re.search(r'href="([^"]*component=%24DirectLink[^"]*page=FrontEndTendersByOrganisation[^"]*sp=[^"]*)"[^>]*>\s*(\d+)\s*</a>', row)
        if not m:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        name = text(cells[1]) if len(cells) > 1 else ""
        out.append({"org": name, "count": int(m.group(2)), "url": portal.url(m.group(1))})
    return out


def tender_rows(html):
    """One organisation's active tenders: [{"tender_id", "title", "reference_no", "published", "closing", "opening", "org_chain", "view_link"}]."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S):
        m = re.search(r'href="([^"]*page=FrontEndViewTender[^"]*)"[^>]*>(.*?)</a>(.*?)</td>', row, re.S)
        if not m:
            continue
        cells = [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        title = text(m.group(2)).strip()
        if title.startswith("[") and title.endswith("]"):
            title = title[1:-1].strip()
        tail = re.findall(r"\[([^\]]*)\]", text(m.group(3)))
        tid = tail[-1] if tail else ""
        if not re.fullmatch(r"\d{4}_[A-Za-z0-9&.-]+_\d+_\d+", tid):
            continue
        out.append({"tender_id": tid, "title": title, "reference_no": tail[0] if len(tail) > 1 else "",
                    "published": cells[1] if len(cells) > 1 else "", "closing": cells[2] if len(cells) > 2 else "",
                    "opening": cells[3] if len(cells) > 3 else "", "org_chain": cells[-1] if len(cells) > 5 else "",
                    "view_link": portal.url(m.group(1))})
    return out


def is_road(row):
    return bool(ROAD.search(row.get("title") or ""))


def _table(html, table_id_or_after):
    """Data rows (lists of cell texts) of the first list table after a marker, plus the raw rows."""
    i = html.find(table_id_or_after)
    if i < 0:
        return []
    j = html.find("</table>", i)
    rows = re.findall(r"<tr([^>]*)>(.*?)</tr>", html[i:j], re.S)
    return [(r, [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]) for attrs, r in rows if "list_header" not in attrs and "<td" in r]


def tender_documents(html):
    """The tender page's NIT documents and work-item documents: [{name, kind, type, description, declared_bytes, link}].
    link is the NIT's download URL (only NIT rows have one; it works only in the session that opened this page)."""
    out = []
    i = html.find("NIT Document")
    if i >= 0:
        for raw, c in _table(html[i:], 'class="list_table"'):
            if len(c) < 4:
                continue
            a = re.search(r'href="([^"]*component=docDownoad[^"]*)"', raw)
            out.append({"name": c[1], "kind": "nit", "type": "NIT", "description": c[2], "declared_bytes": _kb(c[3]),
                        "link": portal.url(a.group(1)) if a else None})
    for raw, c in _table(html, 'id="workItemDocumenttable"'):
        if len(c) < 5:
            continue
        out.append({"name": c[2], "kind": "work_item", "type": c[1], "description": c[3], "declared_bytes": _kb(c[4]), "link": None})
    return out


def _kb(s):
    try:
        return int(float(s.replace(",", "")) * 1024)
    except (ValueError, AttributeError):
        return None


def is_captcha(content):
    return any(m in (content or b"")[:200000] for m in CAPTCHA_MARKS)


# ---- the portal

class Portal:
    """One portal session, at most one request per `gap` seconds, with the hub's request metrics."""

    def __init__(self, hub=None, gap=2.0):
        self.hub, self.gap, self.last = hub, gap, 0.0
        self.fresh()

    def fresh(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.5"})
        portal.cookie(self.s)
        self.get(portal.APP)

    def metric(self, name, n=1):
        if self.hub:
            self.hub.metric(name, n)

    def get(self, url, **kw):
        wait = self.last + self.gap - time.time()
        if wait > 0:
            time.sleep(wait)
        t = time.time()
        try:
            r = self.s.get(url, timeout=kw.pop("timeout", 90), **kw)
        except requests.RequestException:
            self.last = time.time()
            self.metric("http_error")
            if self.hub:
                self.hub.error()
            raise
        self.last = time.time()
        self.metric("n_http")
        self.metric("t_http_s", self.last - t)
        if r.status_code != 200:
            self.metric(f"http_{r.status_code}")
            if r.status_code in (403, 429, 503) and self.hub:
                self.hub.block()
            r.raise_for_status()
        return r


# ---- the two item kinds

def run_poll(hub, P, item, seed, project):
    """List every organisation's active tenders and queue the road-like ones. Returns (active, road, newly queued)."""
    orgs = org_links(P.get(portal.app_url("page=FrontEndTendersByOrganisation&service=page")).text)
    active, road, items = 0, 0, {}
    for o in orgs:
        if stop:
            raise RuntimeError("stopped by signal")
        rows = tender_rows(P.get(o["url"]).text)
        active += len(rows)
        for r in rows:
            if is_road(r):
                road += 1
                items[r["tender_id"]] = {"key": f"t-{r['tender_id']}", "payload": {**r, "org": o["org"], "seen": item["key"][5:]}}
    added = seed(project, list(items.values()))["added"] if items else 0
    hub.metric("open_active", active)
    hub.metric("open_road", road)
    hub.metric("open_queued", added)
    hub.note(f"{portal.NAME}: {len(orgs)} organisations, {active} active tenders, {road} road-like, {added} new")
    return active, road, added


class Docs:
    """Per process: whether this portal's NIT link gives the file (open) or a captcha page (wall). None = not tried yet."""
    state = None


def run_tender(hub, P, item, store=True):
    """Fetch one open tender's page, parse it, try its NIT once per process, store the page (and any open document), make the record."""
    row = item["payload"]
    tid = row["tender_id"]
    html = P.get(row["view_link"]).text
    if tid not in html:                         # an expired session or a stale link: one more try on a fresh session
        P.fresh()
        html = P.get(row["view_link"]).text
        if tid not in html:
            raise RuntimeError("tender page did not load (closed, or the link is stale)")
    fields, _proc, _out = parsing.parse_tender(tid, "", row.get("org", ""), {"view_link": row["view_link"]}, "", html, "", [])
    docs = tender_documents(html)
    members = [("pages.json.gz", documents.pages_bundle("", html, ""))]
    entries = []
    for n, d in enumerate(docs, 1):
        e = {k: d[k] for k in ("name", "kind", "type", "description", "declared_bytes")}
        if d["link"] and Docs.state != "wall":
            r = P.get(d["link"])
            if is_captcha(r.content):
                Docs.state = "wall"
                hub.metric("doc_captcha_wall")
                hub.note(f"{portal.NAME}: the NIT link opens a captcha page; documents are not fetched")
                e["status"] = "captcha"
            else:
                v = documents.verify_download({"name": d["name"], "declared_bytes": d["declared_bytes"]}, r.content)
                if v["ok"]:
                    Docs.state = "open"
                    path = f"docs/{n:02d}_{documents.safe_part(v['filename'])}"
                    members.append((path, r.content))
                    e.update(status="stored", content_type=v["content_type"], bytes=v["bytes"], sha256=v["sha256"], size_ok=v["size_ok"], _path=path)
                    hub.metric("doc_stored")
                else:
                    e.update(status="refused", reason=v["reason"])
                    hub.metric("doc_refused")
        elif d["link"]:
            e["status"] = "captcha"
        else:
            e["status"] = "no_public_link"     # work-item documents: only inside the captcha-guarded zip
        entries.append(e)
    details = {"fields": fields, "documents": entries, "doc_access": Docs.state or "none",
               "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if store:
        tar, where = documents.tender_bundle(members)
        key = hub.upload_many([(f"tenders/{documents.tender_prefix(tid, '')}__{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.tar",
                                tar, "application/x-tar")])[0]["key"]
        details.update(bundle_key=key, pages_key=key, pages_range=where["pages.json.gz"])
        for e in entries:
            if e.get("_path"):
                e.update(key=key, member=e["_path"], range=where[e["_path"]])
    for e in entries:
        e.pop("_path", None)
    hub.record(tid, {"org_name": row.get("org", ""), "list": {k: row.get(k) for k in
                     ("title", "reference_no", "published", "closing", "opening", "org_chain", "view_link")},
                     "seen_open": row.get("seen"), "details": details})
    hub.metric("n_tenders")
    return details


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-index", type=int, default=int(os.environ.get("SCRAPE_JOB_INDEX", 0)))
    ap.add_argument("--total-jobs", type=int, default=int(os.environ.get("SCRAPE_TOTAL_JOBS", 1)))
    ap.add_argument("--max-hours", type=float, default=float(os.environ.get("SCRAPE_MAX_HOURS", 1)))
    ap.add_argument("--max-items", type=int, default=0)
    a = ap.parse_args()
    from scrape_hub_client import HubClient, seed_queue
    deadline = time.time() + a.max_hours * 3600 - 300
    hub = HubClient(PROJECT, a.job_index, a.total_jobs, note="open tenders")
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    P = Portal(hub, max(1.0, hub.settings.get("delay_ms", 2000) / 1000))
    done = 0
    try:
        while time.time() < deadline and not stop and not (a.max_items and done >= a.max_items):
            if hub.settings.get("paused"):
                time.sleep(30)
                continue
            items = hub.claim(1, steal=True, retry_failed=True)
            if not items:
                break
            item = items[0]
            try:
                if item["key"].startswith("poll-"):
                    _, road, added = run_poll(hub, P, item, seed_queue, PROJECT)
                    hub.complete([item["key"]], "done", fetched=road)
                else:
                    run_tender(hub, P, item)
                    hub.complete([item["key"]], "done", fetched=1)
            except Exception as e:      # one bad item must not stop the run; the portal may have ended the session
                hub.failure("open-item", item["key"], f"{type(e).__name__}: {e}"[:200])
                hub.complete([item["key"]], "failed", error=f"{type(e).__name__}: {e}"[:200])
                try:
                    P.fresh()
                except Exception:
                    pass
            done += 1
        hub.close("finished" if not stop else "failed", f"{done} items" + ("" if not stop else ", stopped by signal"))
    except BaseException as e:
        hub.close("failed", f"{type(e).__name__}: {e}"[:200])
        raise


class LocalHub:
    """Stand-in for --local: prints what would be sent."""
    def metric(self, *a): pass
    def error(self, *a): pass
    def block(self, *a): pass
    def note(self, t): print("note:", t)
    def record(self, rid, data): print("record", rid, {k: v for k, v in data.items() if k != "details"}, data["details"]["documents"])


def local():
    hub, P = LocalHub(), Portal(None, 2.0)
    orgs = org_links(P.get(portal.app_url("page=FrontEndTendersByOrganisation&service=page")).text)
    print(len(orgs), "organisations,", sum(o["count"] for o in orgs), "active tenders")
    for o in sorted(orgs, key=lambda o: o["count"]):
        rows = [r for r in tender_rows(P.get(o["url"]).text) if is_road(r)]
        if rows:
            print(o["org"], len(rows), "road-like")
            d = run_tender(hub, P, {"key": "t-x", "payload": {**rows[0], "org": o["org"], "seen": datetime.date.today().isoformat()}}, store=False)
            print({k: v for k, v in d["fields"].items() if k in ("tender_title", "location", "pincode", "tender_value", "bid_submission_end_date")}, d["doc_access"])
            return


if __name__ == "__main__":
    if sys.argv[1:2] == ["--local"]:
        sys.exit(local())
    sys.exit(main())
