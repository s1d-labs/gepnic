"""Tests run against a throwaway Postgres (pgserver) holding the hub tables this app uses (hub_tables.sql) plus the app's own schema.

Requests are signed the way scrape-hub signs them (docs/family-apps.md in s1d-labs/scrape-hub): `login` is the gepnic family owner,
`signed(user, role, steer)` anyone else, `public` a visitor on a public path, `client` sends nothing signed at all.
"""
import hashlib
import hmac
import os
import sys
import tempfile
import time
from urllib.parse import urlsplit

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                                  # app/ (gepnic_app)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "jobs"))

import pgserver  # noqa: E402

_tmp = tempfile.mkdtemp()
_pg = pgserver.get_server(os.path.join(_tmp, "pg"))
os.environ["DATABASE_URL"] = _pg.get_uri()
os.environ["GEPNIC_NO_COUNTER"] = "1"
SECRET = b"test-hub-secret"
open(os.path.join(_tmp, "hub.secret"), "wb").write(SECRET)
os.environ["HUB_APP_SECRET_FILE"] = os.path.join(_tmp, "hub.secret")
os.environ["GEPNIC_REPORTS_DIR"] = os.path.join(_tmp, "reports")
os.environ.update(R2_ENDPOINT="https://acct.r2.example.com", R2_BUCKET="test-bucket", R2_ACCESS_KEY_ID="AKIATEST", R2_SECRET_ACCESS_KEY="secret")
os.environ.update(B2_ENDPOINT="https://s3.us-east-005.backblazeb2.com", B2_REGION="us-east-005", B2_BUCKET="b2-test-bucket", B2_ACCESS_KEY_ID="b2key", B2_SECRET_ACCESS_KEY="b2secret")

import psycopg  # noqa: E402

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as _c:
    _c.execute(open(os.path.join(HERE, "hub_tables.sql")).read())

from fastapi.testclient import TestClient  # noqa: E402

from gepnic_app import db  # noqa: E402
from gepnic_app.app import app  # noqa: E402


def sig(msg: str) -> str:
    return hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()


class Signed(TestClient):
    """A client whose every request carries the hub's signed headers for one person."""

    def __init__(self, app, user="sarthak", role="owner", steer=True):
        super().__init__(app)
        self.user, self.role, self.steer = user, role, steer

    def request(self, method, url, **kw):
        ts = str(int(time.time()))
        st = "1" if self.steer else "0"
        path = urlsplit(str(url)).path
        h = {"X-Hub-User": self.user, "X-Hub-Role": self.role, "X-Hub-Steer": st, "X-Hub-Ts": ts,
             "X-Hub-Sig": sig(f"{ts}\n{self.user}\n{self.role}\n{st}\n{method.upper()}\n{path}")}
        kw["headers"] = {**h, **(kw.get("headers") or {})}
        return super().request(method, url, **kw)


def run_headers(run_id, project, repo, ext):
    ts = str(int(time.time()))
    return {"X-Hub-Run-Id": str(run_id), "X-Hub-Run-Project": project, "X-Hub-Run-Repo": repo, "X-Hub-Ts": ts,
            "X-Hub-Sig": sig(f"{ts}\nrun\n{run_id}\n{project}\n{repo}\n{ext}")}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def clean(client):
    with db.conn() as c:
        c.execute("TRUNCATE bundles, bundle_attempts, probes, capacity, showcase, adjustments, failures, metrics, batches, records, queue, runs, projects, sizes, upload_ledger, bucket_usage, record_counts RESTART IDENTITY CASCADE")


@pytest.fixture
def signed(client):
    return lambda user="ronik", role="member", steer=False: Signed(app, user, role, steer)


@pytest.fixture
def login(client, clean):
    return Signed(app, "sarthak", "owner", True)


@pytest.fixture
def public(client):
    return Signed(app, "", "", False)
