"""Time-based access + redemption-code store for 降重.

A client (cookie jz_cid) has an `expires_at` unix timestamp. Access is allowed
while now < expires_at. Redeeming a code extends expires_at by the code's
duration (default 1 hour). Codes (JZ-............) are sold as 卡密 on
Taobao / 独角数卡; the buyer pastes one to activate.

State persists to state.json next to this file. A threading.Lock serializes
mutations; the slow LLM call happens outside the lock.
"""
import json
import os
import secrets
import threading
import time
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"
FREE_TRIAL_SECONDS = 0          # 0 = 必须有兑换码才能用；设 600 可给新访客 10 分钟试用
DEFAULT_CODE_SECONDS = 3600     # 一个兑换码默认 = 1 小时
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
        c = {"expires_at": time.time() + FREE_TRIAL_SECONDS, "redeemed": []}
        state["clients"][cid] = c
    elif "expires_at" not in c:
        c["expires_at"] = time.time() + FREE_TRIAL_SECONDS
    return c


def is_active(cid: str):
    """Return (active: bool, expires_at: float)."""
    with _lock:
        s = _load()
        c = _client(s, cid)
        _save(s)
        return c["expires_at"] > time.time(), c["expires_at"]


def get_state_summary(cid: str) -> dict:
    with _lock:
        s = _load()
        c = _client(s, cid)
        _save(s)
        exp = c["expires_at"]
        return {"active": exp > time.time(), "expires_at": exp,
                "remaining_seconds": max(0, int(exp - time.time()))}


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
        dur = cd.get("seconds", DEFAULT_CODE_SECONDS)
        base = max(c.get("expires_at", 0), time.time())
        c["expires_at"] = base + dur
        c.setdefault("redeemed", []).append(code)
        remaining = int(c["expires_at"] - time.time())
        _save(s)
        return {"ok": True, "added_seconds": dur, "active": True,
                "remaining_seconds": remaining, "expires_at": c["expires_at"]}


def gen_codes(n: int, seconds: int) -> list:
    codes = []
    with _lock:
        s = _load()
        for _ in range(n):
            code = "JZ-" + secrets.token_hex(6).upper()
            s["codes"][code] = {"seconds": seconds, "used": False}
            codes.append(code)
        _save(s)
    return codes
