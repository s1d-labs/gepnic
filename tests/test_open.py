"""open_tenders.py on saved pages of uktenders.gov.in (2026-11-13): the organisation list, one organisation's active tenders, a tender page
and the captcha page the NIT link opens."""
import os
import sys

os.environ.setdefault("PORTAL", "uktenders.gov.in")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import open_tenders as ot  # noqa: E402
import parsing  # noqa: E402
import portal  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "open")


@pytest.fixture(autouse=True)
def as_uktenders(monkeypatch):
    """Other test modules may have imported portal for eprocure.gov.in first; these pages are uktenders.gov.in's."""
    monkeypatch.setattr(portal, "ORIGIN", "https://uktenders.gov.in")
    monkeypatch.setattr(portal, "LEGACY", False)


def load(name):
    return open(os.path.join(FIX, name), encoding="utf-8", errors="ignore").read()


def test_organisation_list_has_every_organisation_with_its_count():
    orgs = ot.org_links(load("uk_by_organisation.html"))
    assert len(orgs) == 76 and sum(o["count"] for o in orgs) == 760
    urrda = [o for o in orgs if o["org"] == "CEO (Uttarakhand Rural Roads Development Agency)"][0]
    assert urrda["count"] == 2 and urrda["url"].startswith("https://uktenders.gov.in/nicgep/app?component=%24DirectLink&page=FrontEndTendersByOrganisation")


def test_organisation_tenders_are_parsed_with_id_title_and_dates():
    rows = ot.tender_rows(load("uk_org_tenders.html"))
    assert [r["tender_id"] for r in rows] == ["2026_URRDA_102208_1", "2026_URRDA_90559_1"]
    r = rows[0]
    assert r["title"].startswith("Repair of the disaster damaged motor road from Jetha to Kotyura")
    assert r["reference_no"] == "843/E-NIVIDA/PMGSY/2026"
    assert (r["published"], r["closing"], r["opening"]) == ("06-Nov-2026 06:00 PM", "11-Dec-2026 05:00 PM", "12-Dec-2026 01:00 PM")
    assert r["org_chain"] == "CEO (Uttarakhand Rural Roads Development Agency)"
    assert "page=FrontEndViewTender" in r["view_link"] and "&amp;" not in r["view_link"]


def test_road_filter():
    assert ot.is_road({"title": "Improvement of Lakhi To Sarpaani Motor Road"})
    assert ot.is_road({"title": "Construction of CC Road and culvert"})
    assert ot.is_road({"title": "Strengthening of NH-58 km 120"})
    assert not ot.is_road({"title": "Supply of laboratory chemicals"})
    assert not ot.is_road({"title": "Broadband services"})          # "road" only as a whole word


def test_tender_page_documents_nit_has_a_link_work_items_do_not():
    docs = ot.tender_documents(load("uk_tender_2026_URRDA_102208_1.html"))
    assert [(d["name"], d["kind"], d["type"]) for d in docs] == [
        ("Tendernotice_1.pdf", "nit", "NIT"), ("BidJaithaapda.pdf", "work_item", "Tender Documents"), ("BOQ_120372.xls", "work_item", "BOQ")]
    assert docs[0]["link"].endswith("component=docDownoad&page=FrontEndTenderDetails&service=direct&session=T")
    assert docs[1]["link"] is None and docs[2]["link"] is None
    assert docs[2]["declared_bytes"] == int(313.50 * 1024)


def test_tender_page_fields_parse_with_the_status_crawl_parser():
    f, _, _ = parsing.parse_tender("2026_URRDA_102208_1", "", "", {}, "", load("uk_tender_2026_URRDA_102208_1.html"), "", [])
    assert f["tender_title"].startswith("Repair of the disaster damaged motor road")
    assert (f["location"], f["pincode"], f["period_of_work_days"]) == ("Chaukhutia", "263656", 270)
    assert f["bid_submission_end_date"] == "11-Dec-2026 05:00 PM"


def test_captcha_page_is_recognised_and_a_pdf_is_not():
    assert ot.is_captcha(load("uk_document_captcha.html").encode())
    assert not ot.is_captcha(b"%PDF-1.4 ...")


class FakeHub:
    def __init__(self):
        self.records, self.metrics, self.uploads = {}, {}, []

    def metric(self, k, n=1):
        self.metrics[k] = self.metrics.get(k, 0) + n

    def note(self, t):
        pass

    def error(self, *a):
        pass

    def block(self, *a):
        pass

    def record(self, rid, data):
        self.records[rid] = data

    def upload_many(self, files):
        self.uploads += files
        return [{"key": "gepnic-uktenders-open/" + files[0][0]}]


class FakeResponse:
    def __init__(self, body):
        self.content, self.text, self.status_code = body, body.decode("utf-8", "ignore"), 200


class FakePortal:
    """Serves the saved tender page for the view link and the saved captcha page for the NIT link; counts requests."""

    def __init__(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        name = "uk_document_captcha.html" if "docDownoad" in url else "uk_tender_2026_URRDA_102208_1.html"
        return FakeResponse(load(name).encode())

    def fresh(self):
        pass


def test_tender_item_stores_the_page_and_stops_asking_for_documents_after_a_captcha():
    ot.Docs.state = None
    hub, P = FakeHub(), FakePortal()
    row = ot.tender_rows(load("uk_org_tenders.html"))[0]
    d = ot.run_tender(hub, P, {"key": "t-2026_URRDA_102208_1", "payload": {**row, "org": "URRDA", "seen": "2026-11-13"}})
    assert [e["status"] for e in d["documents"]] == ["captcha", "no_public_link", "no_public_link"]
    assert d["doc_access"] == "wall" and hub.metrics["doc_captcha_wall"] == 1
    assert hub.uploads[0][0].startswith("tenders/2026/URRDA/2026_URRDA_102208_1__") and hub.uploads[0][0].endswith(".tar")
    rec = hub.records["2026_URRDA_102208_1"]
    assert rec["details"]["fields"]["pincode"] == "263656" and rec["seen_open"] == "2026-11-13"
    assert rec["details"]["pages_key"].startswith("gepnic-uktenders-open/tenders/")
    # a second tender in the same process does not open the NIT link again
    n = len(P.urls)
    ot.run_tender(hub, P, {"key": "t-2026_URRDA_102208_1", "payload": {**row, "org": "URRDA", "seen": "2026-11-13"}})
    assert len(P.urls) == n + 1
    ot.Docs.state = None
