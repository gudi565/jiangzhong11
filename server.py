"""降重 web service — FastAPI over engine.py + docx I/O + time-based quota."""
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
                  "/api/plagiarism-check", "/api/rewrite-file"}
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
    if any(k in msg for k in ("网络", "超时", "GLM", "timed out", "HTTP 4", "HTTP 5", "空内容", "响应结构")):
        return JSONResponse({"error": "AI 服务暂时不可用，请稍后重试"}, status_code=502)
    traceback.print_exc()
    return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


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

    active, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        data = (engine.rewrite_simple(text, req.strength, req.discipline) if req.mode == "simple"
                else engine.rewrite_pipeline(text, req.strength, req.discipline))
        data["quota"] = quota.consume(request.state.cid)
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
    active, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        data = engine.rewrite_humanize(text, req.strength)
        data["quota"] = quota.consume(request.state.cid)
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
    active, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    try:
        data = engine.rewrite_english(text, req.strength, req.sub)
        data["quota"] = quota.consume(request.state.cid)
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
    active, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    # ① 启发式统计（辅助，30%）
    heu = detectors.detect_aigc(text)
    heu_score = heu["aigc_score"]
    # ② GLM 判断（主力，70%）— AI 最懂 AI 写的文字
    glm_score = heu_score
    glm_reason = ""
    if len(text) >= 20:
        try:
            glm_score, glm_reason = _glm_aigc_judge(text)
        except Exception:
            glm_reason = "GLM 判断暂时不可用"
    # ③ 混合
    if glm_reason and "不可用" not in glm_reason:
        final = round(glm_score * 0.7 + heu_score * 0.3)
    else:
        final = heu_score
    final = max(3, min(97, final))
    if final < 35:
        verdict, color = "偏低（偏人类写作）", "ok"
    elif final < 65:
        verdict, color = "中等（不确定）", "warn"
    else:
        verdict, color = "偏高（偏 AI 生成）", "err"
    heu["aigc_score"] = final
    heu["verdict"] = verdict
    heu["color"] = color
    if glm_reason:
        heu["signals"].append({"name": "GLM 判断", "value": f"{glm_score}%",
                               "score": glm_score, "hint": glm_reason})
    heu["note"] = glm_reason and f"AI模型判断（70%）+ 统计分析（30%）。{glm_reason}" or heu["note"]
    heu["quota"] = quota.get_state_summary(request.state.cid)
    return heu


def _glm_aigc_judge(text):
    """让 GLM 判断文本是否 AI 生成，返回 (score, reason)。"""
    prompt = (
        "你是AI文本检测专家。分析以下中文文本是否由AI生成。\n"
        "从句式工整度、词汇书面化、模板连接词频率、逻辑线性度、缺乏个人色彩等维度判断。\n"
        "给出AIGC概率（0-100），并一句话说明理由。\n"
        '只输出JSON：{"score": 数字, "reason": "一句话"}\n\n'
        f"文本：\n{text[:3000]}"
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
    active, _ = quota.is_active(request.state.cid)
    if not active:
        return JSONResponse(
            {"error": "未激活或已到期，请输入兑换码（淘宝购买）。",
             "quota": quota.get_state_summary(request.state.cid)},
            status_code=402,
        )
    # ① GLM 原创度分析（主力）— 识别常见抄袭模式、风格突变、标准定义
    glm_result = {"score": 0, "suspects": [], "reason": ""}
    try:
        glm_result = _glm_plagiarism_judge(text)
    except Exception:
        glm_result["reason"] = "GLM 分析暂时不可用"
    # ② 网络搜索（辅助）— 抓逐字复制
    web_result = detectors.check_plagiarism(text, max_checks=6)
    web_score = web_result.get("similarity_score", 0) if "error" not in web_result else 0
    web_matches = web_result.get("matches", []) if "error" not in web_result else []
    # ③ 合并：取两者最高风险
    final_score = max(glm_result["score"], web_score)
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
    # 逐段风险（GLM 独有）
    para_risks = glm_result.get("paragraphs", [])
    result = {
        "similarity_score": final_score,
        "verdict": verdict,
        "color": color,
        "matched_count": len(all_matches),
        "checked_count": web_result.get("checked_count", 0),
        "matches": all_matches[:10],
        "paragraphs": para_risks,
        "glm_reason": glm_result["reason"],
        "note": ("GLM 逐段原创度分析 + 互联网搜索双引擎。GLM 基于训练时阅读的海量学术内容判断，"
                 "接近但不等于知网/万方的精确数据库查重，结果仅供参考。"),
        "quota": quota.get_state_summary(request.state.cid),
    }
    return result


def _glm_plagiarism_judge(text):
    """让 GLM 做深度原创度分析：逐段评估 + 精确定位 + 量化。

    GLM 读过海量中文论文/教科书/百科/期刊，能模糊"记住"哪些表达是常见的。
    不是精确数据库比对，但能识别常见定义、模板化表达、风格突变（拼接抄袭）。
    """
    prompt = (
        "你是一名严谨的学术查重分析员。请对以下文本做逐段原创度分析。\n\n"
        "分析维度（每段都评）：\n"
        "- 【高频表达】该段是否包含在学术论文中极常见的定义、描述或模板句式\n"
        "- 【风格一致性】该段写作风格（用词、句长、语气）是否与上下文一致（突变=可能拼接）\n"
        "- 【独创性】该段论述是否像作者原创的观点/实验/数据，还是像转述他人\n"
        "- 【引用缺失】是否有陈述事实/他人观点但未标注引用\n\n"
        "对每段给出风险等级（low/medium/high）。\n"
        "最后给出总体重复风险（0-100）和详细评价。\n\n"
        "输出格式（严格JSON）：\n"
        '{"score": 0-100的数字, '
        '"paragraphs": [{"text": "段落前20字...", "risk": "low/medium/high", "reason": "一句原因"}], '
        '"suspects": ["最可疑的完整句子1", "最可疑的完整句子2"], '
        '"reason": "总体评价2-3句"}\n\n'
        f"待分析文本：\n{text[:4000]}"
    )
    msg = engine.chat([
        {"role": "system", "content": "你是学术查重分析员，只输出JSON，不要输出JSON以外的任何内容。"},
        {"role": "user", "content": prompt},
    ], temperature=0.15)
    import re as _re
    m = _re.search(r'\{.*\}', msg, _re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return {
                "score": int(obj.get("score", 0)),
                "paragraphs": obj.get("paragraphs", [])[:8],
                "suspects": [s[:100] for s in obj.get("suspects", [])][:5],
                "reason": obj.get("reason", ""),
            }
        except Exception:
            pass
    return {"score": 0, "paragraphs": [], "suspects": [], "reason": "分析完成"}


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

    active, _ = quota.is_active(request.state.cid)
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
        return data
    except Exception as e:
        return _engine_err(e)


@app.post("/api/make-docx")
def make_docx(req: DocReq, request: Request):
    active, _ = quota.is_active(request.state.cid)
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
