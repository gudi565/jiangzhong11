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
# 不计入每 cid 通用记录上限的内部 task（write_version 快照有独立的 10 版上限）
EXEMPT_TASKS = ("write_version",)


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


def add(cid: str, task: str, orig_text: str, result: dict, max_per_cid: int = None) -> str:
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
    limit = max_per_cid if max_per_cid is not None else MAX_PER_CID
    with _lock:
        state = _load()
        # write_version 等内部快照不计入通用记录上限（否则免费快照会挤掉付费任务历史）
        counting = [r for r in state["records"].values()
                    if r.get("cid") == cid and r.get("task") not in EXEMPT_TASKS]
        if task not in EXEMPT_TASKS and len(counting) >= limit:
            oldest = min(counting, key=lambda r: r.get("ts", 0))
            state["records"].pop(oldest.get("id"), None)
        state["records"][hid] = _slim(record)
        _save(state)
    return hid


def list_summaries(cid: str, task: str = None) -> list:
    with _lock:
        state = _load()
    items = []
    for r in state["records"].values():
        if r.get("cid") != cid:
            continue
        if task is not None and r.get("task") != task:
            continue
        if task is None and r.get("task") in EXEMPT_TASKS:
            continue  # 内部快照不进通用历史列表
        chars = len(r.get("orig_text", ""))
        if r.get("task") in EXEMPT_TASKS and isinstance(r.get("result", {}).get("full_text"), str):
            chars = len(r["result"]["full_text"])  # 快照字数=全文长度，而非题目
        elif not chars:
            rw = r.get("result", {}).get("rewrite", "")
            chars = len(rw) if isinstance(rw, str) else 0
        label = r.get("result", {}).get("label")
        items.append({"id": r["id"], "task": r.get("task", ""), "title": r.get("title", ""),
                      "ts": r.get("ts", 0), "chars": chars,
                      **({"label": label} if label else {})})
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
