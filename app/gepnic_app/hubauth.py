"""Requests come only through scrape-hub, which signs who is asking (s1d-labs/scrape-hub docs/family-apps.md).

Pages and APIs: X-Hub-User, X-Hub-Role (owner|member|viewer, "" on a public path), X-Hub-Steer (1/0), X-Hub-Ts, X-Hub-Sig =
HMAC-SHA256(secret, "{ts}\\n{user}\\n{role}\\n{steer}\\n{method}\\n{path}"). Runner extras: X-Hub-Run-Id, X-Hub-Run-Project,
X-Hub-Run-Repo, X-Hub-Ts, X-Hub-Sig = HMAC-SHA256(secret, "{ts}\\nrun\\n{run_id}\\n{project}\\n{repo}\\n{ext}").
Anything unsigned, wrongly signed or more than 60 s off is refused with 403.
"""
import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from . import config

_secret = {"v": None}


def secret() -> bytes:
    if _secret["v"] is None:
        try:
            _secret["v"] = open(config.HUB_APP_SECRET_FILE, "rb").read().strip()
        except OSError:
            _secret["v"] = b""
    if not _secret["v"]:
        raise HTTPException(503, "this app has no hub secret configured")
    return _secret["v"]


def sign(msg: str) -> str:
    return hmac.new(secret(), msg.encode(), hashlib.sha256).hexdigest()


def _check(request: Request, msg_tail: str):
    ts = request.headers.get("x-hub-ts", "")
    try:
        if abs(time.time() - int(ts)) > config.SIG_WINDOW_S:
            raise ValueError
    except ValueError:
        raise HTTPException(403, "not signed by the hub (or the signature is stale)")
    if not hmac.compare_digest(request.headers.get("x-hub-sig", ""), sign(f"{ts}\n{msg_tail}")):
        raise HTTPException(403, "not signed by the hub")


def visitor(request: Request) -> dict:
    """{"u", "role", "steer"} of a signed request; role is "" on a public path."""
    u, role, steer = (request.headers.get(h, "") for h in ("x-hub-user", "x-hub-role", "x-hub-steer"))
    _check(request, f"{u}\n{role}\n{steer}\n{request.method}\n{request.url.path}")
    return {"u": u, "role": role, "steer": steer == "1"}


def need_user(request: Request) -> dict:
    """A signed-in person (viewers included)."""
    v = visitor(request)
    if v["role"] not in ("owner", "member", "viewer") or not v["u"]:
        raise HTTPException(401, "login required")
    return v


def need_member(request: Request) -> dict:
    v = need_user(request)
    if v["role"] == "viewer":
        raise HTTPException(403, "viewers can look but not change things")
    return v


def need_steer(request: Request) -> dict:
    v = need_member(request)
    if not v["steer"]:
        raise HTTPException(403, "only the gepnic family owner (or the hub owner) can do this")
    return v


def run_caller(request: Request, run_id: int, ext: str) -> dict:
    """The run behind a runner extra: {"run_id", "project", "repo"}."""
    rid, project, repo = (request.headers.get(h, "") for h in ("x-hub-run-id", "x-hub-run-project", "x-hub-run-repo"))
    if rid != str(run_id):
        raise HTTPException(403, "run id does not match the hub's")
    _check(request, f"run\n{rid}\n{project}\n{repo}\n{ext}")
    return {"run_id": run_id, "project": project, "repo": repo}


def client_ip(request: Request) -> str:
    """The visitor's address as the hub passed it on (Cloudflare's header first), else the connection's."""
    for h in ("cf-connecting-ip", "x-forwarded-for"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else ""
