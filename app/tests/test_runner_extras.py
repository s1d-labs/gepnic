"""The GePNIC crawler's runner extras, as the hub forwards them (POST /hub/runs/{run_id}/<ext>, signed with the run)."""
import json

from conftest import run_headers
from gepnic_app import db

REPO = "s1d-labs/scrape-runner"


def run(project, repo=REPO):
    with db.conn() as c:
        c.execute("INSERT INTO projects (name) VALUES (%s) ON CONFLICT DO NOTHING", (project,))
        return c.execute("INSERT INTO runs (project, repository, gh_run_id, job_index, auth_kind) VALUES (%s, %s, %s, 0, 'oidc') RETURNING id",
                         (project, repo, project)).fetchone()["id"]


def store(project, records):
    with db.conn() as c:
        c.execute("INSERT INTO projects (name) VALUES (%s) ON CONFLICT DO NOTHING", (project,))
        for rid, data in records:
            c.execute("INSERT INTO records (project, record_id, data) VALUES (%s, %s, %s)", (project, rid, json.dumps(data)))


def call(client, rid, project, ext, body, repo=REPO):
    return client.post(f"/hub/runs/{rid}/{ext}", json=body, headers=run_headers(rid, project, repo, ext))


def test_known_records_returns_only_real_captures(client, clean):
    rid = run("gepnic-resuming")
    win = ["01/01/2019", "28/02/2019"]
    store("gepnic-resuming", [
        ("NEW1", {"details": {"fields": {"tender_title": "Road works"}}}),
        ("EMPTY1", {"details": {"fields": {"tender_title": "", "tender_type": ""}}}),          # the portal's 'session expired' page parsed
        ("OLD1", {"details": {"bids": []}}),                                                   # stored by an older parser: no fields
        ("S2", {"org_id": "9", "window": win, "details": {"fields": {"tender_title": "Roads"}, "sections": {"covers": []}}}),   # 'Stale Session'
        ("S3", {"org_id": "9", "window": win, "details": {"fields": {"tender_title": "Roads"}}}),                       # before sections existed
        ("ND", {"details": {"fields": {"tender_title": "x"}, "documents": [{"status": "not_stored"}]}})])                # saved without documents
    out = call(client, rid, "gepnic-resuming", "known-records", {"ids": ["NEW1", "EMPTY1", "OLD1", "S2", "S3", "ND", "NEVER"]}).json()
    assert sorted(out["known"]) == ["NEW1", "S3"]


def test_known_records_is_per_project(client, clean):
    a, b = run("gepnic-a"), run("gepnic-b")
    store("gepnic-a", [("A", {"details": {"fields": {"tender_title": "x"}}})])
    assert call(client, b, "gepnic-b", "known-records", {"ids": ["A"]}).json() == {"known": []}
    assert call(client, a, "gepnic-a", "known-records", {"ids": ["A"]}).json() == {"known": ["A"]}


def test_window_stored_counts_real_records_of_one_window(client, clean):
    rid = run("gepnic-windows")
    win = ["01/01/2019", "28/02/2019"]
    good = lambda i, w=win, org="159": (f"G{i}{org}{w[0]}", {"org_id": org, "window": w, "details": {"fields": {"tender_title": "Road"}}})
    store("gepnic-windows", [good(1), good(2), ("E1", {"org_id": "159", "window": win, "details": {"fields": {"tender_title": ""}}}),
                             good(3, ["01/03/2019", "30/04/2019"]), good(4, org="160")])
    assert call(client, rid, "gepnic-windows", "window-stored", {"org_id": "159", "from_date": win[0], "to_date": win[1]}).json() == {"stored": 2}


def test_backfill_todo_and_patch_leave_other_fields_alone(client, clean):
    rec = lambda i, extra=None: (f"2019_X_{i}_1", {"org_id": "5", "details": {"pages_key": f"t/pages/2019/5/2019_X_{i}_1.json.gz", "fields": {"tender_title": f"T{i}"}, **(extra or {})}})
    store("gepnic-tenders", [rec(1), rec(2, {"sections": {"nit_documents": []}}),
                             ("2020_X_3_1", {"org_id": "5", "details": {"pages_key": "t/pages/2020/5/x.json.gz"}}),
                             ("2019_X_4_1", {"org_id": "6", "details": {"pages_key": "t/pages/2019/6/y.json.gz"}})])
    back = run("gepnic-backfill")
    todo = call(client, back, "gepnic-backfill", "backfill/todo", {"target": "gepnic-tenders", "org_id": "5", "year": "2019"}).json()["records"]
    assert [r["record_id"] for r in todo] == ["2019_X_1_1"] and todo[0]["pages_key"] == "t/pages/2019/5/2019_X_1_1.json.gz"
    out = call(client, back, "gepnic-backfill", "patch-records",
               {"target": "gepnic-tenders", "patches": [{"record_id": "2019_X_1_1", "value": {"nit_documents": [{"name": "a.pdf"}]}}]}).json()
    assert out == {"patched": 1}
    with db.conn() as c:
        d = c.execute("SELECT data FROM records WHERE project='gepnic-tenders' AND record_id='2019_X_1_1'").fetchone()["data"]
        n = c.execute("SELECT records, batches FROM runs WHERE id=%s", (back,)).fetchone()
    assert d["details"]["sections"]["nit_documents"][0]["name"] == "a.pdf" and d["details"]["fields"]["tender_title"] == "T1" and d["org_id"] == "5"
    assert n["records"] == 1 and n["batches"] == 1                                # the backfill's own run shows the work
    assert call(client, back, "gepnic-backfill", "backfill/todo", {"target": "gepnic-tenders", "org_id": "5", "year": "2019"}).json()["records"] == []
    store("mca-companies", [])                                                    # another family's project is refused
    assert call(client, back, "gepnic-backfill", "patch-records", {"target": "mca-companies", "patches": [{"record_id": "x", "value": {}}]}).status_code == 403


def test_runner_extras_need_the_hubs_signature(client, clean):
    rid = run("gepnic-sig")
    assert client.post(f"/hub/runs/{rid}/known-records", json={"ids": []}).status_code == 403
    h = run_headers(rid, "gepnic-sig", REPO, "known-records")
    h["X-Hub-Run-Project"] = "gepnic-other"                                       # not what the hub signed
    assert client.post(f"/hub/runs/{rid}/known-records", json={"ids": []}, headers=h).status_code == 403
    h = run_headers(rid, "gepnic-sig", REPO, "window-stored")                     # signed for another extra
    assert client.post(f"/hub/runs/{rid}/known-records", json={"ids": []}, headers=h).status_code == 403
    h = run_headers(rid + 1, "gepnic-sig", REPO, "known-records")                 # signed for another run
    assert client.post(f"/hub/runs/{rid}/known-records", json={"ids": []}, headers=h).status_code == 403
    other = run("mca-x")
    assert call(client, other, "mca-x", "known-records", {"ids": []}).status_code == 403   # not a GePNIC run


def test_tender_lookup_finds_by_id_prefix(client, clean, login):
    store("x-tenders", [
        ("2025_DRDO_875304_1", {"org_name": "DRDO", "details": {"fields": {"tender_title": "Roads", "tender_value": "5"},
          "sections": {"nit_documents": [{"name": "a.pdf"}]}, "documents": [{"name": "f.pdf", "kind": "document", "status": "refused", "reason": "x"}]}}),
        ("2025_DRDO_875999_1", {"org_name": "DRDO", "details": {"fields": {"tender_title": "Other"}}})])
    out = login.get("/gepnic/api/tender?q=2025_DRDO_8753").json()
    assert out["matches"] == ["2025_DRDO_875304_1"] and out["tender"]["fields"]["tender_title"] == "Roads"
    assert out["tender"]["sections"]["nit_documents"][0]["name"] == "a.pdf" and out["tender"]["documents"][0]["status"] == "refused"
    both = login.get("/gepnic/api/tender?q=2025_DRDO_875").json()
    assert len(both["matches"]) == 2 and both["tender"]["record_id"] == "2025_DRDO_875304_1"
    assert login.get("/gepnic/api/tender?q=25").json() == {"matches": [], "tender": None}
    assert login.get("/gepnic/api/tender?q=2025_%25").json()["matches"] == []
