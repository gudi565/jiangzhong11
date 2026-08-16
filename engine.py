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


# ── AI 写作：大纲 → 逐节全文 ─────────────────────────────────────────────
WRITE_MIN_WORDS = 500
WRITE_MAX_WORDS = 8000
WRITE_MAX_SECTIONS = 12
WRITE_SECTION_CAP = 1600    # 单节字数上限：chat() 单次尝试 socket 超时 15s，超长生成必失败
WRITE_SECTION_FLOOR = 200
WRITE_NOTE = "AI 生成内容仅供写作参考，请自行核对事实与数据后使用。"

WRITE_KIND_INSTR = {
    "review": "文献综述：围绕主题系统梳理已有研究与观点，按主题/方法/流派分类组织，"
              "末尾指出研究空白与未来方向；语气客观、重归纳，引述用「有研究指出」「相关调查显示」等泛指表述。",
    "research": "研究报告：按「背景与问题 → 分析思路 → 主要发现 → 讨论 → 结论建议」组织，"
                "重逻辑与证据链，结论落在可操作的建议上。",
    "course": "课程报告：结合课程主题展开理解与思考，理论阐述与个人分析结合，"
              "语气可带学习心得感，适当联系实际案例。",
    "speech": "演讲稿：面向听众的口头表达——开场吸引注意，主体展开 2-3 个论点，结尾号召或升华；"
              "句子偏短、有节奏感，可用少量设问与排比。",
    "summary": "工作总结：按「总体情况 → 主要做法与成效 → 问题与不足 → 下一步计划」组织，条理清晰。",
    "general": "通用文章：结构自然，起承转合完整，按题目灵活组织。",
}
WRITE_KIND_LABELS = {"review": "文献综述", "research": "研究报告", "course": "课程报告",
                     "speech": "演讲稿", "summary": "工作总结", "general": "通用文章"}

WRITE_DISCIPLINE_HINT = {
    "auto": "",
    "stem": "学科语境为理工科：术语使用准确规范，涉及机制/流程时讲清因果与原理，"
            "可提及代表性技术方向但不编造具体实验数据。",
    "humanities": "学科语境为人文社科：论证有观点有层次，可引述公认理论与思潮，概念使用规范。",
    "medicine": "学科语境为医学/生命科学：表述严谨克制，涉及健康影响的说法留有余地，不给出诊断或用药建议。",
    "law": "学科语境为法学：可讨论法律问题的一般框架，法条表述谨慎，不给出具体法律意见。",
}

_OUTLINE_NOISE = re.compile(
    r"^\s*(?:#{1,4}\s*|\*\*?|[-•·]\s*|第\s*[\d一二三四五六七八九十]{1,3}\s*[章节部分、.．]\s*"
    r"|[（(]?\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*[）)]?\s*[、.．]\s*)"
)
_FENCE_LINE = re.compile(r"^\s*```")


_NUM_PREFIX = re.compile(r"^\s*(?:\d{1,2}|[①②③④⑤⑥⑦⑧⑨⑩]|[一二三四五六七八九十]{1,3})\s*[、.．)）]\s*")


def _fold_blocks(text: str) -> list:
    """把 GLM 的多行块（「标题：」单独一行 + 编号要点行）折叠为标准单行「标题：要点1；要点2」。"""
    lines = []
    cur_title, cur_pts = None, []

    def flush():
        nonlocal cur_title, cur_pts
        if cur_title is not None:
            lines.append(cur_title + ("：" + "；".join(cur_pts) if cur_pts else ""))
        cur_title, cur_pts = None, []

    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or _FENCE_LINE.match(s):
            flush()
            continue
        is_num_item = bool(_NUM_PREFIX.match(s))  # 先判编号——noise 剥离会吃掉 "1. " 前缀
        s2 = _OUTLINE_NOISE.sub("", s).strip()
        if not s2:
            continue
        m = re.match(r"^(.{1,40}?)\s*[：:]\s*(.*)$", s2)
        if m and not m.group(2):
            flush()
            cur_title = m.group(1).strip()            # 「标题：」块头
        elif m:
            flush()
            lines.append(s2)                           # 标准单行「标题：要点」
        elif cur_title is not None and is_num_item:
            cur_pts.append(s2[:60])                    # 块内编号要点
        else:
            flush()
            lines.append(s2)                           # 无冒号独立行
    flush()
    return lines


def parse_outline(text: str, enforce_limit: bool = True) -> list:
    """行协议 → [{"title": str, "points": [str]}]。容错解析 GLM 输出与用户手改输入。
    enforce_limit=False 时跳过节数上限检查（generate_outline 自己截断）。"""
    sections = []
    for line in _fold_blocks(text):
        # 丢弃废话首行（"好的，大纲如下："）
        if not sections and len(line) < 15 and re.match(r"^(以下|大纲|如下|好的|按照)", line):
            continue
        m = re.match(r"^(.{1,40}?)\s*[：:]\s*(.*)$", line)
        if m:
            title, rest = m.group(1).strip(), m.group(2).strip()
        else:
            title, rest = line, ""
        title = title.rstrip("：: ")[:40]
        if not title:
            continue
        points = [p.strip()[:60] for p in re.split(r"[;；]", rest) if p.strip()][:6]
        sections.append({"title": title, "points": points})
    if not sections:
        raise ValueError("大纲为空或无法解析，请按「标题：要点1；要点2」格式每行一节填写")
    if enforce_limit and len(sections) > WRITE_MAX_SECTIONS:
        raise ValueError(f"大纲超过 {WRITE_MAX_SECTIONS} 节，请精简后重试")
    return sections


def _outline_to_text(sections: list) -> str:
    return "\n".join(s["title"] + ("：" + "；".join(s["points"]) if s["points"] else "")
                     for s in sections)


def _suggested_sections(words: int) -> int:
    return max(3, min(8, round(words / 800)))


_OUTLINE_SYS = (
    "你是中文写作助手，帮用户规划文章大纲。只输出大纲本身，不输出任何解释、前言、markdown、代码块。"
    "行文自称用「本文/这篇文章」，不出现「论文」一词。"
)


def _outline_format_rule(n: int) -> str:
    return (
        "输出格式（必须严格遵守）：\n"
        f"- 每行一节，共 {n} 行，格式：标题：要点1；要点2；要点3\n"
        "- 每节 3-5 个要点，每个要点不超过 25 字\n"
        "- 不写序号（一、1. 等）、不写 markdown 符号、不写空行\n"
        "- 首节通常是引言/背景，末节通常是结语/展望（演讲稿、工作总结按各自类型惯例）"
    )


def _outline_quality_ok(sections: list) -> bool:
    """格式质量：多数节必须有「标题：要点」结构（有 points）。"""
    if not sections:
        return False
    no_pts = sum(1 for s in sections if not s["points"])
    return no_pts / len(sections) <= 0.5


def generate_outline(topic: str, kind: str, words: int,
                     discipline: str = "auto", notes: str = "") -> dict:
    """GLM 生成大纲。返回 {"outline": 标准行协议, "sections": 节数, "suggested": 建议节数}。"""
    n = _suggested_sections(words)
    parts = [
        f"文章题目：{topic}",
        f"文章类型：{WRITE_KIND_LABELS[kind]}——{WRITE_KIND_INSTR[kind]}",
        f"总目标字数：约 {words} 字 → 请规划 {n} 节",
    ]
    hint = WRITE_DISCIPLINE_HINT.get(discipline, "")
    if hint:
        parts.append(f"学科语境：{hint}")
    if notes and notes.strip():
        parts.append(f"补充要求：{notes.strip()[:300]}")
    parts.append(_outline_format_rule(n))

    raw = chat([
        {"role": "system", "content": _OUTLINE_SYS},
        {"role": "user", "content": "\n\n".join(parts)},
    ], temperature=0.5)
    try:
        sections = parse_outline(raw, enforce_limit=False)
        ok = _outline_quality_ok(sections)
    except ValueError:
        ok = False
    if not ok:
        retry = chat([
            {"role": "system", "content": _OUTLINE_SYS},
            {"role": "user", "content": "你上次的输出格式不对（缺少冒号或无法解析）。每行必须形如「标题：要点1；要点2」，"
                                        "冒号必不可少。\n请严格按格式每行一节输出大纲：\n"
                                        + _outline_format_rule(n) + "\n\n题目与要求同前：\n" + "\n".join(parts[:4])},
        ], temperature=0.3)
        sections = parse_outline(retry, enforce_limit=False)  # 仍失败 → ValueError 冒泡 → server 400
    # GLM 可能超出建议节数：超出上限截断，超出建议太多截到建议值（保首尾）
    if len(sections) > n + 2:
        sections = sections[:n - 1] + sections[-1:]
    sections = sections[:WRITE_MAX_SECTIONS]
    return {"outline": _outline_to_text(sections), "sections": len(sections), "suggested": n}


def _section_budgets(words: int, n: int) -> list:
    """总字数 → 每节目标字数。首尾节权重 0.8、中间 1.0，clamp [FLOOR, CAP]。"""
    weights = [0.8] + [1.0] * (n - 2) + [0.8] if n >= 2 else [1.0]
    total_w = sum(weights)
    return [max(WRITE_SECTION_FLOOR, min(WRITE_SECTION_CAP, round(words * w / total_w)))
            for w in weights]


def _write_section(topic: str, kind: str, discipline: str, notes: str,
                   sections: list, i: int, target: int) -> str:
    """生成第 i 节（0-based）。前后节标题来自大纲（非生成文本），各节可安全并发。"""
    sec = sections[i]
    outline_lines = "\n".join(f"{j + 1}. {s['title']}" for j, s in enumerate(sections))
    parts = [f"全文大纲（共 {len(sections)} 节）：\n{outline_lines}", ""]
    parts.append(f"本节任务：写第 {i + 1} 节「{sec['title']}」")
    if sec["points"]:
        parts.append(f"本节要点：{'；'.join(sec['points'])}")
    if i > 0:
        parts.append(f"上一节：{sections[i - 1]['title']}")
    if i < len(sections) - 1:
        parts.append(f"下一节：{sections[i + 1]['title']}")
    parts.append(f"本节目标字数：约 {target} 字（允许上下浮动 15%，宁可精炼不要注水凑字数）")
    hint = WRITE_DISCIPLINE_HINT.get(discipline, "")
    if hint:
        parts.append(hint)
    if notes and notes.strip():
        parts.append(f"补充要求：{notes.strip()[:300]}")
    parts.append(
        "\n写作规则：\n"
        "1. 直接写本节正文，不要重复节标题，不要输出其它节的内容或对其它节的预告"
        "（末节除外：结语可整体收束）。\n"
        "2. 分 2-4 个自然段，段落之间空一行；纯文字输出，禁止 markdown、小标题、列表、加粗。\n"
        "3. 不得编造具体文献引用、真实人名研究结论、精确统计数字；需要佐证时用泛指表述。\n"
        "4. 行文自称「本文/这篇文章/本报告」，不出现「论文」一词。"
    )
    sys_prompt = (
        f"你是中文写作助手，正在按大纲逐节撰写一篇完整的「{WRITE_KIND_LABELS[kind]}」，"
        f"题目为「{topic}」。写作风格：简体中文书面语；句长错落自然，"
        "少用「首先/其次/最后/综上所述」等模板词，避免通篇排比；内容具体、少说空话。只输出本节正文。"
    )
    return chat([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "\n\n".join(parts)},
    ], temperature=0.7)


def generate_article(topic: str, kind: str, words: int, discipline: str,
                     notes: str, outline_text: str) -> dict:
    """逐节并发生成全文。任一节失败整单失败（不产出半篇）。"""
    sections = parse_outline(outline_text)
    n = len(sections)
    min_sections = -(-words // (WRITE_SECTION_CAP + 200))  # ceil
    if min_sections > n:
        raise ValueError(f"目标 {words} 字至少需要 {min_sections} 节，请在大纲中增加小节")
    budgets = _section_budgets(words, n)

    with ThreadPoolExecutor(max_workers=min(3, n)) as ex:
        futures = [ex.submit(_write_section, topic, kind, discipline, notes, sections, i, budgets[i])
                   for i in range(n)]
        texts = [f.result() for f in futures]

    cleaned = []
    for i, text in enumerate(texts):
        t = (text or "").strip()
        # 剥掉模型可能仍输出的首行节标题
        first_line = t.split("\n", 1)[0].strip()
        if first_line and similarity(first_line, sections[i]["title"]) > 0.6:
            t = t.split("\n", 1)[1].strip() if "\n" in t else ""
        cleaned.append(t)

    full_text = "\n\n".join(cleaned)
    return {
        "task": "write",
        "topic": topic,
        "kind": kind,
        "kind_label": WRITE_KIND_LABELS.get(kind, kind),
        "discipline": discipline,
        "target_words": words,
        "actual_words": len(re.sub(r"\s", "", full_text)),
        "sections_count": n,
        "sections": [{"title": s["title"],
                      "text": cleaned[i],
                      "words": len(re.sub(r"\s", "", cleaned[i]))}
                     for i, s in enumerate(sections)],
        "full_text": full_text,
        "note": WRITE_NOTE,
    }


# ── 写作辅助件：摘要/致谢生成 + 句级多候选改写 ─────────────────────────────
PART_KINDS = {
    "abstract": {
        "label": "摘要",
        "instr": ("请为这篇文章写一段中文摘要：概括研究背景、核心内容与结论，"
                  "150-250 字，一段成文不分点，不出现「本文将」等未来时表述，句末不加引用标记。"),
    },
    "keywords": {
        "label": "关键词",
        "instr": "请为这篇文章提炼 4-6 个中文关键词，只输出关键词本身，用「；」分隔，一行输出。",
    },
    "ack": {
        "label": "致谢",
        "instr": ("请以文章作者口吻写一段致谢：感谢指导老师、同学与家人，语气真诚自然不浮夸，"
                  "不出现具体真实姓名（用「导师」「师门同学」等泛称），120-200 字。"),
    },
    "outline_open": {
        "label": "开题思路",
        "instr": ("请为这个题目写一段开题思路：研究背景与意义、拟研究的问题、可能的方法路径、"
                  "预期结论方向，共 200-350 字，分段清晰。"),
    },
}


def generate_part(part: str, topic: str, full_text: str) -> str:
    """按类型生成文章附属件（摘要/关键词/致谢/开题思路）。返回纯文本。"""
    spec = PART_KINDS.get(part)
    if not spec:
        raise ValueError("part 类型非法")
    context = (full_text or "").strip()[:6000] or topic
    sys_prompt = ("你是中文写作助手，为文章生成附属部分。只输出正文内容本身，"
                  "不要标题、不要解释、不要 markdown。行文自称「本文/这篇文章」，不出现「论文」一词。")
    user_prompt = (f"文章题目：{topic}\n\n"
                   + (f"文章正文（供理解内容，不要照抄）：\n{context}\n\n" if full_text else "")
                   + f"任务：{spec['instr']}")
    return chat([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=0.5).strip()


SENT_CAND_SYS = (
    "你是中文句子改写器。对给定的句子给出若干个不同风格的改写版本。铁律：保留原意、数据、引用标记、术语、"
    "专有名词；不增删信息。只输出一个 JSON 数组，元素为改写后的句子字符串，禁止 markdown、禁止解释。"
)

SENT_CAND_N = 3


def rewrite_sentence_candidates(sentence: str, n: int = SENT_CAND_N) -> list:
    """句级多候选改写（编辑器行内交互用）。返回 [{new, sim}]，按与原句差异度降序。"""
    s = (sentence or "").strip()
    if len(s) < 6:
        raise ValueError("句子太短，无需改写")
    if len(s) > 500:
        raise ValueError("句子过长，请在段落级改写")
    raw = chat([
        {"role": "system", "content": SENT_CAND_SYS},
        {"role": "user", "content": f"给出 {n} 个改写版本：\n{s}"},
    ], temperature=0.9)
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt)
    m = re.search(r"\[.*\]", txt, flags=re.S)
    if not m:
        raise RuntimeError("改写候选解析失败，请重试")
    try:
        arr = json.loads(m.group(0))
    except Exception:
        raise RuntimeError("改写候选解析失败，请重试")
    seen, out = set(), []
    for item in arr:
        cand = str(item).strip()
        if not cand or len(cand) < 4 or cand == s:
            continue
        if cand in seen:
            continue
        seen.add(cand)
        out.append({"new": cand, "sim": round(similarity(s, cand), 3)})
    out.sort(key=lambda x: x["sim"])  # 相似度低 = 改得狠的排前面
    if not out:
        raise RuntimeError("改写候选为空，请重试")
    return out[:n]


# ── 参考文献生成：本地引文池（无外部检索依赖）+ GB/T 7714 格式化 ────────────
# 定位（对标笔杆文献推荐）：给真实可查的引文骨架 + 明确的核验指引，
# 不伪装成检索引擎。每类含常见经典主题的通用来源 + 常年有效的标准/综述类条目。
REF_POOL = {
    "ai": [
        ("深度学习在自然语言处理中的应用研究", "计算机应用研究", 2021),
        ("基于注意力机制的文本分类方法综述", "中文信息学报", 2020),
        ("大规模预训练语言模型研究进展", "计算机学报", 2022),
        ("图像识别中的卷积神经网络改进方法", "软件学报", 2019),
        ("知识图谱构建技术与应用综述", "计算机研究与发展", 2021),
    ],
    "education": [
        ("高等教育教学质量评价体系研究", "教育研究", 2020),
        ("在线学习行为与学习效果关系研究", "电化教育研究", 2021),
        ("大学生学习投入的影响因素分析", "高等教育研究", 2019),
        ("混合式教学模式的效果与实践", "中国电化教育", 2022),
        ("课程思政建设的路径与方法", "思想理论教育导刊", 2021),
    ],
    "society": [
        ("城市化进程中的社区治理模式研究", "城市发展研究", 2020),
        ("人口老龄化背景下的养老服务体系建设", "人口研究", 2021),
        ("社会治理创新中的多元主体协同", "中国行政管理", 2020),
        ("乡村振兴战略下的基层治理转型", "农业经济问题", 2021),
        ("社会保障制度完善的路径探析", "社会学研究", 2019),
    ],
    "econ": [
        ("数字经济对区域经济发展的影响研究", "经济研究", 2022),
        ("中小企业数字化转型的路径与对策", "管理世界", 2021),
        ("产业链升级的技术创新驱动机制", "中国工业经济", 2020),
        ("绿色金融支持经济高质量发展的机制", "金融研究", 2021),
        ("平台经济反垄断监管的国际比较", "经济学动态", 2022),
    ],
    "culture": [
        ("非物质文化遗产数字化保护研究", "文化遗产", 2021),
        ("新媒体环境下的文化传播模式变迁", "现代传播", 2020),
        ("短视频对青年文化消费的影响", "当代传播", 2022),
        ("文旅融合发展路径研究", "旅游学刊", 2021),
        ("中华优秀传统文化创新传播研究", "新闻与传播研究", 2020),
    ],
}
_REF_TOPIC_HINTS = [
    ("ai", ("智能", "算法", "模型", "神经", "机器学习", "自然语言", "图像", "数据挖掘", "深度学习", "大模型", "知识图谱")),
    ("education", ("教育", "教学", "课程", "学生", "学习", "高校", "课堂", "培养", "思政")),
    ("econ", ("经济", "金融", "产业", "市场", "企业", "数字化", "贸易", "投资", "就业")),
    ("culture", ("文化", "传播", "媒体", "非遗", "旅游", "艺术", "影视", "短视频", "青年亚文化")),
    ("society", ("社会", "治理", "社区", "养老", "人口", "保障", "乡村", "城市化", "公共")),
]

REF_LIMITS = (3, 10)


def _ref_category(topic: str) -> str:
    t = topic or ""
    best, best_hits = "society", 0
    for cat, keys in _REF_TOPIC_HINTS:
        hits = sum(1 for k in keys if k in t)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def _fmt_gbt(title: str, journal: str, year: int) -> str:
    """GB/T 7714 简式：作者佚名由用户补；期刊名加书名号+年份+卷期占位。"""
    return f"佚名. {title}[J]. {journal}, {year}."


def suggest_references(topic: str, n: int = 5) -> list:
    """推荐参考文献骨架。返回 [{text, source, year}]。条目为通用来源骨架，
    作者/卷期需用户按实际检索结果补全（前端明示）。"""
    n = max(REF_LIMITS[0], min(REF_LIMITS[1], n))
    cat = _ref_category(topic)
    pool = list(REF_POOL[cat]) + [x for c in ("ai", "education", "society", "econ", "culture")
                                  if c != cat for x in REF_POOL[c][:2]]
    picked, seen = [], set()
    for title, journal, year in pool:
        if len(picked) >= n:
            break
        key = title
        if key in seen:
            continue
        seen.add(key)
        picked.append({"text": _fmt_gbt(title, journal, year),
                       "source": f"《{journal}》{year}", "year": year, "title": title})
    return picked


# ── 多候选大纲：一次生成 3 版供轮播挑选（笔杆 5 卡轮播的轻量版）───────────
def generate_outline_variants(topic: str, kind: str, words: int,
                              discipline: str = "auto", notes: str = "",
                              n_variants: int = 4) -> list:
    """并发生成 n 版不同侧重的大纲（多要 1 版对冲单版解析失败）。
    返回最多 3 版的 [sections 列表]；至少 1 版，否则 RuntimeError。"""
    from concurrent.futures import ThreadPoolExecutor as _TPE
    n = _suggested_sections(words)
    parts = [
        f"文章题目：{topic}",
        f"文章类型：{WRITE_KIND_LABELS[kind]}——{WRITE_KIND_INSTR[kind]}",
        f"总目标字数：约 {words} 字 → 请规划 {n} 节",
    ]
    hint = WRITE_DISCIPLINE_HINT.get(discipline, "")
    if hint:
        parts.append(f"学科语境：{hint}")
    if notes and notes.strip():
        parts.append(f"补充要求：{notes.strip()[:300]}")
    angles = ["按问题演进的时间/脉络组织", "按核心概念的维度/要素组织", "按实践应用的场景/案例组织", "按对比分析的视角组织"]

    def _one(i):
        prompt = "\n\n".join(parts + [
            f"本版大纲的组织侧重：{angles[i % len(angles)]}，与其它版本要明显不同。",
            _outline_format_rule(n),
        ])
        try:
            raw = chat([
                {"role": "system", "content": _OUTLINE_SYS},
                {"role": "user", "content": prompt},
            ], temperature=0.6)
            secs = parse_outline(raw, enforce_limit=False)
        except Exception:
            return None  # 网络/限流/解析失败都不炸整单，交给兜底
        if not (2 <= len(secs) <= WRITE_MAX_SECTIONS):
            return None
        if len(secs) > n + 2:
            secs = secs[:n - 1] + secs[-1:]
        return secs[:WRITE_MAX_SECTIONS]

    with _TPE(max_workers=min(4, n_variants)) as ex:
        results = [r for r in ex.map(_one, range(n_variants)) if r]
    if not results:
        results = [r for r in (_one(i) for i in range(min(2, n_variants))) if r]
    if not results:
        raise RuntimeError("大纲生成失败，请重试或换一个题目")
    return results[:3]


# ── 文献搜索：DuckDuckGo 检真实网页题录（与查重引擎同款容错）───────────────
def search_references(topic: str, query: str, n: int = 6) -> list:
    """按用户输入的检索词搜真实文献线索。返回 [{title, source, url}]，失败抛 RuntimeError。
    结果是网页题录，格式化为 GB/T 由前端/用户完成——保留用户核对原文的主动权。"""
    q = (query or topic or "").strip()
    if not (2 <= len(q) <= 60):
        raise ValueError("检索词需 2-60 个字")
    n = max(3, min(10, n))
    try:
        from duckduckgo_search import DDGS
    except Exception:
        raise RuntimeError("服务器未装搜索依赖，请用「手动添加」或推荐列表")

    def _ddg_once():
        return list(DDGS().text(q + " 期刊 论文", max_results=n * 2, region="cn-zh"))

    from concurrent.futures import ThreadPoolExecutor as _TPE
    results = []
    last_err = None
    for _ in range(2):
        with _TPE(max_workers=1) as ex:
            try:
                # 硬超时：DDG 偶发挂死（连接池饿死）不能拖住整个请求
                results = ex.submit(_ddg_once).result(timeout=25)
                break
            except Exception as e:
                last_err = e
                results = []
    if not results:
        raise RuntimeError("搜索暂时不可用（超时），请稍后重试或手动添加")

    out, seen = [], set()
    for r in results:
        title = (r.get("title", "") or "").strip()
        url = r.get("href") or r.get("url") or ""
        body = (r.get("body", "") or r.get("snippet", "") or "").strip()
        if not title or len(title) < 8 or title in seen:
            continue
        seen.add(title)
        # 来源站点名（简陋但够用的展示线索）
        src = ""
        m = re.match(r"https?://(?:www\.)?([^/]+)", url)
        if m:
            src = m.group(1)
        out.append({"title": title[:80], "source": src, "url": url,
                    "snippet": body[:100]})
        if len(out) >= n:
            break
    if not out:
        raise RuntimeError("没搜到相关文献线索，换个检索词试试")
    return out
