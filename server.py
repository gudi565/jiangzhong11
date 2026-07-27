"""降重 web service — FastAPI over engine.py + docx I/O + time-based quota."""
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
        data["quota"] = quota.get_state_summary(request.state.cid)
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
        data["quota"] = quota.get_state_summary(request.state.cid)
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
        data["quota"] = quota.get_state_summary(request.state.cid)
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
    data = detectors.detect_aigc(text)
    data["quota"] = quota.get_state_summary(request.state.cid)
    return data


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
    data = detectors.check_plagiarism(text)
    if "error" in data:
        return JSONResponse(data, status_code=502)
    data["quota"] = quota.get_state_summary(request.state.cid)
    return data


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
        data["quota"] = quota.get_state_summary(request.state.cid)
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
