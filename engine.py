"""Multi-stage rewrite pipeline for 降重 (paraphrase / anti-plagiarism).

Per document:
  classify  -> label each paragraph (review/method/result/conclusion/general)
  rewrite   -> type-aware + strength-aware + discipline-aware rewrite per paragraph
  verify    -> local char-bigram similarity vs a target threshold
  retry     -> one harder pass if similarity too high (medium/deep only)
  repair    -> restore any dropped citations / numbers / formulas / proper nouns
"""
import json
import os
import re
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _load_key() -> str:
    try:
        cfg = json.load(open(Path.home() / ".claude.json"))
        return cfg["mcpServers"]["painting-coach"]["env"]["ZHIPU_API_KEY"]
    except Exception:
        return os.environ.get("ZHIPU_API_KEY", "")


KEY = _load_key()
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4-flash"
MAX_CHARS = 8000

SYSTEM = (
    "你是一名资深中文学术编辑，专门做「降重改写」：在严格保留原文含义、数据、引用、逻辑的前提下，"
    "最大程度地变换语言表达，使改写后的文字与原文的文字相似度尽量低，从而降低查重系统的重复率。\n\n"
    "铁律（不可违反）：\n"
    "1. 引用标记原样保留并置于语义对应处：[1]、[2,3]、(Smith et al., 2020)、（张三，2021）。\n"
    "2. 数字、单位、百分比、年份、统计量不变：95%、3.14、p < 0.01、2023 年、12.5 mm。\n"
    "3. 公式、化学式、代码、变量名、缩写、专有名词、学术术语、人名、地名、机构名不变。\n"
    "4. 不增删、不歪曲信息，不加观点或解释，不编造。\n"
    "5. 保持段落划分与论证顺序。\n"
    "6. 只输出改写后的正文，不要前言/后记/解释/Markdown 代码块。"
)

STRENGTH_INSTR = {
    "light": "请【轻度改写】：以同义词替换、语序调整、连接词变换为主，保留原句骨架与长度，不做句式重构。",
    "medium": "请【中度改写】：在保义前提下重构句式——主动被动互换、长句拆分、短句合并、定语状语前后移、换主语，辅以同义词替换，表达要有明显变化。",
    "deep": "请【深度改写】：完全重新组织语言表达相同含义，重排句子顺序、替换表述结构、打散重组信息单元，追求差异最大化，但严格保留所有事实、数据、引用、因果与逻辑。",
}

LABEL_INSTR = {
    "review": "这是文献综述段落：请变换引述动词（指出/提出/发现/认为/证明/强调）、调整对前人工作的叙述顺序、重组并列的文献列举，所有引用标记与作者名必须保留。",
    "method": "这是方法/实验段落：严格保留所有实验步骤、参数、条件、仪器名、术语；主要通过句式重构（主动↔被动、拆分或合并步骤句、调整状语位置）降重，不得增删实验细节。",
    "result": "这是结果段落：严格保留所有数值、单位、百分比、统计量、图表引用；主要通过重新组织对结果的描述顺序与表述方式降重。",
    "conclusion": "这是结论/讨论段落：在保留核心论点与逻辑关系前提下，重组论证结构、替换抽象表述，使表达更差异化但不改变结论含义。",
    "general": "无特殊类型，按强度要求改写即可。",
}

DISCIPLINE_INSTR = {
    "auto": "",
    "stem": "学科语境为理工科：专业术语、物理量、单位、公式、变量、缩写、算法/模型名称必须逐字保留，不得改写或意译；主要对非技术性的连接、过渡与描述性语句做句式和用词变换。",
    "humanities": "学科语境为人文社科：可在保义前提下较大幅度地变换词汇、句式与论证角度，丰富表达；引用、人名、专有名词、特定概念术语仍须保留。",
    "medicine": "学科语境为医学/生命科学：药品名、剂量、单位、诊断与统计指标、实验条件必须逐字保留；主要改写论述性与解释性语句，不得改变任何数据或临床结论的含义。",
    "law": "学科语境为法学：法律法规名称、条款编号、专门术语必须逐字保留；法条的条/款/项/章/编等单位严禁互换或更改；主要改写法理分析、论证与说理部分的句式与措辞，不得改变对法条的理解。",
}

TEMPERATURE = {"light": 0.3, "medium": 0.7, "deep": 0.95}
TARGET_SIM = {"light": 0.99, "medium": 0.46, "deep": 0.33}


def chat(messages, temperature=0.7) -> str:
    body = json.dumps({
        "model": MODEL, "messages": messages,
        "temperature": temperature, "top_p": 0.85,
    }).encode("utf-8")
    last_err = None
    for attempt in range(3):
        req = urllib.request.Request(
            ENDPOINT, data=body,
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(f"GLM HTTP {e.code}: {e.read()[:200]!r}")
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:  # 退避重试
                time.sleep(1.5 * (attempt + 1)); continue
            raise last_err
        except OSError as e:  # 含 socket.timeout（3.9 里不是 TimeoutError，必须用 OSError 接）
            last_err = RuntimeError(f"网络/超时: {e}")
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1)); continue
            raise last_err
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"GLM 响应结构异常: {str(data)[:200]}")
        if not content or not content.strip():
            raise RuntimeError("GLM 返回空内容（可能触发内容审核，请换段文字或降低强度后重试）")
        return content.strip()
    raise last_err if last_err else RuntimeError("GLM 调用失败")


def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def chunk_paragraphs(text: str, max_len: int = 1500) -> list:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks = []
    for p in paras:
        if len(p) <= max_len:
            blocks.append(p)
            continue
        sents = re.findall(r"[^。；！？\n]+[。；！？]?", p)
        cur = ""
        for s in sents:
            if cur and len(cur) + len(s) > max_len:
                blocks.append(cur)
                cur = s
            else:
                cur = (cur + s) if cur else s
        if cur:
            blocks.append(cur)
    return blocks or [text]


def preserve_tokens(text: str) -> set:
    toks = set()
    toks.update(re.findall(r"\[[\w,\s\-;&]+?\]", text))
    toks.update(re.findall(r"\([^()]*\d{4}[^()]*\)", text))
    toks.update(re.findall(r"（[^（）]*\d{4}[^（）]*）", text))
    toks.update(re.findall(r"\d+(?:\.\d+)?%", text))
    toks.update(re.findall(r"\b[A-Z][A-Za-z0-9\-]{1,}\b", text))
    toks.update(re.findall(r"\$[^$]+\$", text))
    toks.update(re.findall(r"《[^》]+》", text))
    toks.update(re.findall(r"第[\d一二三四五六七八九十百千\.]+条", text))
    toks.update(re.findall(r"\d+(?:\.\d+)?\s?(?:mm|cm|km|kg|mg|μg|mol|Hz|kHz|MHz|GHz|mmHg|IU|mol/L)\b", text, flags=re.I))
    toks.discard("")
    return toks


def missing_tokens(orig: str, rewrite: str) -> set:
    return {t for t in preserve_tokens(orig) if t not in rewrite}


CLASSIFY_SYS = (
    "你是学术论文段落类型分类器。对输入按空行分隔的每一段，从下列选一个最贴切的标签："
    "review（文献综述/相关工作）、method（方法/实验/材料/设计）、"
    "result（结果/数据/实验结果）、conclusion（结论/讨论/意义/展望）、general（其他/通用）。"
    "只输出 JSON 字符串数组，元素为标签，顺序与段落一致。不要任何解释。"
)


def classify_paragraphs(blocks: list) -> list:
    prompt = "\n\n".join(f"【段落{i + 1}】\n{b[:600]}" for i, b in enumerate(blocks))
    raw = chat([
        {"role": "system", "content": CLASSIFY_SYS},
        {"role": "user", "content": prompt},
    ], temperature=0.1)
    m = re.search(r"\[.*?\]", raw, flags=re.S)
    try:
        labels = json.loads(m.group(0)) if m else []
    except Exception:
        labels = []
    labels = [l if l in LABEL_INSTR else "general" for l in labels]
    while len(labels) < len(blocks):
        labels.append("general")
    return labels[:len(blocks)]


def rewrite_block(block: str, strength: str, label: str, discipline: str = "auto", harder: bool = False) -> str:
    extra = ("上一版改得不够，请这次改得更彻底，进一步降低与原文的文字相似度，但仍必须保义、保引用、保数据。"
             if harder else "")
    msg = "\n\n".join(x for x in [
        STRENGTH_INSTR[strength],
        LABEL_INSTR.get(label, ""),
        DISCIPLINE_INSTR.get(discipline, ""),
        extra,
        f"原文：\n{block}",
    ] if x)
    return chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": msg},
    ], temperature=TEMPERATURE[strength] + (0.05 if harder else 0))


def repair_block(block: str, rewrite: str, missing: set) -> str:
    msg = (
        "你在改写时丢失了以下必须保留的标记，请在已改写表达的基础上把它们补回语义对应的位置，"
        "不要重新改写整段：\n"
        f"丢失标记：{', '.join(sorted(missing))}\n\n当前改写：\n{rewrite}"
    )
    return chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": msg},
    ], temperature=0.2)


def process_block(block: str, strength: str, label: str, discipline: str = "auto") -> dict:
    rw = rewrite_block(block, strength, label, discipline)
    sim = similarity(block, rw)
    retries, repaired = 0, False

    if strength in ("medium", "deep") and sim > TARGET_SIM[strength]:
        rw = rewrite_block(block, strength, label, discipline, harder=True)
        sim = similarity(block, rw)
        retries = 1

    missing = missing_tokens(block, rw)
    for _ in range(2):           # loop repair until clean or no progress
        if not missing:
            break
        rw = repair_block(block, rw, missing)
        repaired = True
        prev = missing
        missing = missing_tokens(block, rw)
        if missing == prev:      # model didn't fix it; stop burning calls
            break
    sim = similarity(block, rw)

    return {
        "label": label,
        "sim": round(sim, 3),
        "retries": retries,
        "repaired": repaired,
        "still_missing": sorted(missing),
        "len": len(rw),
        "rewrite": rw,
    }


def rewrite_simple(text: str, strength: str, discipline: str = "auto") -> dict:
    blocks = chunk_paragraphs(text)
    parts = [rewrite_block(b, strength, "general", discipline) for b in blocks]
    out = "\n\n".join(p.strip() for p in parts)
    sim = similarity(text, out)
    return {
        "rewrite": out,
        "similarity": round(sim, 3),
        "coverage": round(1 - sim, 3),
        "chunks": len(blocks),
        "strength": strength,
        "discipline": discipline,
        "mode": "simple",
    }


def rewrite_pipeline(text: str, strength: str, discipline: str = "auto") -> dict:
    blocks = chunk_paragraphs(text)
    labels = classify_paragraphs(blocks)
    with ThreadPoolExecutor(max_workers=min(2, len(blocks))) as ex:
        futures = [ex.submit(process_block, b, strength, labels[i], discipline) for i, b in enumerate(blocks)]
        results = [f.result() for f in futures]

    out = "\n\n".join(r["rewrite"].strip() for r in results)
    overall = similarity(text, out)
    stages = ["classify", "rewrite", "verify"]
    if any(r["retries"] for r in results):
        stages.append("retry")
    if any(r["repaired"] for r in results):
        stages.append("repair")

    diag = [{k: v for k, v in r.items() if k != "rewrite"} for r in results]

    return {
        "rewrite": out,
        "similarity": round(overall, 3),
        "coverage": round(1 - overall, 3),
        "chunks": len(blocks),
        "strength": strength,
        "discipline": discipline,
        "mode": "pipeline",
        "stages": stages,
        "diagnostics": diag,
    }
