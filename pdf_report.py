"""查重报告解析 — 从知网/维普/万方等导出的 PDF 报告里定位标红句。

流程：PDF(或 zip 内 PDF) → fitz 按行提取 span 文本+颜色 → 保偏移切句 →
句内红字占比 ≥0.5 判定为标红句（全局编号 i）→ 只改写红句后按偏移重组全文。

pages[].sents 的 {start,end,red,i} 是「只换红句」的唯一依据：
客户端解析后原样回传 structure，服务端 apply_rewrites 按偏移拼接，黑字零改动。
"""
import io
import re
import zipfile

try:
    import fitz  # pymupdf
except ImportError:  # 新版包名
    import pymupdf as fitz

MAX_TEXT_CHARS = 30000   # 报告全文上限
MAX_RED_CHARS = 12000    # 标红总字数上限
REPORT_FILE_MAX = 20 * 1024 * 1024

# fitz 的 color 是 int（0xRRGGBB）
REDS = {
    (255, 0, 0), (255, 51, 51), (255, 80, 80), (237, 28, 36), (192, 0, 0),
    (204, 0, 0), (220, 20, 20), (200, 30, 30), (255, 0, 60),
}


class ReportError(Exception):
    """携带用户可读中文信息的解析失败。"""


def _is_red(color: int) -> bool:
    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    if (r, g, b) in REDS:
        return True
    # 通用红/橙色系判定（兼容各品牌导出的红色差异）
    return r > 150 and g < 130 and b < 130


def _unwrap_zip(raw: bytes) -> bytes:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ReportError("zip 压缩包损坏，无法解压")
    names = [n for n in zf.namelist()
             if n.lower().endswith(".pdf") and not n.startswith("__MACOSX") and "/." not in n]
    if not names:
        raise ReportError("压缩包里没找到 PDF 文件，请上传包含查重报告 PDF 的 zip")
    name = sorted(names)[0]
    info = zf.getinfo(name)
    if info.file_size > REPORT_FILE_MAX:
        raise ReportError("压缩包内的 PDF 超过 20MB 限制")
    return zf.read(name)


_BRANDS = [
    ("知网", ("中国知网", "CNKI", "学术不端", "combine-check")),
    ("维普", ("维普", "VIP", "CQVIP", "cqvip")),
    ("万方", ("万方", "Wanfang", "wanfangdata")),
    ("PaperPass", ("PaperPass", "paperpass", "paperpass.com")),
    ("大雅", ("大雅", "Daya", "大地雅")),
    ("格子达", ("格子达", "gezida", "GoCheck")),
    ("Turnitin", ("Turnitin", "turnitin")),
]


def _guess_brand(head: str) -> str:
    for name, keys in _BRANDS:
        for k in keys:
            if k in head:
                return name
    return "未知来源"


# 页码/表头/纯数字行过滤
_JUNK_LINE = re.compile(r"^[\d\s%.:：、,，/|\-—]*$")


def _extract_pages(raw: bytes):
    """返回 (pages, brand)。pages = [{"text": str, "spans": [(start, end, red), ...]}, ...]"""
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as e:
        raise ReportError(f"PDF 打不开：{e}")
    if doc.needs_pass:
        raise ReportError("PDF 已加密。请用浏览器打开报告后「打印为 PDF」重新导出再上传")
    pages = []
    head_text = ""
    for page in doc:
        d = page.get_text("dict", sort=True)
        text_parts, spans, pos = [], [], 0
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                line_text = "".join(s.get("text", "") for s in line.get("spans", []))
                if not line_text or _JUNK_LINE.match(line_text.strip()) or len(line_text.strip()) < 2:
                    continue
                if text_parts:
                    text_parts.append("\n")  # 行边界 = 句边界，防黑字行与红字行粘连稀释占比
                    pos += 1
                for s in line.get("spans", []):
                    st = s.get("text", "")
                    if not st:
                        continue
                    start = pos
                    text_parts.append(st)
                    pos += len(st)
                    spans.append((start, pos, _is_red(s.get("color", 0))))
        page_text = "".join(text_parts)
        if page_text.strip():
            pages.append({"text": page_text, "spans": spans})
            if len(head_text) < 3000:
                head_text += page_text
    doc.close()
    total = sum(len(p["text"]) for p in pages)
    if total < 50:
        raise ReportError("没提取到文字，疑似扫描版/图片型 PDF。请上传带文字层的报告 PDF（含标红的详细版/全文对照版）")
    return pages, _guess_brand(head_text[:3000])


_SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")


def _iter_sentences(text: str):
    """保偏移切句，yield (start, end, s)。"""
    for m in _SENT_RE.finditer(text):
        s = m.group(0)
        if s.strip():
            yield m.start(), m.end(), s


def _red_ratio(text: str, spans: list, start: int, end: int) -> float:
    """句区间 [start,end) 内红字字符数 / 非空白字符数。"""
    red_chars = total = 0
    for ss, se, red in spans:
        if se <= start or ss >= end:
            continue
        seg = text[max(ss, start):min(se, end)]
        n = len(re.sub(r"\s+", "", seg))
        total += n
        if red:
            red_chars += n
    if total == 0:  # 句子全在 span 外（不该发生，防御）
        inner = text[start:end]
        return 0.0, len(inner)
    return red_chars / total, total


def parse_report(file_bytes: bytes) -> dict:
    """总入口：解析报告 → 标红句 + 保偏移结构。"""
    head = file_bytes[:4]
    if head[:2] == b"PK":
        file_bytes = _unwrap_zip(file_bytes)
    elif not file_bytes[:5].startswith(b"%PDF"):
        raise ReportError("文件既不是 PDF 也不是 zip，请上传查重报告 PDF")

    pages, brand = _extract_pages(file_bytes)
    total_chars = sum(len(p["text"]) for p in pages)
    if total_chars > MAX_TEXT_CHARS:
        raise ReportError(f"报告文字量 {total_chars} 字，超过 {MAX_TEXT_CHARS} 字上限。请分段上传或截取标红集中的章节")

    red_sents = []
    red_chars = 0
    for pno, page in enumerate(pages):
        text, spans = page["text"], page["spans"]
        sents = []
        for start, end, s in _iter_sentences(text):
            ratio, non_ws = _red_ratio(text, spans, start, end)
            if non_ws >= 8 and ratio >= 0.5:
                i = len(red_sents)
                red_sents.append({"i": i, "text": s.strip(), "chars": non_ws, "page": pno + 1})
                sents.append({"start": start, "end": end, "red": True, "i": i})
                red_chars += non_ws
            else:
                sents.append({"start": start, "end": end, "red": False, "i": None})
        page["sents"] = sents

    if not red_sents:
        raise ReportError("没检测到标红句子。请确认上传的是查重报告（含红色标注的详细版/全文对照版），而不是原文")
    if red_chars > MAX_RED_CHARS:
        raise ReportError(f"标红部分共 {red_chars} 字，超过 {MAX_RED_CHARS} 字上限。请截取标红集中的章节分次处理")

    return {
        "brand_guess": brand,
        "red_chars": red_chars,
        "total_chars": total_chars,
        "red_count": len(red_sents),
        "red_sents": red_sents,
        "pages": pages,
    }


def build_context(parsed: dict) -> dict:
    """{红句编号: 前后文 160 字片段}，供 GLM 理解代词与逻辑。"""
    ctx = {}
    for pno, page in enumerate(parsed["pages"]):
        text = page["text"]
        for sent in page["sents"]:
            if sent.get("i") is None:
                continue
            ctx[sent["i"]] = text[max(0, sent["start"] - 80):sent["end"] + 80]
    return ctx


def apply_rewrites(parsed: dict, rewrite_map: dict):
    """重组全文：句区间内红句替换为 rewrite_map[i]，其余字符零改动。"""
    orig_parts, new_parts = [], []
    for page in parsed["pages"]:
        text = page["text"]
        op, np_, pos = [], [], 0
        for sent in page["sents"]:
            seg_orig = text[pos:sent["start"]]
            op.append(seg_orig)
            np_.append(seg_orig)
            seg = text[sent["start"]:sent["end"]]
            op.append(seg)
            i = sent.get("i")
            np_.append(rewrite_map.get(i, seg) if i is not None else seg)
            pos = sent["end"]
        op.append(text[pos:])
        np_.append(text[pos:])
        orig_parts.append("".join(op))
        new_parts.append("".join(np_))
    return "\n".join(orig_parts), "\n".join(new_parts)
