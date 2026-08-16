"""降重 web service — FastAPI over engine.py + docx I/O + time-based quota."""
import json
import time
import secrets
import traceback
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import engine
import docx_io
import quota
import detectors
import history
import pdf_report
import pay
import wechatpay

BASE_DIR = Path(__file__).parent
MAX_CHARS = engine.MAX_CHARS
FILE_MAX_CHARS = 20000

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class RewriteReq(BaseModel):
    text: str = Field(...)
    strength: str = "medium"
    mode: str = "pipeline"
    discipline: str = "auto"


class DocReq(BaseModel):
    text: str


class RedeemReq(BaseModel):
    code: str


class GenReq(BaseModel):
    secret: str
    n: int = 1
    seconds: int = 3600   # 一个兑换码 = 多少秒使用时间（默认 1 小时）


app = FastAPI(title="jiangzhong")
app.mount("/assets", StaticFiles(directory=BASE_DIR), name="assets")


@app.middleware("http")
async def cid_middleware(request: Request, call_next):
    """Identify each client by X-Client-Id header (non-browser) or jz_cid cookie."""
    cid = (request.headers.get("x-client-id") or request.cookies.get("jz_cid")
           or quota.new_client_id())
    request.state.cid = cid
    _EXPENSIVE = {"/api/rewrite", "/api/humanize", "/api/edit-english",
                  "/api/plagiarism-check", "/api/rewrite-file", "/api/make-report",
                  "/api/report-parse", "/api/report-rewrite",
                  "/api/write-outline", "/api/write-generate",
                  "/api/write-part", "/api/sentence-rewrite"}
    if request.url.path in _EXPENSIVE and request.method == "POST":
        rl = _rate_check(request)
        if rl:
            return rl
    response = await call_next(request)
    if not (request.headers.get("x-client-id") or request.cookies.get("jz_cid")):
        response.set_cookie("jz_cid", cid, httponly=True, max_age=31536000, samesite="lax")
    return response


def _validate_opts(strength, mode, discipline):
    if strength not in engine.STRENGTH_INSTR:
        return "strength 必须是 light / medium / deep"
    if mode not in ("pipeline", "simple"):
        return "mode 必须是 pipeline / simple"
    if discipline not in engine.DISCIPLINE_INSTR:
        return "discipline 非法"
    return None


def _engine_err(e: Exception):
    """GLM/网络故障 → 502 + 干净中文；其它异常 → 500（traceback 落日志便于排障）。"""
    msg = str(e)
    if any(k in msg for k in ("网络", "超时", "GLM", "timed out", "HTTP 4", "HTTP 5", "空内容", "响应结构",
                              "改写候选")):
        return JSONResponse({"error": "AI 服务暂时不可用，请稍后重试"}, status_code=502)
    traceback.print_exc()
    return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


def _save_history(request: Request, task: str, orig_text: str, result: dict):
    """任务成功后落一条历史记录。失败绝不影响主响应。"""
    try:
        slim = {k: v for k, v in result.items() if k != "quota"}
        history.add(request.state.cid, task, orig_text, slim)
    except Exception:
        traceback.print_exc()


# ── 请求频率限制（防 DoS / 防 GLM 额度被刷）────────────────────────────────
_rate_log = {}  # cid -> [timestamps]

def _rate_check(request, limit=15, window=60):
    """每客户端 window 秒内最多 limit 次昂贵请求。超限返回 429。"""
    cid = request.state.cid
    now = time.time()
    recent = [t for t in _rate_log.get(cid, []) if now - t < window]
    if len(recent) >= limit:
        return JSONResponse({"error": "请求过于频繁，请稍后再试"}, status_code=429)
    recent.append(now)
    _rate_log[cid] = recent
    if len(_rate_log) > 5000:
        _rate_log.clear()
    return None


# ── 逐句打分 helper（报告逐句标色用，不调模型，纯 bigram）───────────────────
import re as _re


def _bg(s):
    s = _re.sub(r"\s+", "", s)
    return set(s[i:i + 2] for i in range(len(s) - 1)) if len(s) > 1 else set()


def _split_sents(text):
    return [s.strip() for s in _re.split(r"[。！？\n;；]+", text or "") if s.strip()]


def _rewrite_sentence_scores(orig_text, rewritten_text):
    """逐句「改写后 vs 原文」相似度 → 双栏表右列上色。
    高=改得不够(红 err)、中=部分残留(橙 warn)、低=改得好(绿 ok)。"""
    orig_sents = _split_sents(orig_text)
    orig_whole = _bg(orig_text)
    out = []
    for r in _split_sents(rewritten_text):
        R = _bg(r)
        best = 0.0
        for o in orig_sents:
            O = _bg(o)
            if R and O:
                v = len(R & O) / min(len(R), len(O))
                if v > best:
                    best = v
        if best == 0.0 and R and orig_whole:
            best = len(R & orig_whole) / len(R)
        score = round(best * 100)
        color = "err" if best >= 0.55 else ("warn" if best >= 0.30 else "ok")
        out.append({"sentence": r, "overlap": score, "color": color})
    return out


def _plag_sentence_scores(text, all_matches):
    """逐句查重命中度 → 正文逐句标色。
    web 命中=真实 overlap；GLM suspect=标称 60；未命中=0。
    ≥50 红、30-49 橙、<30 绿。"""
    by_sent = {}
    for m in all_matches:
        key = (m.get("sentence") or "").strip()
        if key and key not in by_sent:
            by_sent[key] = m
    out = []
    for s in _split_sents(text):
        m = by_sent.get(s)
        if m:
            if (m.get("title") or "").startswith("GLM"):
                score = 60
            else:
                try:
                    score = int(m.get("overlap", 0))
                except Exception:
                    score = 0
        else:
            score = 0
        color = "err" if score >= 50 else ("warn" if score >= 30 else "ok")
        out.append({"sentence": s, "overlap": score, "color": color})
    return out


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "model": engine.MODEL,
        "key_configured": bool(engine.KEY),
        "max_chars": MAX_CHARS,
        "file_max_chars": FILE_MAX_CHARS,
        "free_trial_seconds": quota.FREE_TRIAL_SECONDS,
        "admin_secret_default": quota.ADMIN_SECRET == "dev-secret-change-me",
    }


@app.get("/api/quota")
def get_quota(request: Request):
    return quota.get_state_summary(request.state.cid)


@app.post("/api/redeem")
def redeem(req: RedeemReq, request: Request):
    res = quota.redeem(request.state.cid, req.code)
    status = 200 if res.get("ok") else 400
    return JSONResponse(res, status_code=status)


@app.post("/api/admin/gen-codes")
def admin_gen_codes(req: GenReq):
    if req.secret != quota.ADMIN_SECRET:
        return JSONResponse({"error": "secret 错误"}, status_code=403)
    if not (1 <= req.n <= 10000) or not (60 <= req.seconds <= 31_536_000):
        return JSONResponse({"error": "n 或 seconds 超出范围"}, status_code=400)
    codes = quota.gen_codes(req.n, req.seconds)
    mins = req.seconds // 60
    return {"codes": codes, "seconds_each": req.seconds, "minutes_each": mins, "count": len(codes),
            "note": f"每个码可用 {mins} 分钟。码作为卡密在淘宝售卖，买家粘贴后即激活"}


@app.post("/api/rewrite")
def rewrite(req: RewriteReq, request: Request):
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY（检查 ~/.claude.json）"}, status_code=500)
    text = req.text.strip()
    if len(text) < 10:
        return JSONResponse({"error": "文本太短，请至少输入 10 个字"}, status_code=400)
    if len(text) > MAX_CHARS:
        return JSONResponse({"error": f"文本过长（>{MAX_CHARS} 字），请分段处理"}, status_code=413)
    err = _validate_opts(req.strength, req.mode, req.discipline)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        data = (engine.rewrite_simple(text, req.strength, req.discipline) if req.mode == "simple"
                else engine.rewrite_pipeline(text, req.strength, req.discipline))
        data["orig_text"] = text
        data["sentence_scores"] = _rewrite_sentence_scores(text, data.get("rewrite", ""))
        data["quota"] = quota.consume(request.state.cid)
        _save_history(request, "rewrite", text, data)
        return data
    except Exception as e:
        return _engine_err(e)


class HumanizeReq(BaseModel):
    text: str = Field(...)
    strength: str = "medium"


@app.post("/api/humanize")
def humanize(req: HumanizeReq, request: Request):
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY"}, status_code=500)
    text = req.text.strip()
    if len(text) < 10:
        return JSONResponse({"error": "文本太短，请至少输入 10 个字"}, status_code=400)
    if len(text) > MAX_CHARS:
        return JSONResponse({"error": f"文本过长（>{MAX_CHARS} 字），请分段处理"}, status_code=413)
    if req.strength not in engine.STRENGTH_INSTR:
        return JSONResponse({"error": "strength 必须是 light / medium / deep"}, status_code=400)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        data = engine.rewrite_humanize(text, req.strength)
        data["orig_text"] = text
        data["sentence_scores"] = _rewrite_sentence_scores(text, data.get("rewrite", ""))
        data["quota"] = quota.consume(request.state.cid)
        _save_history(request, "humanize", text, data)
        return data
    except Exception as e:
        return _engine_err(e)


class EnglishReq(BaseModel):
    text: str = Field(...)
    strength: str = "medium"
    sub: str = "polish"


@app.post("/api/edit-english")
def edit_english(req: EnglishReq, request: Request):
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY"}, status_code=500)
    text = req.text.strip()
    if len(text) < 10:
        return JSONResponse({"error": "文本太短，请至少输入 10 个字"}, status_code=400)
    if len(text) > MAX_CHARS:
        return JSONResponse({"error": f"文本过长（>{MAX_CHARS} 字），请分段处理"}, status_code=413)
    if req.strength not in engine.STRENGTH_INSTR:
        return JSONResponse({"error": "strength 必须是 light / medium / deep"}, status_code=400)
    if req.sub not in ("polish", "dedup", "translate"):
        return JSONResponse({"error": "sub 必须是 polish / dedup / translate"}, status_code=400)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        data = engine.rewrite_english(text, req.strength, req.sub)
        data["orig_text"] = text
        data["sentence_scores"] = _rewrite_sentence_scores(text, data.get("rewrite", ""))
        data["quota"] = quota.consume(request.state.cid)
        _save_history(request, "english", text, data)
        return data
    except Exception as e:
        return _engine_err(e)


class CheckReq(BaseModel):
    text: str = Field(...)


@app.post("/api/aigc-check")
def aigc_check(req: CheckReq, request: Request):
    text = req.text.strip()
    if len(text) < 10:
        return JSONResponse({"error": "文本太短，请至少输入 10 个字"}, status_code=400)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    import ai_detector

    # ① 先调 GLM（快，2-3秒，趁内存还没被 GPT-2 占用）
    glm_score = -1
    glm_reason = ""
    if len(text) >= 20:
        try:
            glm_score, glm_reason = _glm_aigc_judge(text)
        except Exception as e:
            glm_reason = f"暂时不可用: {type(e).__name__}"

    # ② 统计分析（快，内存小）
    heu_data = detectors.detect_aigc(text)
    heu_score = heu_data.get("aigc_score", 50)

    # ③ GPT-2 困惑度（慢，占内存，放最后）
    gpt2_data = ai_detector.detect_aigc(text)
    gpt2_score = gpt2_data.get("aigc_score", 50)
    if gpt2_score < 0:
        gpt2_score = 50

    # 混合：GLM 最准（50%）+ GPT-2 困惑度（30%）+ 统计（20%）
    # 但如果 GLM 和 GPT-2 都>60（两个引擎都认为偏AI），额外加10分
    if glm_score >= 0 and "不可用" not in glm_reason:
        final = round(gpt2_score * 0.30 + glm_score * 0.50 + heu_score * 0.20)
        if glm_score >= 60 and gpt2_score >= 55:
            final = min(97, final + 10)  # 双引擎一致认为偏AI → 加分
    else:
        final = round(gpt2_score * 0.55 + heu_score * 0.45)
    final = max(3, min(97, final))

    if final < 35:
        verdict, color = "偏低（偏人类写作）", "ok"
    elif final < 65:
        verdict, color = "中等（不确定）", "warn"
    else:
        verdict, color = "偏高（偏 AI 生成）", "err"

    signals = gpt2_data.get("signals", [])
    if glm_score >= 0:
        signals.append({"name": "GLM 判断", "value": f"{glm_score}%", "score": glm_score, "hint": glm_reason})
    signals.append({"name": "统计特征", "value": f"{heu_score}%", "score": heu_score,
                    "hint": "套话/句长分析"})

    # 逐句 AIGC 疑似度（报告逐句标色用，复用已加载的 GPT-2，零额外模型开销）
    try:
        sentence_scores = ai_detector.score_sentences_aigc(text, gpt2_data.get("_sent_ppls"))
    except Exception:
        sentence_scores = []

    result = {
        "aigc_score": final,
        "verdict": verdict,
        "color": color,
        "note": "三引擎混合检测：GPT-2 困惑度 + GLM 语义判断 + 统计特征。综合判断，结果仅供参考。",
        "signals": signals,
        "perplexity": gpt2_data.get("perplexity", 0),
        "burstiness": gpt2_data.get("burstiness", 0),
        "glm_reason": glm_reason,
        "sentence_scores": sentence_scores,
        "sentence_count": gpt2_data.get("sentence_count", 0),
        "char_count": gpt2_data.get("char_count", 0),
        "quota": quota.consume(request.state.cid),
    }
    _save_history(request, "aigc", text, result)
    return result


def _glm_aigc_judge(text):
    """让 GLM 判断文本是否 AI 生成，返回 (score, reason)。"""
    prompt = (
        "你是AI文本检测专家。分析以下中文文本是否由AI生成。\n\n"
        "注意区分「正式学术写作」和「AI生成文本」——它们不同：\n"
        "- 正式学术写作：虽然书面化、有术语，但有个人视角、具体案例、独特论述角度\n"
        "- AI生成文本：除了书面化之外，还有以下【多重】特征同时出现才算AI：\n"
        "  a) 每段结构高度一致（背景→分析→结论的套路重复）\n"
        "  b) 大量排比对仗和工整的四字/六字短语\n"
        "  c) 几乎每句都是长句，没有短句穿插\n"
        "  d) 抽象空洞（很多正确但缺少具体信息的话）\n"
        "  e) 缺少具体的数字、日期、人名、案例等细节\n\n"
        "如果文本包含具体的案例、个人创作经历、明确的数字/引用/人名，说明是人类写的，降低分数。\n"
        "只有当上述a-e中至少3个特征同时出现时才给高分（>70）。\n\n"
        '只输出JSON：{"score": 0-100的数字, "reason": "一句话说明命中了哪些特征"}\n\n'
        f"待检测文本：\n{text[:3000]}"
    )
    msg = engine.chat([
        {"role": "system", "content": "你是AI文本检测专家，只输出JSON。"},
        {"role": "user", "content": prompt},
    ], temperature=0.1)
    import re as _re
    m = _re.search(r'\{[^}]+\}', msg, _re.S)
    if m:
        obj = json.loads(m.group(0))
        return int(obj.get("score", 50)), obj.get("reason", "")
    return 50, "判断失败"


@app.post("/api/plagiarism-check")
def plagiarism_check(req: CheckReq, request: Request):
    text = req.text.strip()
    if len(text) < 10:
        return JSONResponse({"error": "文本太短，请至少输入 10 个字"}, status_code=400)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    # ① 先卸载 GPT-2（释放内存给 GLM HTTP 请求）
    try:
        import ai_detector, gc
        ai_detector._model = None
        gc.collect()
    except Exception:
        pass
    # ② 调 GLM（查重主力）
    glm_result = {"score": 0, "suspects": [], "reason": "", "paragraphs": []}
    try:
        glm_result = _glm_plagiarism_judge(text)
    except Exception as e:
        glm_result["reason"] = f"GLM 分析暂时不可用: {type(e).__name__}"

    # ② 统计分析（辅助，估算常见表达占比）
    heu = detectors.detect_aigc(text)
    heu_score = heu.get("aigc_score", 30)  # 统计AIGC分高≈套话多≈查重风险高
    # 统计AIGC分 → 查重风险（套话多≈重复风险高）
    stat_score = min(heu_score + 10, 80)  # 微调：统计分+10作为查重估算

    # ③ 网络搜索（辅助）— 抓逐字复制
    web_result = detectors.check_plagiarism(text, max_checks=6)
    web_score = web_result.get("similarity_score", 0) if "error" not in web_result else 0
    web_matches = web_result.get("matches", []) if "error" not in web_result else []

    # ④ 合并：GLM(80%) + 统计(15%) + 网络(5%)
    # GLM 单引擎区分度 55 分，统计仅 14 分且会误判人类原创，故 GLM 权重最大化
    glm_s = glm_result.get("score")
    if glm_s is None:
        # GLM 两次重试都失败：用统计+网络兜底，避免给 0 分误判为"原创度较高"
        final_score = round(stat_score * 0.80 + web_score * 0.20)
    else:
        final_score = round(glm_s * 0.80 + stat_score * 0.15 + web_score * 0.05)
    final_score = max(5, min(95, final_score))
    if final_score < 20:
        verdict, color = "原创度较高", "ok"
    elif final_score < 50:
        verdict, color = "部分内容可能存在重复", "warn"
    else:
        verdict, color = "重复风险较高", "err"
    # 构建报告
    all_matches = []
    for s in glm_result.get("suspects", []):
        all_matches.append({"sentence": s, "overlap": 0, "title": "GLM 识别：疑似非原创", "url": ""})
    for m in web_matches:
        all_matches.append(m)
    para_risks = glm_result.get("paragraphs", [])
    sentence_scores = _plag_sentence_scores(text, all_matches)
    result = {
        "similarity_score": final_score,
        "verdict": verdict,
        "color": color,
        "matched_count": len(all_matches),
        "checked_count": web_result.get("checked_count", 0),
        "matches": all_matches[:10],
        "sentence_scores": sentence_scores,
        "paragraphs": para_risks,
        "glm_reason": glm_result.get("reason", ""),
        "perplexity": 0,
        "note": "三引擎查重：GLM 原创度分析 + 统计特征 + 互联网搜索。综合估算，接近但不等于知网精确查重，仅供参考。",
        "quota": quota.consume(request.state.cid),
    }
    _save_history(request, "plagiarism", text, result)
    return result


def _glm_plagiarism_judge(text):
    """让 GLM 做原创度分析，带具体校准例题。
    temperature=0 + markdown strip + 1 次重试，避免 GLM 偶发不吐 JSON 导致 score=0 误判。"""
    prompt = (
        "你是学术查重系统。判断这段文字的查重率估算（0-100）。\n\n"
        "【校准例题】以下是已知查重率的文本片段，参照打分：\n"
        "例1：「人工智能是研究、开发用于模拟、延伸和扩展人的智能的理论方法技术及应用系统的技术科学」→ 标准定义，到处都有 → 查重率 92分\n"
        "例2：「随着深度学习技术的快速发展，卷积神经网络在图像识别领域取得了显著突破」→ 常见背景介绍，论文高频句 → 75分\n"
        "例3：「本文从XX视角出发，结合XX理论，通过XX分析，探讨XX问题」→ 方法论套话 → 65分\n"
        "例4：「本研究在ResNet-50基础上引入了通道注意力模块，参数量减少了32%」→ 有具体方法+数据，部分原创 → 30分\n"
        "例5：「我在创作过程中发现，直接套用马格利特的手法效果并不理想」→ 个人创作经历，独特观点 → 12分\n\n"
        "【打分规则】\n"
        "通篇都是万能套话/标准定义/模板背景，无任何具体数据/人名/案例 → 70-90分\n"
        "大部分是套话但有少量具体技术名词 → 45-65分\n"
        "有具体实验数据/方法/案例穿插 → 25-40分\n"
        "大量个人经历/原创观点/独特案例 → 10-20分\n\n"
        '只输出一个JSON对象，禁止输出markdown代码块、反引号或任何解释文字：{"score":数字,"suspects":["最像套话的句子1","句子2"],"reason":"一句评价"}\n\n'
        f"文本：\n{text[:2500]}"
    )
    import re as _re
    sys_msg = "你是查重分析员。回复必须且只能是一个JSON对象，以 { 开头、} 结尾，不要```、不要代码块、不要任何文字解释。"
    for _ in range(2):
        msg = engine.chat([
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt},
        ], temperature=0.0)
        s = (msg or "").strip()
        s = _re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = _re.sub(r"\s*```\s*$", "", s)
        m = _re.search(r'\{.*\}', s, _re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                return {
                    "score": int(obj.get("score", 0)),
                    "suspects": [x[:80] for x in obj.get("suspects", [])][:4],
                    "reason": obj.get("reason", ""),
                    "paragraphs": [],
                }
            except Exception:
                pass
    return {"score": None, "suspects": [], "reason": "GLM 暂时无法分析，已用统计特征估算", "paragraphs": []}

@app.post("/api/rewrite-file")
async def rewrite_file(
    request: Request,
    file: UploadFile = File(...),
    strength: str = Form("medium"),
    mode: str = Form("pipeline"),
    discipline: str = Form("auto"),
    task: str = Form("rewrite"),
    sub: str = Form("polish"),
):
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY"}, status_code=500)
    if not (file.filename or "").lower().endswith(".docx"):
        return JSONResponse({"error": "只支持 .docx 文件"}, status_code=400)
    if task not in ("rewrite", "humanize", "english"):
        return JSONResponse({"error": "task 非法"}, status_code=400)
    err = _validate_opts(strength, mode, discipline)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    cl = request.headers.get("content-length")
    if cl and int(cl) > 10 * 1024 * 1024:
        return JSONResponse({"error": "文件过大（>10MB）"}, status_code=413)
    raw = await file.read()
    try:
        paras = docx_io.extract_paragraphs(raw)
    except Exception as e:
        return JSONResponse({"error": f"解析 docx 失败：{type(e).__name__}: {e}"}, status_code=400)
    if not paras:
        return JSONResponse({"error": "文档里没找到文本段落"}, status_code=400)

    text = "\n\n".join(paras)
    if len(text) > FILE_MAX_CHARS:
        return JSONResponse({"error": f"文档文本过长（{len(text)} > {FILE_MAX_CHARS} 字），请精简或分段"}, status_code=413)

    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        if task == "humanize":
            data = engine.rewrite_humanize(text, strength)
        elif task == "english":
            data = engine.rewrite_english(text, strength, sub)
        else:
            data = (engine.rewrite_simple(text, strength, discipline) if mode == "simple"
                    else engine.rewrite_pipeline(text, strength, discipline))
        data["orig_text"] = text
        data["quota"] = quota.consume(request.state.cid)
        _save_history(request, task, text, data)
        return data
    except Exception as e:
        return _engine_err(e)


@app.post("/api/report-parse")
async def report_parse(request: Request, file: UploadFile = File(...)):
    """查重报告解析（免费预览）：PDF/zip → 标红句列表 + 保偏移结构（供回传）。"""
    fname = (file.filename or "").lower()
    if not (fname.endswith(".pdf") or fname.endswith(".zip")):
        return JSONResponse({"error": "只支持 .pdf / .zip 文件"}, status_code=400)
    cl = request.headers.get("content-length")
    if cl and int(cl) > pdf_report.REPORT_FILE_MAX:
        return JSONResponse({"error": "文件过大（>20MB）"}, status_code=413)
    raw = await file.read()
    if len(raw) > pdf_report.REPORT_FILE_MAX:
        return JSONResponse({"error": "文件过大（>20MB）"}, status_code=413)
    try:
        parsed = pdf_report.parse_report(raw)
    except pdf_report.ReportError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": f"解析失败: {type(e).__name__}: {e}"}, status_code=500)
    return {
        "ok": True,
        "brand_guess": parsed["brand_guess"],
        "red_count": parsed["red_count"],
        "red_chars": parsed["red_chars"],
        "total_chars": parsed["total_chars"],
        "red_sents": [{"i": s["i"], "text": s["text"], "chars": s["chars"]} for s in parsed["red_sents"]],
        "structure": parsed,
    }


class ReportRewriteReq(BaseModel):
    structure: dict = Field(default_factory=dict)
    strength: str = "medium"


class WriteOutlineReq(BaseModel):
    topic: str = Field(...)
    kind: str = "general"
    words: int = 2000
    discipline: str = "auto"
    notes: str = ""


class WriteGenReq(WriteOutlineReq):
    outline: str = Field(...)


class WritePartReq(BaseModel):
    part: str = Field(...)
    topic: str = Field(...)
    text: str = ""


class SentenceRewriteReq(BaseModel):
    sentence: str = Field(...)


def _validate_write(req):
    topic = (req.topic or "").strip()
    if not (2 <= len(topic) <= 80):
        return "题目需 2-80 个字"
    if req.kind not in engine.WRITE_KIND_INSTR:
        return "文章类型非法"
    if not (engine.WRITE_MIN_WORDS <= req.words <= engine.WRITE_MAX_WORDS):
        return f"目标字数需在 {engine.WRITE_MIN_WORDS}-{engine.WRITE_MAX_WORDS} 之间"
    if req.discipline not in engine.DISCIPLINE_INSTR:
        return "discipline 非法"
    if len(req.notes or "") > 300:
        return "补充要求不超过 300 字"
    return None


@app.post("/api/report-rewrite")
def report_rewrite(req: ReportRewriteReq, request: Request):
    """报告降重：只改写 structure 里的标红句，黑字原文零改动。"""
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY"}, status_code=500)
    structure = req.structure or {}
    red_sents = structure.get("red_sents") or []
    if not structure.get("pages") or not red_sents:
        return JSONResponse({"error": "报告结构缺失，请重新上传解析"}, status_code=400)
    if req.strength not in engine.STRENGTH_INSTR:
        return JSONResponse({"error": "strength 必须是 light / medium / deep"}, status_code=400)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        context = pdf_report.build_context(structure)
        rr = engine.rewrite_report_sentences(red_sents, context, req.strength)
        orig_full, full_text = pdf_report.apply_rewrites(structure, rr["rewrites"])
        by_i = {s["i"]: s.get("text", "") for s in red_sents}
        rewrites = []
        for s in red_sents:
            i, orig = s["i"], s.get("text", "")
            new = rr["rewrites"].get(i, orig)
            A, B = _bg(orig), _bg(new)
            overlap = round(len(A & B) / min(len(A), len(B)) * 100) if (A and B) else 0
            color = "err" if overlap >= 55 else ("warn" if overlap >= 30 else "ok")
            rewrites.append({"i": i, "orig": orig, "new": new, "color": color, "overlap": overlap})
        red_orig = "".join(by_i[s["i"]] for s in red_sents)
        red_new = "".join(rr["rewrites"].get(s["i"], "") for s in red_sents)
        sim = engine.similarity(red_orig, red_new) if red_new else 1.0
        data = {
            "task": "report",
            "brand_guess": structure.get("brand_guess", "未知来源"),
            "red_count": len(red_sents),
            "red_chars": structure.get("red_chars", len(red_orig)),
            "total_chars": structure.get("total_chars", 0),
            "rewrites": rewrites,
            "failed": rr["failed"],
            "batches": rr["batches"],
            "orig_text": orig_full,
            "full_text": full_text,
            "similarity": round(sim, 3),
            "coverage": round(1 - sim, 3),
            "strength": req.strength,
            "mode": "report",
            "quota": quota.consume(request.state.cid),
        }
        _save_history(request, "report", orig_full, data)
        return data
    except Exception as e:
        return _engine_err(e)


@app.post("/api/write-outline")
def write_outline(req: WriteOutlineReq, request: Request):
    """AI 写作第一步：生成可编辑大纲。激活用户免费迭代（不 consume）。"""
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY"}, status_code=500)
    err = _validate_write(req)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        out = engine.generate_outline(req.topic.strip(), req.kind, req.words,
                                      req.discipline, req.notes)
        return {"ok": True, **out,
                "note": "大纲可自由编辑（每行一节，格式：标题：要点1；要点2），确认后再生成全文。"}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return _engine_err(e)


@app.post("/api/write-generate")
def write_generate(req: WriteGenReq, request: Request):
    """AI 写作第二步：按大纲逐节生成全文（consume 一次）。"""
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY"}, status_code=500)
    err = _validate_write(req)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        data = engine.generate_article(req.topic.strip(), req.kind, req.words,
                                       req.discipline, req.notes, req.outline)
        data["quota"] = quota.consume(request.state.cid)
        _save_history(request, "write", req.topic.strip(), data)
        return data
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return _engine_err(e)


@app.post("/api/write-part")
def write_part(req: WritePartReq, request: Request):
    """写作辅助件：摘要/关键词/致谢/开题思路。激活用户免费迭代（不 consume）。"""
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY"}, status_code=500)
    if req.part not in engine.PART_KINDS:
        return JSONResponse({"error": "part 类型非法"}, status_code=400)
    topic = (req.topic or "").strip()
    if not (2 <= len(topic) <= 80):
        return JSONResponse({"error": "题目需 2-80 个字"}, status_code=400)
    if len(req.text or "") > MAX_CHARS:
        return JSONResponse({"error": f"正文超出长度限制（>{MAX_CHARS} 字）"}, status_code=413)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        out = engine.generate_part(req.part, topic, req.text)
        return {"ok": True, "part": req.part,
                "label": engine.PART_KINDS[req.part]["label"], "text": out}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return _engine_err(e)


@app.post("/api/sentence-rewrite")
def sentence_rewrite(req: SentenceRewriteReq, request: Request):
    """句级多候选改写（编辑器行内交互，火龙果式）。扣 1 次。"""
    if not engine.KEY:
        return JSONResponse({"error": "未配置 ZHIPU_API_KEY"}, status_code=500)
    s = (req.sentence or "").strip()
    if not (6 <= len(s) <= 500):
        return JSONResponse({"error": "句子长度需在 6-500 字之间"}, status_code=400)
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        cands = engine.rewrite_sentence_candidates(s)
        return {"ok": True, "sentence": s, "candidates": cands,
                "quota": quota.consume(request.state.cid)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return _engine_err(e)


@app.get("/api/history")
def history_list(request: Request):
    return {"items": history.list_summaries(request.state.cid)}


@app.get("/api/history/{hid}")
def history_get(hid: str, request: Request):
    rec = history.get(request.state.cid, hid)
    if not rec:
        return JSONResponse({"error": "记录不存在"}, status_code=404)
    return rec


@app.delete("/api/history/{hid}")
def history_delete(hid: str, request: Request):
    ok = history.delete(request.state.cid, hid)
    if not ok:
        return JSONResponse({"error": "记录不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/make-docx")
def make_docx(req: DocReq, request: Request):
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    paras = [p.strip() for p in (req.text or "").split("\n\n") if p.strip()]
    if not paras:
        return JSONResponse({"error": "没有可导出的文本"}, status_code=400)
    try:
        blob = docx_io.build_docx(paras)
    except Exception as e:
        return JSONResponse({"error": f"生成 docx 失败：{type(e).__name__}: {e}"}, status_code=500)
    return Response(
        content=blob, media_type=DOCX_MIME,
        headers={"Content-Disposition": 'attachment; filename="rewrite.docx"'},
    )


class ReportReq(BaseModel):
    task: str
    orig_text: str = ""
    result: dict = Field(default_factory=dict)


_REPORT_NAMES = {
    "rewrite": "降重报告",
    "humanize": "降AIGC报告",
    "english": "英文修改报告",
    "aigc": "AIGC检测报告",
    "plagiarism": "查重报告",
    "report": "报告降重报告",
    "write": "AI写作报告",
}


@app.post("/api/make-report")
def make_report(req: ReportReq, request: Request):
    """生成可编辑 Word 报告（排版已算好的 result，不重跑模型、不扣额度）。
    客户端把 {task, orig_text, result} 原样回传——result 就是各任务接口已返回的 JSON。"""
    active, _, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    if req.task not in _REPORT_NAMES:
        return JSONResponse({"error": "task 非法"}, status_code=400)
    try:
        blob = docx_io.build_report(req.task, req.orig_text, req.result)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": f"生成报告失败: {type(e).__name__}: {e}"}, status_code=500)
    fname = _REPORT_NAMES[req.task] + ".docx"
    from urllib.parse import quote
    cd = (f"attachment; filename=\"report.docx\"; "
          f"filename*=UTF-8''{quote(fname)}")
    return Response(
        content=blob, media_type=DOCX_MIME,
        headers={"Content-Disposition": cd},
    )


class OrderReq(BaseModel):
    plan: str


@app.post("/api/order/create")
def order_create(req: OrderReq, request: Request):
    if req.plan not in pay.PLANS:
        return JSONResponse({"error": "套餐非法"}, status_code=400)
    plan = pay.PLANS[req.plan]
    cid = request.state.cid
    order_id = "JZ" + secrets.token_hex(8).upper()
    quota.create_order_record(order_id, cid, req.plan)
    base = str(request.base_url).rstrip("/")
    notify_url = base + "/api/order/notify"

    # ① 微信支付 v3 Native（优先）
    if wechatpay.configured():
        try:
            code_url = wechatpay.create_order(order_id, int(float(plan["price"]) * 100),
                                              f"降重工具 {plan['name']}", notify_url)
            import qrcode as _qr, io as _io, base64 as _b64
            img = _qr.make(code_url)
            buf = _io.BytesIO(); img.save(buf, format="PNG")
            qr = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
            return {"order_id": order_id, "qr_image": qr, "qr_url": code_url}
        except Exception as e:
            return JSONResponse({"error": f"微信支付下单失败: {e}"}, status_code=502)

    # ② 虎皮椒免签（备选）
    if pay.configured():
        try:
            res = pay.create_order(order_id, plan["price"], f"降重工具 {plan['name']}", notify_url, base + "/")
            if res.get("errcode") != 0:
                return JSONResponse({"error": f"虎皮椒下单失败: {res.get('errmsg')}"}, status_code=502)
            return {"order_id": order_id, "pay_url": res.get("url") or res.get("url_qrcode"), "method": "xunhupay"}
        except Exception as e:
            return JSONResponse({"error": f"支付接口错误: {e}"}, status_code=502)

    # ③ 测试模式（仅 localhost，生产环境返 503 防白嫖）
    is_local = "localhost" in str(request.base_url) or "127.0.0.1" in str(request.base_url)
    if not is_local:
        return JSONResponse({"error": "在线支付暂未配置，请使用兑换码"}, status_code=503)
    summary = quota.activate_uses(cid, plan["uses"]) if plan.get("type") == "uses" else quota.activate(cid, plan["seconds"])
    quota.mark_order_paid(order_id)
    return {"test": True, "message": "测试模式", "quota": summary}


@app.post("/api/order/notify")
async def order_notify(request: Request):
    # 微信支付 v3 回调（JSON body + RSA 签名验证）
    if wechatpay.configured():
        body_bytes = await request.body()
        headers = {k: v for k, v in request.headers.items()}
        try:
            result = wechatpay.handle_notify(headers, body_bytes)
        except Exception:
            result = None
        if result and result.get("event_type") == "TRANSACTION.SUCCESS":
            resource = result.get("resource") or result.get("decrypt") or {}
            if isinstance(resource, str):
                resource = json.loads(resource)
            trade_no = resource.get("out_trade_no", "")
            res = quota.mark_order_paid(trade_no)
            if res:
                cid, plan_key = res
                pd = pay.PLANS[plan_key]
                if pd.get("type") == "uses":
                    quota.activate_uses(cid, pd["uses"])
                else:
                    quota.activate(cid, pd["seconds"])
        return JSONResponse({"code": "SUCCESS", "message": "成功"})

    # 虎皮椒回调（form-data + MD5 hash 验签）
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if not pay.verify_notify(params):
        return Response(content="fail", media_type="text/plain")
    if params.get("status") == "OD":
        res = quota.mark_order_paid(params.get("trade_order_id", ""))
        if res:
            cid, plan = res
            quota.activate(cid, pay.PLANS[plan]["seconds"])
    return Response(content="success", media_type="text/plain")


@app.get("/api/order/status")
def order_status(order_id: str, request: Request):
    o = quota.get_order(order_id)
    if not o:
        return JSONResponse({"error": "订单不存在"}, status_code=404)
    return {"status": o["status"], "paid": o["status"] == "paid",
            "quota": quota.get_state_summary(request.state.cid)}
