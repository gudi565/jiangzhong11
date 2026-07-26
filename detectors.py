"""AIGC (AI-generated content) heuristic detector — lightweight, no model.

Returns a 0-100 'AIGC 概率' estimate from text statistics. This is a rough
reference signal only — AIGC detectors are notoriously unreliable (high false
positives), so the output must always be labeled 估算/参考.
"""
import re

AI_TELL_PHRASES = [
    "首先", "其次", "此外", "另外", "最后", "综上所述", "由此可见", "值得注意的是",
    "总的来说", "总而言之", "总之", "不仅", "而且", "一方面", "另一方面", "众所周知",
    "随着", "在当今", "基于", "因此", "然而", "同时", "并且", "进而", "从而",
    "毫无疑问", "不言而喻", "显然", "换言之", "简而言之", "至关重要", "举足轻重",
    "日新月异", "蓬勃发展", "应运而生", "具有重要意义", "发挥了重要作用", "提供了有益参考",
]

_PUNCT = set("，。！？；：、, . ! ? ; : 、 （ ） 《 》 “ ” ‘ ’ "" '' - — …")


def _sentences(text):
    return [s.strip() for s in re.split(r"[。！？\n;；]+", text) if s.strip()]


def _clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


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
    burst_score = _clamp(90 - (cv - 0.2) / 0.4 * 80)  # cv≤0.2→90(AI), cv≥0.6→10(human)

    # 2) AI-tell phrase density per 100 chars
    tell_count = sum(text.count(p) for p in AI_TELL_PHRASES)
    density = tell_count / max(chars, 1) * 100
    tell_score = _clamp((density - 0.5) / 2.5 * 100)  # ≤0.5/100→human, ≥3/100→AI

    # 3) punctuation variety
    ptypes = len(set(re.findall(r"[，。！？；：、,.!?;:、（） 《》“”‘’…—]", text)))
    punct_score = _clamp(90 - (ptypes - 2) / 4 * 80)  # ≤2种→AI, ≥6种→human

    score = round(burst_score * 0.35 + tell_score * 0.40 + punct_score * 0.25)
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
        ],
        "sentence_count": len(sents),
        "char_count": chars,
    }
