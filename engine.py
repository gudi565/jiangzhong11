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
MAX_CHARS = 20000

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

DETEMPLATE_INSTR = (
    "额外要求（反模板化，降低 AI 检测与语义查重，必须执行）：\n"
    "- 不要只做同义词替换，必须改变句子的表达方式与信息组织顺序。\n"
    "- 拆解工整的排比对仗、四字短语扎堆、「一方面…另一方面」「不仅…而且」这类对称结构，换成不对称的自然表达。\n"
    "- 打破「总-分-总」「首先/其次/最后」的标准模板，重组论证顺序。\n"
    "- 避免扎堆使用「推动/促进/旨在/有助于/为核心/具有重要意义/取得显著成效」这类 AI 高频套话动词与短语，换更具体或更不常见的说法。\n"
    "- 允许补充一两句自然的过渡或解释性表述（不改变核心论点），让行文更像人写、不像模板生成。"
)

SYSTEM_FLEX = (
    "你是一名资深中文学术编辑，做「深度降重改写」：在保留原文含义、数据、引用、逻辑的前提下，"
    "大幅变换语言表达方式，让文字既像人写的、又不像模板生成，从而同时降低字面查重率和语义/AI 查重。\n\n"
    "保留规则（必须）：引用标记、数字、单位、百分比、年份、统计量、公式、术语、专有名词原样保留；不歪曲原意、不编造事实。\n"
    "允许且鼓励（这是深度降重的核心，不算违规）：重组信息顺序、拆解模板与排比结构、补充自然的过渡或解释性表述、"
    "变换论证角度与表达方式、换用更具体或不常见的动词与连接词。只换词不换结构的改写对深度降重无效。\n"
    "只输出改写后的正文，不要前言/后记/解释/Markdown 代码块。"
)


TEMPERATURE = {"light": 0.3, "medium": 0.7, "deep": 0.95}
TARGET_SIM = {"light": 0.99, "medium": 0.46, "deep": 0.33}

# ── 查重报告标红句批量改写 ────────────────────────────────────────────────
REPORT_BATCH_SENTS = 8    # 每批句子数
REPORT_BATCH_CHARS = 2400  # 每批红句总字数上限（先到先停）

REPORT_INSTR = (
    "对下列编号句子逐一降重改写。规则：\n"
    "1. 只改写给出的编号句子，不得增删句子、不得合并或拆分编号。\n"
    "2. 严格保留原意、数据、引用标记（[1]、（张三，2021））、术语、专有名词。\n"
    "3. 每句附的「上下文」仅帮助理解代词与逻辑，绝对不要改写或输出上下文。\n"
    "4. 按所选强度改写句式与用词，降低与原句的字面相似度。\n"
    '只输出一个 JSON 对象，形如 {"编号":"改写后句子", ...}，包含全部编号，禁止 markdown、禁止解释。'
)


def chat(messages, temperature=0.7) -> str:
    body = json.dumps({
        "model": MODEL, "messages": messages,
        "temperature": temperature, "top_p": 0.85,
    }).encode("utf-8")
    last_err = None
    start = time.time()
    for attempt in range(3):  # 单次 GLM 调用总预算 ~30s：大文本生成需要更久，放宽超时
        if time.time() - start > 30:
            break
        timeout = max(8, min(15, 30 - (time.time() - start)))
        req = urllib.request.Request(
            ENDPOINT, data=body,
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(f"GLM HTTP {e.code}: {e.read()[:200]!r}")
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:  # 退避重试
                time.sleep(0.8); continue
            raise last_err
        except OSError as e:  # 含 socket.timeout（3.9 里不是 TimeoutError，必须用 OSError 接）
            last_err = RuntimeError(f"网络/超时: {e}")
            if attempt < 2:
                time.sleep(0.8); continue
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


def chunk_paragraphs(text: str, max_len: int = 4000) -> list:
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


def _has_hard_facts(text: str) -> bool:
    """含数字/百分比/年份/引用/单位 → 是数据/方法段，不能激进反模板化。"""
    return bool(re.search(r"\d+(\.\d+)?\s*[%年mm克毫克]|\[\d|（[^）]*\d{4}|p\s*[<>]\s*0\.|表\s*\d|图\s*\d", text))


def rewrite_block(block: str, strength: str, label: str, discipline: str = "auto", harder: bool = False) -> str:
    extra = ("上一版改得不够，请这次改得更彻底，进一步降低与原文的文字相似度，但仍必须保义、保引用、保数据。"
             if harder else "")
    # deep 模式 + 非数据段（无硬事实）→ 叠加反模板化，直击 GLM 查重的「模板化判断」
    detemplate = ""
    if strength == "deep" and not _has_hard_facts(block):
        detemplate = DETEMPLATE_INSTR
    msg = "\n\n".join(x for x in [
        STRENGTH_INSTR[strength],
        LABEL_INSTR.get(label, ""),
        DISCIPLINE_INSTR.get(discipline, ""),
        detemplate,
        extra,
        f"原文：\n{block}",
    ] if x)
    sys_prompt = SYSTEM_FLEX if detemplate else SYSTEM
    return chat([
        {"role": "system", "content": sys_prompt},
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

    if strength == "deep" and sim > TARGET_SIM[strength]:  # 仅深度模式重试，轻度/中度直接出结果（提速）
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


HUMANIZE_INSTR = (
    "请把这段文字「去 AI 化」改写——目标是让它读起来像人写的，降低 AI 检测工具的识别率。手法："
    "① 大幅拉开句子长度的起伏（短句和长句交错，不要每句都差不多长）；"
    "② 拆解工整的并列/模板结构，少用「首先/其次/最后」「一方面/另一方面」「综上所述」「由此可见」「值得注意的是」这类 AI 高频套话，换成更自然的说法或直接重组语序；"
    "③ 偶尔用一点带个人语气的表达（在学术允许范围内），去掉过度工整和过分精准的措辞；"
    "④ 句式多样化（可适度出现疑问、倒装、省略、插入语）；"
    "⑤ 不要把每段都写成差不多的「总—分—总」结构。\n\n"
    "铁律不变：保留原意与逻辑；引用标记（[1]、(Smith, 2020)、（张三，2021）等）、数字、单位、百分比、年份、统计量、公式、术语、专有名词原样保留在语义对应处；不增删信息。只改「AI 味」，不改内容。"
)

HUMANIZE_SYS = (
    "你是一名资深中文学术编辑，专门做「去 AI 化改写」：让 AI 生成的学术文字读起来像人写的，"
    "大幅降低 AI 检测工具的识别率，同时保留原意与学术性。\n\n"
    "保留规则（必须）：引用标记、数字、单位、百分比、年份、统计量、公式、术语、专有名词原样保留于语义对应处；不增删事实信息、不编造数据。\n"
    "允许且鼓励（这是去 AI 化的核心，不算违规）：变换表达方式、加入自然的过渡与解释、改变论证顺序、"
    "拆解模板与排比结构、加入作者口吻与判断（如「笔者认为」「令人意外的是」「这一点常被忽视」）。"
    "这些是降低 AI 痕迹的必要手段，不要因为「保义」而拒绝改变表达——只换词不改结构的改写对去 AI 化无效。\n"
    "只输出改写后的正文，不要前言/后记/解释/Markdown 代码块。"
)


def rewrite_humanize(text: str, strength: str = "medium") -> dict:
    blocks = chunk_paragraphs(text)
    parts = []
    for b in blocks:
        msg = "\n\n".join(x for x in [HUMANIZE_INSTR, STRENGTH_INSTR[strength], f"原文：\n{b}"] if x)
        parts.append(chat([
            {"role": "system", "content": HUMANIZE_SYS},
            {"role": "user", "content": msg},
        ], temperature=0.95))
    out = "\n\n".join(p.strip() for p in parts)
    sim = similarity(text, out)
    return {
        "rewrite": out,
        "similarity": round(sim, 3),
        "coverage": round(1 - sim, 3),
        "chunks": len(blocks),
        "strength": strength,
        "mode": "humanize",
    }


ENGLISH_EDIT_INSTR = (
    "You are a professional academic English editor helping a non-native researcher. "
    "Revise the text to be publication-ready: fix grammar, spelling, and punctuation; "
    "improve clarity, flow, and academic tone; make awkward or non-native phrasing natural and professional; "
    "tighten wordy sentences. "
    "Rules: preserve meaning exactly; keep all technical terms, proper nouns, numbers, units, statistics, "
    "citations (e.g., [1], (Smith, 2020)), equations, and abbreviations unchanged; do not add claims or delete information; "
    "keep paragraph structure. Output only the revised English text, no commentary."
)

ENGLISH_STRENGTH = {
    "light": "Light proofreading: fix only grammar, spelling, and punctuation. Keep original wording otherwise.",
    "medium": "Moderate polish: fix errors and improve clarity, flow, and academic tone. Keep meaning and most wording.",
    "deep": "Deep revision: thoroughly rewrite for stronger academic impact — vary sentence structure, sharpen phrasing — while strictly preserving meaning, data, citations, and technical terms.",
}


ENGLISH_DEDUP_INSTR = (
    "You are an academic paraphrasing tool. Rewrite the English text to substantially reduce its textual similarity to the original "
    "(to lower plagiarism-detection scores) while preserving the meaning, all data, citations, and technical terms. "
    "Thoroughly change sentence structure, vocabulary, and phrasing — do not just swap synonyms. Keep it natural, fluent, academic English. "
    "Rules: same meaning; citations ([1], (Smith, 2020)), numbers, units, equations, and proper nouns unchanged; output only the rewritten English."
)

ENGLISH_TRANSLATE_INSTR = (
    "You are a professional academic translator. Translate the Chinese text into natural, publication-ready academic English. "
    "Rules: translate accurately without adding or omitting meaning; keep all numbers, units, citations ([1]), and proper nouns "
    "(transliterate Chinese names, keep any English terms already present); match academic tone and terminology; output only the English translation."
)


def rewrite_english(text: str, strength: str = "medium", sub: str = "polish") -> dict:
    blocks = chunk_paragraphs(text)
    if sub == "dedup":
        instr, extra, sysmsg, temp = ENGLISH_DEDUP_INSTR, ENGLISH_STRENGTH[strength], "You are an academic paraphrasing tool.", 0.7
    elif sub == "translate":
        instr, extra, sysmsg, temp = ENGLISH_TRANSLATE_INSTR, "", "You are a professional academic translator (Chinese to English).", 0.3
    else:  # polish
        instr, extra, sysmsg, temp = ENGLISH_EDIT_INSTR, ENGLISH_STRENGTH[strength], "You are a professional academic English editor.", 0.6
    label = "Translate to English:" if sub == "translate" else "Original:"
    parts = []
    for b in blocks:
        msg = "\n\n".join(x for x in [instr, extra, f"{label}\n{b}"] if x)
        parts.append(chat([
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": msg},
        ], temperature=temp))
    out = "\n\n".join(p.strip() for p in parts)
    sim = similarity(text, out)
    return {
        "rewrite": out,
        "similarity": round(sim, 3),
        "coverage": round(1 - sim, 3),
        "chunks": len(blocks),
        "strength": strength,
        "mode": "english",
        "sub": sub,
    }


def rewrite_pipeline(text: str, strength: str, discipline: str = "auto") -> dict:
    blocks = chunk_paragraphs(text)
    # 跳过单独的分类调用以提速；段落类型感知由 system prompt + discipline overlay 承担
    labels = ["general"] * len(blocks)
    with ThreadPoolExecutor(max_workers=min(2, len(blocks))) as ex:
        futures = [ex.submit(process_block, b, strength, labels[i], discipline) for i, b in enumerate(blocks)]
        results = [f.result() for f in futures]

    out = "\n\n".join(r["rewrite"].strip() for r in results)
    overall = similarity(text, out)
    stages = ["rewrite", "verify"]
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


def _parse_report_json(raw: str) -> dict:
    """GLM 输出 → {编号: 改写句}。strip 围栏 + 提取最外层 {} + 容错解析。"""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    m = re.search(r"\{.*\}", s, flags=re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            try:
                out[int(str(k).strip())] = str(v).strip()
            except (ValueError, TypeError):
                continue
    return out


def _rewrite_report_batch(batch: list, context_map: dict, strength: str) -> dict:
    """改一批红句（[{i,text}]），返回 {i: new}。缺号重试 1 次，仍缺抛给上层兜底。"""
    lines = []
    for s in batch:
        ctx = (context_map.get(s["i"]) or "").replace("\n", " ")
        lines.append(f"【{s['i']}】句子：{s['text']}\n（上下文：{ctx[:160]}）")
    prompt = "\n\n".join([
        REPORT_INSTR,
        STRENGTH_INSTR[strength],
        "待改写句子：\n" + "\n".join(lines),
    ])
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]
    result = _parse_report_json(chat(messages, temperature=TEMPERATURE.get(strength, 0.7)))
    missing = [s["i"] for s in batch if s["i"] not in result]
    if missing:
        retry = "\n\n".join(
            f"【{s['i']}】句子：{s['text']}\n（上下文：{(context_map.get(s['i']) or '')[:160]}）"
            for s in batch if s["i"] in missing
        )
        result2 = _parse_report_json(chat([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": REPORT_INSTR + "\n\n你上次输出缺失了编号 " +
             ", ".join(str(m) for m in missing) + "，请补全这些编号：\n" + retry},
        ], temperature=TEMPERATURE.get(strength, 0.7)))
        result.update(result2)
    return result


def rewrite_report_sentences(red_sents: list, context_map: dict, strength: str = "medium") -> dict:
    """查重报告标红句批量改写。red_sents=[{i,text}]，context_map={i: 上下文}。
    返回 {"rewrites": {i: new}, "failed": [i], "batches": n}。
    失败句（缺号/太短/与原文相同）保留原句并记入 failed，绝不整单失败。"""
    batches, cur, cur_chars = [], [], 0
    for s in red_sents:
        if cur and (len(cur) >= REPORT_BATCH_SENTS or cur_chars >= REPORT_BATCH_CHARS):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(s)
        cur_chars += len(s.get("text", ""))
    if cur:
        batches.append(cur)

    results = [{}] * len(batches)
    with ThreadPoolExecutor(max_workers=min(2, len(batches))) as ex:
        futures = [ex.submit(_rewrite_report_batch, b, context_map, strength) for b in batches]
        results = [f.result() for f in futures]

    merged = {}
    for r in results:
        merged.update(r or {})

    rewrites, failed = {}, []
    for s in red_sents:
        i, orig = s["i"], s.get("text", "")
        new = (merged.get(i) or "").strip()
        if new and len(new) >= 4 and new != orig:
            rewrites[i] = new
        else:
            failed.append(i)  # 保留原句
    return {"rewrites": rewrites, "failed": failed, "batches": len(batches)}
