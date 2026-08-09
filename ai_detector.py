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


def _split_sents(text):
    return [s.strip() for s in re.split(r"[。！？\n;；]+", text) if s.strip()]


def _compute_blocks(text, cleaned):
    """切 ~200 字块并算每块困惑度（复用给聚合 + 逐句，避免重复推理）。
    返回 (sents, blocks, block_ppls, valid_ppls, sent_ppl):
      - block_ppls: 每块 ppl（无效为 None），与 blocks 对齐
      - valid_ppls: 仅有效 ppl 列表（聚合用）
      - sent_ppl: 每句 → 其所在块的 ppl（无效为 None），逐句着色用
    """
    sents = _split_sents(text)
    blocks, cur = [], ""
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

    block_ppls = []
    for b in blocks[:10]:
        p = _sentence_ppl(b)
        block_ppls.append(p if (p and 0 < p < 1000) else None)
    while len(block_ppls) < len(blocks):
        block_ppls.append(None)

    valid_ppls = [p for p in block_ppls if p]
    sent_ppl = {}
    for s in sents:
        for i, b in enumerate(blocks):
            if s in b:
                sent_ppl[s] = block_ppls[i] if i < len(block_ppls) else None
                break
    return sents, blocks, block_ppls, valid_ppls, sent_ppl


def _ppl_to_score(ppl):
    """困惑度 → AI 概率分段（与 detect_aigc 同口径）。"""
    if ppl < 12:
        return 95
    if ppl < 20:
        return 78
    if ppl < 28:
        return 58
    if ppl < 40:
        return 30
    if ppl < 55:
        return 15
    return 8


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

    sents, blocks, block_ppls, ppls, sent_ppl = _compute_blocks(text, cleaned)

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

    ppl_score = _ppl_to_score(avg_ppl)

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
        "_sent_ppls": sent_ppl,  # 内部字段：逐句 ppl，供 score_sentences_aigc 复用，不入对外响应
    }


def score_sentences_aigc(text, sent_ppl=None):
    """逐句 AIGC 疑似度（报告逐句标色用）。复用 detect_aigc 已算的 sent_ppl，
    无则自算（会触发 _compute_blocks，模型需已加载）。返回 [{sentence, score, color}]。
    score 0-100，color: err(≥65)/warn(40-65)/ok(<40)。"""
    import detectors
    text = text or ""
    sents = _split_sents(text)
    if sent_ppl is None:
        cleaned = re.sub(r"\s+", "", text)
        try:
            _, _, _, _, sent_ppl = _compute_blocks(text, cleaned)
        except Exception:
            sent_ppl = {}
    sent_ppl = sent_ppl or {}

    out = []
    for s in sents:
        chars = len(re.sub(r"\s+", "", s))
        tell = sum(1 for p in detectors.AI_TELL_PHRASES if p in s)
        tell_dens = tell / max(chars, 1) * 100
        ppl = sent_ppl.get(s)
        if ppl and chars >= 8:
            ppl_score = _ppl_to_score(ppl)
            heu = min(100, tell_dens * 25 + (15 if tell else 0))
            score = round(ppl_score * 0.70 + heu * 0.30)
        else:
            # 短句 ppl 不可靠：只靠套话启发式
            score = round(min(100, tell_dens * 30 + (25 if tell else 5)))
        score = max(3, min(97, score))
        color = "err" if score >= 65 else ("warn" if score >= 40 else "ok")
        out.append({"sentence": s, "score": score, "color": color})
    return out
