"""AIGC (AI-generated content) heuristic detector — lightweight, no model.

Returns a 0-100 'AIGC 概率' estimate from text statistics. This is a rough
reference signal only — AIGC detectors are notoriously unreliable (high false
positives), so the output must always be labeled 估算/参考.
"""
import re

AI_TELL_PHRASES = [
    "首先", "其次", "此外", "另外", "最后",
    "综上所述", "由此可见", "值得注意的是", "总的来说", "总而言之", "换言之", "简而言之",
    "一方面", "另一方面", "不仅如此",
    "众所周知", "毫无疑问", "不言而喻",
    "至关重要", "举足轻重", "日新月异", "蓬勃发展", "应运而生",
    "具有重要意义", "发挥了重要作用", "提供了有益参考", "具有重要价值",
    "在当今",
]

_PUNCT = set("，。！？；：、, . ! ? ; : 、 （ ） 《 》 “ ” ‘ ’ "" '' - — …")


def _sentences(text):
    return [s.strip() for s in re.split(r"[。！？\n;；]+", text) if s.strip()]


def _clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def _bg(s):
    s = re.sub(r"\s+", "", s)
    return set(s[i:i + 2] for i in range(len(s) - 1)) if len(s) > 1 else ({s} if s else set())


def _containment(needle, haystack):
    """How much of `needle`'s bigrams appear in `haystack` (0-1)."""
    A, B = _bg(needle), _bg(haystack)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A)


def check_plagiarism(text: str, max_checks: int = 8) -> dict:
    """逐句在 DuckDuckGo 上搜，比对公开网络文本的重叠。只抓得到'复制自网络'
    的雷同，抓不到'抄自知网/万方等闭源库'的内容。≠ 知网查重率。"""
    text = text or ""
    sents = _sentences(text)
    # 优先检查较长、较可能独特的句子
    candidates = sorted(sents, key=lambda s: len(re.sub(r"\s+", "", s)), reverse=True)[:max_checks]

    try:
        from duckduckgo_search import DDGS
    except Exception:
        return {"error": "服务器未装搜索依赖（duckduckgo-search）"}

    matches = []
    checked = 0
    any_success = False
    for s in candidates:
        q = re.sub(r"\s+", " ", s).strip()[:70]
        if len(q) < 8:
            continue
        checked += 1
        # 每次新建 DDGS + 重试 3 次，规避 DDG 偶发连接错误（如 h11 的 0x304）
        results = []
        for _ in range(3):
            try:
                results = list(DDGS().text(q, max_results=3))
                any_success = True
                break
            except Exception:
                results = []
        best = None
        for r in results:
            blob = " ".join([r.get("title", ""), r.get("body", ""), r.get("snippet", "")]).strip()
            ov = _containment(s, blob)
            if best is None or ov > best[0]:
                best = (ov, r)
        if best and best[0] >= 0.5:
            matches.append({
                "sentence": s,
                "overlap": round(best[0] * 100),
                "title": (best[1].get("title", "") or "")[:80],
                "url": best[1].get("href") or best[1].get("url") or "",
            })

    if checked > 0 and not any_success:
        return {"error": "搜索服务暂时不可用，请稍后重试"}

    score = round(len(matches) / max(checked, 1) * 100)
    if score < 20:
        verdict, color = "未发现明显网络雷同", "ok"
    elif score < 50:
        verdict, color = "部分内容与网络雷同", "warn"
    else:
        verdict, color = "大量内容与网络雷同", "err"

    return {
        "similarity_score": score,
        "verdict": verdict,
        "color": color,
        "matched_count": len(matches),
        "checked_count": checked,
        "matches": matches[:8],
        "note": ("逐句比对公开互联网（DuckDuckGo）。能发现「复制粘贴自网络」的雷同，"
                 "抓不到「抄自 知网/万方 等闭源库」的内容。≠ 知网查重率，仅供参考。"
                 "权威查重率请以 cx.cnki.net 官方结果为准。"),
    }


def detect_aigc(text: str) -> dict:
    text = text or ""
    chars = len(re.sub(r"\s+", "", text))
    sents = _sentences(text)

    # 1) burstiness: coefficient of variation of sentence lengths
    if len(sents) >= 2:
        lens = [len(re.sub(r"\s+", "", s)) for s in sents]
        mean = sum(lens) / len(lens)
        std = (sum((l - mean) ** 2 for l in lens) / len(lens)) ** 0.5
        cv = std / mean if mean > 0 else 0
    else:
        cv = 0.4  # neutral when too few sentences
    burst_score = _clamp(90 - (cv - 0.2) / 0.35 * 80)  # cv≤0.2→90(AI), cv≥0.55→10(human)

    # 2) AI-tell phrase density per 100 chars (trimmed list — only formulaic ones)
    tell_count = sum(text.count(p) for p in AI_TELL_PHRASES)
    density = tell_count / max(chars, 1) * 100
    tell_score = _clamp((density - 1.0) / 3.0 * 100)  # ≤1/100→human, ≥4/100→AI

    # 3) punctuation variety
    ptypes = len(set(re.findall(r"[，。！？；：、,.!?;:、（） 《》“”‘’…—]", text)))
    punct_score = _clamp(80 - (ptypes - 2) / 3 * 70)  # ≤2种→80, ≥5种→10

    # 4) informality / personal-voice markers (humans use ？！、——、我/我们/我猜/有点…)
    informal_count = len(re.findall(r"[？！…—]|我(们)?|觉得|猜测|感觉|应该|估计|大概|好像|或许|有点|挺|蛮", text))
    informal_score = _clamp(70 - min(informal_count, 2) / 2 * 70)  # 0 处→70(AI), ≥2 处→0(human)

    score = round(burst_score * 0.30 + tell_score * 0.35 + punct_score * 0.15 + informal_score * 0.20)
    score = _clamp(score, 3, 95)  # never claim certainty

    if score < 35:
        verdict, color = "偏低（偏人类写作）", "ok"
    elif score < 65:
        verdict, color = "中等（不确定）", "warn"
    else:
        verdict, color = "偏高（偏 AI 生成）", "err"

    short = chars < 80 or len(sents) < 3
    note = ("样本较短，结果仅供参考，可信度有限。"
            if short else
            "基于文本统计特征估算。AIGC 检测器普遍存在误判，本结果仅供参考，不作为定论。")

    return {
        "aigc_score": score,
        "verdict": verdict,
        "color": color,
        "note": note,
        "signals": [
            {"name": "句长起伏", "value": f"变异系数 {cv:.2f}", "score": round(burst_score),
             "hint": "句子长度较均匀（偏 AI）" if burst_score > 55 else "句长起伏较自然（偏人）"},
            {"name": "AI 套话密度", "value": f"{density:.1f} 处/100 字（共 {tell_count}）", "score": round(tell_score),
             "hint": "套话偏多（偏 AI）" if tell_score > 55 else "套话不多（偏人）"},
            {"name": "标点多样性", "value": f"{ptypes} 种", "score": round(punct_score),
             "hint": "标点较单一（偏 AI）" if punct_score > 55 else "标点较丰富（偏人）"},
            {"name": "口语 / 个人语气", "value": f"{informal_count} 处", "score": round(informal_score),
             "hint": "较书面（偏 AI）" if informal_score > 45 else "有口语 / 个人感（偏人）"},
        ],
        "sentence_count": len(sents),
        "char_count": chars,
    }
