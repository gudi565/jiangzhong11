"""Per-client quota — supports TWO plan types simultaneously:

1) TIME plan (按天数): expires_at timestamp, unlimited uses during period.
2) USES plan (按次数): remaining_uses counter, each rewrite consumes 1.

A client can have both active at once (e.g., bought 7 days + 10 extra uses).
is_active = time not expired OR uses > 0.
consume() only decrements uses (time plans = unlimited).
"""
import json
import os
import secrets
import threading
import time
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"
LOCK_PATH = STATE_PATH.with_suffix(".lock")
FREE_TRIAL_SECONDS = 0
ADMIN_SECRET = os.environ.get("JZ_ADMIN_SECRET", "dev-secret-change-me")


class _FileLock:
    """双层锁：threading.Lock 锁进程内多线程（uvicorn 线程池并发请求），
    fcntl 锁跨进程（多 worker / 多进程部署）。
    单用 fcntl 不行——POSIX 下同进程对同一文件的多次 LOCK_EX 不互斥。"""
    _thread_lock = threading.Lock()

    def __enter__(self):
        self._thread_lock.acquire()
        self._f = open(LOCK_PATH, "w")
        try:
            import fcntl
            fcntl.flock(self._f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass  # Windows / 不支持 flock → 退化为仅线程锁（单进程仍安全）
        return self

    def __exit__(self, *a):
        try:
            import fcntl
            fcntl.flock(self._f, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self._f.close()
        self._thread_lock.release()


_lock = _FileLock()


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
        c = {"expires_at": 0, "remaining_uses": 0, "used": 0, "redeemed": []}
        state["clients"][cid] = c
    elif "remaining_uses" not in c:
        c["remaining_uses"] = 0
    return c


def is_active(cid: str):
    """Return (active, expires_at, remaining_uses)."""
    with _lock:
        s = _load()
        c = _client(s, cid)
        _save(s)
        return (c["expires_at"] > time.time() or c["remaining_uses"] > 0,
                c["expires_at"], c["remaining_uses"])


def consume(cid: str) -> dict:
    """Consume 1 use. Time-plan users don't consume (unlimited). Returns summary."""
    with _lock:
        s = _load()
        c = _client(s, cid)
        now = time.time()
        if c.get("expires_at", 0) > now:
            pass  # time plan active = unlimited
        elif c.get("remaining_uses", 0) > 0:
            c["remaining_uses"] -= 1
            c["used"] = c.get("used", 0) + 1
        exp = c.get("expires_at", 0)
        uses = c.get("remaining_uses", 0)
        _save(s)
        return _summary(exp, uses)


def _summary(exp, uses):
    now = time.time()
    time_active = exp > now
    uses_active = uses > 0
    return {
        "active": time_active or uses_active,
        "expires_at": exp,
        "remaining_seconds": max(0, int(exp - now)) if time_active else 0,
        "remaining_uses": uses,
        "time_active": time_active,
        "uses_active": uses_active,
    }


def get_state_summary(cid: str) -> dict:
    with _lock:
        s = _load()
        c = _client(s, cid)
        exp = c.get("expires_at", 0)
        uses = c.get("remaining_uses", 0)
        _save(s)
        return _summary(exp, uses)


def activate(cid: str, seconds: int) -> dict:
    """Time plan: extend expires_at."""
    with _lock:
        s = _load()
        c = _client(s, cid)
        base = max(c.get("expires_at", 0), time.time())
        c["expires_at"] = base + seconds
        exp = c["expires_at"]
        uses = c.get("remaining_uses", 0)
        _save(s)
        return _summary(exp, uses)


def activate_uses(cid: str, count: int) -> dict:
    """Uses plan: add count to remaining_uses."""
    with _lock:
        s = _load()
        c = _client(s, cid)
        c["remaining_uses"] = c.get("remaining_uses", 0) + count
        exp = c.get("expires_at", 0)
        uses = c["remaining_uses"]
        _save(s)
        return _summary(exp, uses)


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
        dur = cd.get("seconds", 0)
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


def create_order_record(order_id: str, cid: str, plan: str) -> None:
    with _lock:
        s = _load()
        s.setdefault("orders", {})[order_id] = {
            "cid": cid, "plan": plan, "status": "pending", "time": time.time(),
        }
        _save(s)


def mark_order_paid(order_id: str):
    with _lock:
        s = _load()
        o = s.get("orders", {}).get(order_id)
        if not o or o.get("status") == "paid":
            return None
        o["status"] = "paid"
        cid, plan = o["cid"], o["plan"]
        _save(s)
        return cid, plan


def get_order(order_id: str) -> dict:
    with _lock:
        s = _load()
        return s.get("orders", {}).get(order_id)
