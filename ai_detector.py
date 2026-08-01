"""AI 文本检测器 — 困惑度 (Perplexity) + 突发性 (Burstiness) 双指标。

原理（与 GPTZero 相同）：
- 困惑度：AI 生成的文字"可预测性高"（困惑度低），人类写的"不可预测"（困惑度高）
- 突发性：AI 文本的困惑度均匀（低突发性），人类文本的困惑度波动大（高突发性）

使用中文 GPT-2 模型计算，模型 ~400MB，首次加载后缓存在内存中。
"""
import math
import re

_torch = None
_tok = None
_model = None


def _load():
    global _torch, _tok, _model
    if _model is None:
        import torch
        from transformers import BertTokenizer, GPT2LMHeadModel
        _torch = torch
        _tok = BertTokenizer.from_pretrained("uer/gpt2-chinese-cluecorpussmall")
        _model = GPT2LMHeadModel.from_pretrained("uer/gpt2-chinese-cluecorpussmall")
        _model.eval()


def _sentence_ppl(text):
    """计算单段文本的困惑度。"""
    _load()
    text = text.strip()
    if len(text) < 4:
        return None
    inputs = _tok(text, return_tensors="pt", max_length=512, truncation=True)
    with _torch.no_grad():
        out = _model(**inputs, labels=inputs["input_ids"])
    return math.exp(min(out.loss.item(), 20))  # cap to avoid overflow


def detect_aigc(text):
    """用困惑度 + 突发性检测 AI 生成内容。返回 0-100 的 AIGC 概率。"""
    text = text or ""
    try:
        _load()
    except Exception as e:
        return {"aigc_score": -1, "error": f"模型加载失败: {e}"}

    cleaned = re.sub(r"\s+", "", text)
    if len(cleaned) < 10:
        return {"aigc_score": 50, "verdict": "文本太短", "color": "warn",
                "note": "样本过短，无法可靠检测", "signals": [],
                "perplexity": 0, "burstiness": 0, "sentence_count": 0, "char_count": len(cleaned)}

    # 按较长块算困惑度（不拆短句，避免短句虚高）
    # 按 ~200 字一块切
    blocks = []
    sents = [s.strip() for s in re.split(r"[。！？\n;；]+", text) if s.strip()]
    cur = ""
    for s in sents:
        if cur and len(cur) + len(s) > 200:
            blocks.append(cur)
            cur = s
        else:
            cur = (cur + s) if cur else s
    if cur:
        blocks.append(cur)
    if not blocks:
        blocks = [cleaned[:500]]

    ppls = []
    for b in blocks[:10]:
        p = _sentence_ppl(b)
        if p and 0 < p < 1000:
            ppls.append(p)

    if not ppls:
        return {"aigc_score": 50, "verdict": "无法分析", "color": "warn",
                "note": "文本格式异常", "signals": [],
                "perplexity": 0, "burstiness": 0, "sentence_count": 0, "char_count": len(cleaned)}

    avg_ppl = sum(ppls) / len(ppls)
    if len(ppls) >= 2:
        mean = sum(ppls) / len(ppls)
        std = (sum((p - mean) ** 2 for p in ppls) / len(ppls)) ** 0.5
        burstiness = std / mean if mean > 0 else 0
    else:
        burstiness = 0.5

    # 困惑度 → AI 概率（本地实测：AI=13, 人类=31, 学术=52）
    if avg_ppl < 10:
        ppl_score = 95
    elif avg_ppl < 18:
        ppl_score = 82
    elif avg_ppl < 28:
        ppl_score = 55
    elif avg_ppl < 45:
        ppl_score = 28
    else:
        ppl_score = 10

    # 突发性
    if burstiness < 0.2:
        burst_score = 80
    elif burstiness < 0.4:
        burst_score = 55
    elif burstiness < 0.7:
        burst_score = 30
    else:
        burst_score = 15

    final = round(ppl_score * 0.70 + burst_score * 0.30)
    final = max(3, min(97, final))

    if final < 35:
        verdict, color = "偏低（偏人类写作）", "ok"
    elif final < 65:
        verdict, color = "中等（不确定）", "warn"
    else:
        verdict, color = "偏高（偏 AI 生成）", "err"

    signals = [
        {"name": "困惑度", "value": f"{avg_ppl:.1f}", "score": ppl_score,
         "hint": "高度可预测（偏 AI）" if ppl_score > 55 else "表达较自然（偏人）"},
        {"name": "突发性", "value": f"{burstiness:.2f}", "score": burst_score,
         "hint": "句式高度均匀（偏 AI）" if burst_score > 55 else "句式有起伏（偏人）"},
    ]

    return {
        "aigc_score": final,
        "verdict": verdict,
        "color": color,
        "note": "基于中文 GPT-2 困惑度 + 突发性双指标检测（与 GPTZero 原理相同）。模型级判断，非 LLM 猜测。结果仅供参考。",
        "signals": signals,
        "perplexity": round(avg_ppl, 1),
        "burstiness": round(burstiness, 2),
        "sentence_count": len(sents),
        "char_count": len(cleaned),
    }
