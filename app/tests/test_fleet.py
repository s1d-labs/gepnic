"""The portal fleet: registry, checks, ramp verdicts, the fleet/showcase/quality APIs, pages, navigation and sign-in."""
import json

from fastapi.testclient import TestClient

from gepnic_app import db, fleet
from gepnic_app.app import app


def test_registry_has_every_portal_once_and_groups_shared_servers():
    ps = fleet.load()
    assert len(ps) == 46 and len({p["portal"] for p in ps}) == 46
    groups = fleet.servers(ps)
    assert len(groups) == 40
    assert len(groups["164.100.79.193"]) == 6                       # the six shipyard / defence-PSU portals share one server
    first = next(iter(groups.values()))
    assert first[0]["portal"] == "wbtenders.gov.in"                 # biggest server first
    assert all(p["check_url"].startswith("https://") for p in ps)


def test_page_ok_wants_the_live_application():
    gepnic = {"ui": "gepnic"}
    assert fleet.page_ok(gepnic, 200, "<html><FORM id=x>")[0]
    assert fleet.page_ok(gepnic, 200, "<html>static front page</html>") == (False, "answered but the page has no form")
    assert fleet.page_ok(gepnic, 503, "<form>") == (False, "HTTP 503")
    assert fleet.page_ok({"ui": "gepnic2"}, 200, "Server is up")[0] and not fleet.page_ok({"ui": "gepnic2"}, 200, "error")[0]


def test_step_verdict_stops_on_failures_and_on_a_slowdown():
    fast = [(0.2, True)] * 40
    s, stop = fleet.step_verdict(fast, None)
    assert s["med_ms"] == 200 and not stop
    assert not fleet.step_verdict([(0.35, True)] * 40, 200)[1]          # 75% slower but under +500 ms: still fine
    assert fleet.step_verdict([(0.9, True)] * 40, 200)[1]               # more than doubled and +700 ms: overloaded
    assert fleet.step_verdict([(0.2, True)] * 18 + [(30, False)] * 2, 200)[1]   # 10% failed
    assert fleet.step_verdict([(30, False)] * 3, None)[1]               # nothing answered
    assert fleet.step_verdict([(14.5, True)] * 4, None)[1]              # answering, but 14.5 s even with one worker: struggling


def test_fleet_api_reports_status_uptime_and_capacity(client, clean, login):
    with db.conn() as c:
        for i in range(10):
            c.execute("INSERT INTO probes (target, at, ok, ms) VALUES ('gepnic:wbtenders.gov.in', now() - make_interval(mins => %s), %s, 400)", (10 - i, i >= 3))
            c.execute("INSERT INTO probes (target, at, ok, ms, detail) VALUES ('gepnic:etender.up.nic.in', now() - make_interval(mins => %s), false, 20000, 'ReadTimeout')", (10 - i,))
        c.execute("""INSERT INTO capacity (server_ip, at, portals, safe, tested_max, base_ms, steps) VALUES
                     ('164.100.69.49', '2026-10-18 22:00+00', '{wbtenders.gov.in}', 16, 16, 210, %s::jsonb),
                     ('164.100.69.49', '2026-10-18 10:00+00', '{wbtenders.gov.in}', 4, 8, 900, '[]')""",
                  (json.dumps([{"workers": 1, "med_ms": 210, "n": 50, "fail": 0, "rps": 4.8, "stopped": False}]),))
    d = login.get("/gepnic/api/fleet").json()
    assert len(d["servers"]) == 40 and abs(sum(s["share"] for s in d["servers"]) - 1) < 1e-9
    wb = d["servers"][0]
    assert wb["ip"] == "164.100.69.49" and wb["portals"][0]["ok"] is True and wb["portals"][0]["uptime"] == 0.7
    assert wb["capacity"]["night"]["safe"] == 16 and wb["capacity"]["day"]["safe"] == 4        # 22:00 UTC is 03:30 IST; 10:00 UTC is 15:30 IST
    up = next(s for s in d["servers"] if s["portals"][0]["portal"] == "etender.up.nic.in")["portals"][0]
    assert up["ok"] is False and up["detail"] == "ReadTimeout" and up["uptime"] == 0
    assert login.get("/gepnic/api/fleet/capacity?server_ip=164.100.69.49&days=30").json()["ramps"][0]["safe"] == 4


def test_showcase_is_served_once_built(client, clean, login):
    assert login.get("/gepnic/api/showcase").status_code == 404
    with db.conn() as c:
        c.execute("INSERT INTO showcase (project, data) VALUES ('eprocure-tenders', %s::jsonb)", (json.dumps({"totals": {"tenders": 5}, "by_year": []}),))
    d = login.get("/gepnic/api/showcase").json()
    assert d["totals"]["tenders"] == 5 and d["project"] == "eprocure-tenders" and d["built_at"]


def test_quality_is_served_once_built(client, clean, login):
    assert login.get("/gepnic/api/quality").status_code == 404
    with db.conn() as c:
        c.execute("INSERT INTO showcase (project, data) VALUES ('quality', %s::jsonb)", (json.dumps({"overall": {"tenders": 7}, "portals": []}),))
    d = login.get("/gepnic/api/quality").json()
    assert d["overall"]["tenders"] == 7 and d["built_at"]
    assert TestClient(app).get("/gepnic/api/quality").status_code == 403      # not through the hub


def test_fleet_shows_counting_and_scraping_progress_per_portal(client, clean, login):
    with db.conn() as c:
        c.execute("""INSERT INTO queue (project, key, partition_id, status, payload, fetched) VALUES
            ('gepnic-sikkimtender-plan', 'count:1', 0, 'done', '{"kind":"count"}', 96),
            ('gepnic-sikkimtender-plan', 'count:2', 0, 'pending', '{"kind":"count"}', 0),
            ('gepnic-wbtenders', 'w1', 0, 'done', '{"expected": 1000}', 990),
            ('gepnic-wbtenders', 'w2', 0, 'pending', '{"expected": 500}', 0)""")
    d = login.get("/gepnic/api/fleet").json()
    by = {p["portal"]: p for s in d["servers"] for p in s["portals"]}
    sk, wb = by["sikkimtender.gov.in"]["progress"], by["wbtenders.gov.in"]["progress"]
    assert sk["count_done"] == 1 and sk["count_open"] == 1 and sk["counted"] == 96
    assert wb["windows_done"] == 1 and wb["windows_open"] == 1 and wb["tenders_expected"] == 1500 and wb["tenders_fetched"] == 990
    assert by["tntenders.gov.in"]["progress"] is None and by["eprocure.gov.in"]["progress"] is None


def test_pages_carry_the_hub_links_and_the_app_menu(client, clean, login):
    for path in ("/gepnic", "/gepnic/portals", "/gepnic/data", "/gepnic/tender", "/gepnic/bundles", "/gepnic/throttle"):
        html = login.get(path).text
        assert '<a href="/gepnic" aria-current="page">' in html and '<nav class="nav sub">' in html and f'<a href="{path}" aria-current="page">' in html
        assert '<a href="/runners">' in html and "<!--NAV-->" not in html
    html = login.get("/gepnic/data/quality").text
    assert '<a href="/gepnic/data" aria-current="page">' in html and "/gepnic/api/quality" in html
    assert "/gepnic/api/scrapes" in login.get("/gepnic").text and "/gepnic/api/throttle" in login.get("/gepnic/throttle").text


def test_only_signed_requests_get_in(client, clean, signed):
    assert client.get("/gepnic/portals").status_code == 403 and client.get("/gepnic/api/fleet").status_code == 403
    ok = signed("harsh", "viewer")
    assert ok.get("/gepnic/api/fleet").status_code == 200
    ts = str(int(__import__("time").time()))
    h = {"X-Hub-User": "harsh", "X-Hub-Role": "owner", "X-Hub-Steer": "1", "X-Hub-Ts": ts, "X-Hub-Sig": "0" * 64}
    assert client.get("/gepnic/api/fleet", headers=h).status_code == 403
    from conftest import sig
    old = str(int(__import__("time").time()) - 120)
    h = {"X-Hub-User": "harsh", "X-Hub-Role": "viewer", "X-Hub-Steer": "0", "X-Hub-Ts": old,
         "X-Hub-Sig": sig(f"{old}\nharsh\nviewer\n0\nGET\n/gepnic/api/fleet")}
    assert client.get("/gepnic/api/fleet", headers=h).status_code == 403           # stale
    assert signed("", "").get("/gepnic/api/fleet").status_code == 401             # public visitor: only public paths


def test_fleet_load_from_the_runners_counters(client, clean, login):
    with db.conn() as c:
        c.execute("INSERT INTO runs (project, repository, gh_run_id, job_index, auth_kind, state) VALUES ('gepnic-wbtenders', 'r/x', '1', 0, 'oidc', 'running')")
        c.execute("INSERT INTO metrics (run_id, project, name, value) VALUES (1, 'gepnic-wbtenders', 'n_http', 300), (1, 'gepnic-wbtenders', 't_http_s', 450)")
    wb = login.get("/gepnic/api/fleet").json()["servers"][0]["portals"][0]
    assert wb["load"] == {"req_per_min": 20.0, "s_per_req": 1.5}


def test_journal_needs_the_file(client, clean, login, monkeypatch, tmp_path):
    from gepnic_app import app as gapp
    monkeypatch.setattr(gapp, "REPORTS", tmp_path)
    assert login.get("/gepnic/journal").status_code == 404                     # no journal on this server
    (tmp_path / "gepnic.html").write_text("<html><body><!--NAV--><p>the story</p></body></html>")
    r = login.get("/gepnic/journal")
    assert r.status_code == 200 and "the story" in r.text and 'href="/gepnic/journal" aria-current="page"' in r.text
    (tmp_path / "chart.png").write_bytes(b"\x89PNG fake")
    assert login.get("/reports/chart.png").content.startswith(b"\x89PNG") and login.get("/reports/chart.exe").status_code == 404
    assert client.get("/reports/chart.png").status_code == 403


def test_scrapes_traffic_throttle_and_adjustments_are_gepnic_only(client, clean, login):
    with db.conn() as c:
        c.execute("""INSERT INTO projects (name, settings) VALUES ('gepnic-wbtenders', '{"adaptive": {"max_windows": 20}, "probe": "gepnic:wbtenders.gov.in"}'),
                     ('mca-companies', '{"family": "mca", "adaptive": {}}')""")
        c.execute("""INSERT INTO adjustments (project, threads_from, threads_to, claimed_from, claimed_to, reason) VALUES
                     ('gepnic-wbtenders', 4, 4, 20, 24, 'auto: 10 healthy minutes at the ceiling: item cap 20 -> 24'),
                     ('mca-companies', 2, 2, 1, 2, 'auto: more')""")
    fams = {s["family"] for s in login.get("/gepnic/api/scrapes").json()["sources"]}
    assert fams <= {"gepnic", "eprocure"} and "gepnic" in fams
    items = login.get("/gepnic/api/adjustments").json()["items"]
    assert [i["source"] for i in items] == ["wbtenders"]
    assert [i["source"] for i in login.get("/gepnic/api/adjustments?caps=1").json()["items"]] == ["wbtenders"]   # new "item cap" wording
    t = login.get("/gepnic/api/throttle?hours=1").json()
    assert [s["project"] for s in t["sources"]] == ["gepnic-wbtenders"] and t["sources"][0]["caps"][-1][1] == 24
    assert "sources" in login.get("/gepnic/api/traffic").json() and login.get("/gepnic/api/traffic?minutes=60").json()["bucket"] == 1
