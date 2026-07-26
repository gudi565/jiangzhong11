"""Per-client character quota + redemption-code store for 降重.

No user accounts: a client is identified by a random cookie (jz_cid).
Each new client gets FREE_QUOTA chars; redeeming a code adds chars.
Codes are opaque strings (JZ-............) meant to be sold as 卡密 via
dujiaoka (独角数卡) and pasted into the redeem box.

State persists to state.json next to this file. A threading.Lock serializes
mutations; the (slow) LLM call happens outside the lock.
"""
import json
import os
import secrets
import threading
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"
FREE_QUOTA = 2000
ADMIN_SECRET = os.environ.get("JZ_ADMIN_SECRET", "dev-secret-change-me")

_lock = threading.Lock()


def _load() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"clients": {}, "codes": {}}


def _save(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, STATE_PATH)


def new_client_id() -> str:
    return secrets.token_urlsafe(18)


def _client(state, cid):
    c = state["clients"].get(cid)
    if c is None:
        c = {"remaining": FREE_QUOTA, "used": 0, "redeemed": []}
        state["clients"][cid] = c
    return c


def get_state_summary(cid: str) -> dict:
    with _lock:
        s = _load()
        c = _client(s, cid)
        out = {"remaining": c["remaining"], "used": c.get("used", 0), "free_quota": FREE_QUOTA}
        _save(s)
        return out


def check(cid: str, need: int):
    """True if client has enough quota (also creates/persists the client)."""
    with _lock:
        s = _load()
        c = _client(s, cid)
        ok = c["remaining"] >= need
        _save(s)
        return ok, c["remaining"]


def deduct(cid: str, n: int) -> int:
    with _lock:
        s = _load()
        c = _client(s, cid)
        c["remaining"] = max(0, c["remaining"] - n)
        c["used"] = c.get("used", 0) + n
        _save(s)
        return c["remaining"]


def redeem(cid: str, code: str) -> dict:
    with _lock:
        s = _load()
        c = _client(s, cid)
        code = (code or "").strip()
        cd = s["codes"].get(code)
        if not code or cd is None:
            _save(s)
            return {"ok": False, "error": "兑换码无效"}
        if cd.get("used"):
            _save(s)
            return {"ok": False, "error": "兑换码已被使用"}
        cd["used"] = True
        cd["used_by"] = cid
        c["remaining"] += cd["chars"]
        c.setdefault("redeemed", []).append(code)
        remaining = c["remaining"]
        _save(s)
        return {"ok": True, "added": cd["chars"], "remaining": remaining}


def gen_codes(n: int, chars: int) -> list:
    codes = []
    with _lock:
        s = _load()
        for _ in range(n):
            code = "JZ-" + secrets.token_hex(6).upper()
            s["codes"][code] = {"chars": chars, "used": False}
            codes.append(code)
        _save(s)
    return codes
