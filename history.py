"""降重历史记录 — 每个浏览器（cid）最近 20 条任务结果，history.json 落盘。

复用 quota 的 _FileLock（双层锁）+ tmp+os.replace 原子写模式。
记录体积超 60KB 依次瘦身（删 sentence_scores/matches 等派生字段），
rewrite/rewrites（核心交付物）永远保留。
"""
import json
import os
import secrets
import time
from pathlib import Path

from quota import _FileLock

HISTORY_PATH = Path(__file__).parent / "history.json"
_lock = _FileLock(str(HISTORY_PATH.with_suffix(".hlock")))

MAX_PER_CID = 20
MAX_RECORD_BYTES = 60 * 1024
TRUNCATE_CHARS = 20000

# 超限时依次删除的 result 派生字段（按可牺牲程度排序）
SLIM_FIELDS = ("sentence_scores", "matches", "diagnostics", "signals", "stages")


def _load() -> dict:
    if HISTORY_PATH.exists():
        try:
            data = json.loads(HISTORY_PATH.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("records"), dict):
                return data
        except Exception:
            pass
    return {"records": {}}


def _save(state: dict) -> None:
    tmp = HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    os.replace(tmp, HISTORY_PATH)


def _size(record: dict) -> int:
    return len(json.dumps(record, ensure_ascii=False).encode("utf-8"))


def _slim(record: dict) -> dict:
    result = record.get("result")
    if isinstance(result, dict):
        for field in SLIM_FIELDS:
            if _size(record) <= MAX_RECORD_BYTES:
                return record
            if field in result:
                result[field] = []
        if _size(record) > MAX_RECORD_BYTES and isinstance(result.get("full_text"), str):
            result["full_text"] = result["full_text"][:TRUNCATE_CHARS]
    if _size(record) > MAX_RECORD_BYTES and isinstance(record.get("orig_text"), str):
        record["orig_text"] = record["orig_text"][:TRUNCATE_CHARS]
    return record


def add(cid: str, task: str, orig_text: str, result: dict) -> str:
    hid = secrets.token_hex(6)
    title = (orig_text or "").strip().replace("\n", " ")[:20] or task
    record = {
        "id": hid,
        "cid": cid,
        "task": task,
        "title": title,
        "ts": time.time(),
        "orig_text": orig_text or "",
        "result": result or {},
    }
    with _lock:
        state = _load()
        state["records"][hid] = _slim(record)
        mine = sorted(
            (r for r in state["records"].values() if r.get("cid") == cid),
            key=lambda r: r.get("ts", 0),
        )
        for old in mine[:-MAX_PER_CID]:
            state["records"].pop(old.get("id"), None)
        _save(state)
    return hid


def list_summaries(cid: str) -> list:
    with _lock:
        state = _load()
    items = [
        {
            "id": r["id"], "task": r.get("task", ""), "title": r.get("title", ""),
            "ts": r.get("ts", 0),
            "chars": len(r.get("orig_text", "")) or len(r.get("result", {}).get("rewrite", "") or ""),
        }
        for r in state["records"].values() if r.get("cid") == cid
    ]
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


def get(cid: str, hid: str):
    with _lock:
        state = _load()
    r = state["records"].get(hid)
    if not r or r.get("cid") != cid:
        return None
    return r


def delete(cid: str, hid: str) -> bool:
    with _lock:
        state = _load()
        r = state["records"].get(hid)
        if not r or r.get("cid") != cid:
            return False
        del state["records"][hid]
        _save(state)
    return True
