const SAMPLE = (
  "近年来，随着深度学习技术的快速发展，卷积神经网络在图像识别领域取得了显著突破[1]。" +
  "He 等人于 2016 年提出的残差网络（ResNet）通过引入跳跃连接，有效缓解了深层网络中的梯度消失问题，" +
  "在 ImageNet 数据集上将 top-5 错误率降低至 3.57%[2]。" +
  "然而，这些模型通常需要大规模标注数据和昂贵的计算资源，限制了其在医疗、金融等数据敏感领域的应用。\n\n" +
  "本研究提出一种基于注意力机制轻量化方法。该方法在 ImageNet 数据集上进行了评估，" +
  "实验结果表明，模型参数量减少了 32%，同时 top-1 准确率保持在 91.2%[3]。" +
  "由此可见，轻量化设计在保证性能的同时，能显著降低部署成本。"
);

const LABEL_ZH = { review: "综述", method: "方法", result: "结果", conclusion: "结论", general: "通用" };
const STAGE_ZH = { classify: "分类", rewrite: "重写", verify: "校验", retry: "重试", repair: "修复" };
const DISC_ZH = { auto: "自动", stem: "理工", humanities: "人文社科", medicine: "医学", law: "法学" };

const $ = (id) => document.getElementById(id);
let strength = "medium";

$("strength").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  document.querySelectorAll("#strength button").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  strength = b.dataset.v;
});

let englishSub = "polish";
$("english-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  document.querySelectorAll("#english-mode button").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  englishSub = b.dataset.sub;
});

let task = "rewrite";
let busy = false;
const TASK_LABELS = { rewrite: "改写", humanize: "降 AIGC", english: "英文修改", aigc: "查 AIGC 率", plagiarism: "查重", report: "报告降重", write: "AI 写作", history: "历史" };
document.querySelector(".tabs").addEventListener("click", (e) => {
  const t = e.target.closest(".tab");
  if (!t || t.disabled) return;
  if (busy) return;  // 操作进行中不允许切 tab，避免按钮状态/结果串台
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  task = t.dataset.task;
  const isRewrite = task === "rewrite";
  const isCheck = task === "aigc" || task === "plagiarism";
  const isPanelTask = task === "report" || task === "history" || task === "write";
  document.querySelector(".pipe-toggle").style.display = isRewrite ? "" : "none";
  document.querySelector(".field").style.display = isRewrite ? "" : "none";
  document.getElementById("strength").style.display = isCheck ? "none" : "";
  document.getElementById("upload").style.display = isCheck ? "none" : "";
  document.getElementById("english-mode").style.display = (task === "english") ? "" : "none";
  document.getElementById("input").style.display = isPanelTask ? "none" : "";
  document.querySelector(".controls").style.display = isPanelTask ? "none" : "";
  document.getElementById("report-panel").hidden = task !== "report";
  document.getElementById("history-panel").hidden = task !== "history";
  document.getElementById("write-panel").hidden = task !== "write";
  document.getElementById("report-full-wrap").hidden = true;
  if (task === "history") loadHistory();
  $("go").textContent = TASK_LABELS[task] || "改写";
  $("result").hidden = true;
  $("check-result").hidden = true;
  $("err").hidden = true;
});

$("sample").addEventListener("click", () => { $("input").value = SAMPLE; });

$("pipe").addEventListener("change", () => {
  $("mode-label").textContent = $("pipe").checked ? "质量管线" : "单模型";
});

$("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(currentOutput); flash($("copy"), "已复制"); }
  catch { flash($("copy"), "复制失败"); }
});

function flash(btn, text) {
  const old = btn.textContent;
  btn.textContent = text;
  setTimeout(() => { btn.textContent = old; }, 1200);
}

let currentOutput = "";

// 缓存最近一次结果，供「下载报告」回传给 /api/make-report 排版（不重跑模型）
let lastReport = null;

const REPORT_NAMES = {
  rewrite: "降重报告.docx", humanize: "降AIGC报告.docx", english: "英文修改报告.docx",
  aigc: "AIGC检测报告.docx", plagiarism: "查重报告.docx", report: "报告降重报告.docx",
  write: "AI写作报告.docx",
};

async function downloadReportPayload(payload, btn) {
  try {
    const r = await fetch("/api/make-report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (r.status === 402) { const d = await r.json(); handleQuotaError(d); return; }
    if (!r.ok) { let m = "HTTP " + r.status; try { const d = await r.json(); m = d.error || m; } catch {} showError(m); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const cd = r.headers.get("Content-Disposition") || "";
    let dlName = REPORT_NAMES[payload.task] || "报告.docx";
    const star = cd.match(/filename\*=UTF-8''([^;]+)/);
    if (star) dlName = decodeURIComponent(star[1]);
    else { const m = cd.match(/filename="([^"]+)"/); if (m && m[1]) dlName = m[1]; }
    a.download = dlName;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    if (btn) flash(btn, "已下载");
  } catch (e) {
    showError("下载报告失败：" + (e.message || e));
  }
}

async function downloadReport() {
  if (!lastReport) return;
  const btn = $("dl-report") || $("dl-report-aigc") || $("dl-report-plag");
  await downloadReportPayload(lastReport, btn);
}

// ── quota（时间制：active + 剩余秒数）────────────────────────────────────
function fmtDuration(sec) {
  if (sec <= 0) return "已到期";
  const h = Math.floor(sec / 3600), m = Math.ceil((sec % 3600) / 60);
  return h > 0 ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}
function setQuota(summary) {
  if (!summary) return;
  const chip = document.querySelector(".quota-chip");
  if (summary.active) {
    let parts = [];
    if (summary.remaining_uses > 0) parts.push(`剩余 <b>${summary.remaining_uses}</b> 次`);
    if (summary.time_active) parts.push(`剩余 <b>${fmtDuration(summary.remaining_seconds)}</b>`);
    chip.innerHTML = parts.join(" · ") || "已激活";
  } else {
    chip.innerHTML = `<b>未激活</b> · 需购买`;
  }
}

async function fetchQuota() {
  try {
    const r = await fetch("/api/quota");
    if (r.ok) { const d = await r.json(); setQuota(d); }
  } catch {}
}

$("redeem-btn").addEventListener("click", async () => {
  const code = $("redeem-code").value.trim();
  if (!code) { $("redeem-code").focus(); return; }
  const btn = $("redeem-btn");
  btn.disabled = true;
  try {
    const r = await fetch("/api/redeem", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const d = await r.json();
    if (d.ok) {
      setQuota(d);
      $("redeem-code").value = "";
      flash(btn, `+${Math.round((d.added_seconds || 0) / 60)} 分钟`);
    } else {
      flash(btn, "失败");
      showError(d.error || "兑换失败");
    }
  } catch (e) { showError(e.message || String(e)); }
  finally { btn.disabled = false; }
});

fetchQuota();

// ── 购买时长（站内支付）─────────────────────────────────────────────────
$("buy-btn").addEventListener("click", () => {
  $("buy-panel").hidden = !$("buy-panel").hidden;
  $("buy-status").textContent = "";
});

// 支付弹窗:点套餐后弹出,可关闭切换其他套餐
const payModal = $("pay-modal");
let payToken = 0;
function closePayModal() { payToken++; payModal.hidden = true; }
$("pay-modal-close").addEventListener("click", closePayModal);
payModal.addEventListener("click", (e) => { if (e.target === payModal) closePayModal(); });
const PLAN_NAMES = {
  u5: "5 次 ¥4.9", u10: "10 次 ¥8.9", u30: "30 次 ¥19.9",
  "1h": "1 小时 ¥4.99", "3h": "3 小时 ¥12.99", "1d": "1 天 ¥29.9", "7d": "7 天 ¥69.9",
};
function setModalBody(html) { if (payModal.hidden) return; $("pay-modal-body").innerHTML = html; }

document.querySelectorAll(".plan-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const plan = btn.dataset.plan;
    const myToken = ++payToken; // 让之前的轮询失效
    $("pay-modal-title").textContent = PLAN_NAMES[plan] || "支付";
    payModal.hidden = false;
    setModalBody('<div class="pay-status">正在创建订单…</div>');
    try {
      const r = await fetch("/api/order/create", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan }),
      });
      const data = await r.json();
      if (myToken !== payToken) return; // 已被关闭或切换
      if (!r.ok) { setModalBody('<div class="pay-status">❌ ' + (data.error || "下单失败") + '</div>'); return; }
      if (data.test) {
        if (data.quota) setQuota(data.quota);
        setModalBody('<div class="pay-status">✅ 测试模式:已直接开通</div>');
        setTimeout(() => { if (myToken === payToken) closePayModal(); }, 1500);
        return;
      }
      if (!data.qr_image) { setModalBody('<div class="pay-status">订单创建失败</div>'); return; }
      setModalBody('<img src="' + data.qr_image + '" class="pay-qr" />'
        + '<a href="' + data.qr_url + '" target="_blank" class="pay-link">手机用户点此支付 →</a>'
        + '<div class="pay-hint">微信扫码支付,完成后自动开通</div>');
      const oid = data.order_id;
      for (let i = 0; i < 90; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        if (myToken !== payToken) return; // 被关闭或切换
        const sr = await fetch(`/api/order/status?order_id=${oid}`);
        const sd = await sr.json();
        if (sd.paid) {
          if (sd.quota) setQuota(sd.quota);
          setModalBody('<div class="pay-status">✅ 开通成功!可以用了</div>');
          setTimeout(() => { if (myToken === payToken) closePayModal(); }, 2000);
          return;
        }
      }
      if (myToken === payToken) setModalBody('<div class="pay-status">未检测到支付,如已付款请联系客服</div>');
    } catch (e) {
      if (myToken === payToken) setModalBody('<div class="pay-status">❌ ' + (e.message || String(e)) + '</div>');
    }
  });
});

// ── text utilities ───────────────────────────────────────────────────────
function bigrams(s) {
  s = s.replace(/\s+/g, "");
  if (s.length < 2) return s ? new Set([s]) : new Set();
  const set = new Set();
  for (let i = 0; i < s.length - 1; i++) set.add(s.slice(i, i + 2));
  return set;
}
function sim(a, b) {
  const A = bigrams(a), B = bigrams(b);
  if (A.size === 0 && B.size === 0) return 1;
  if (A.size === 0 || B.size === 0) return 0;
  let inter = 0;
  for (const x of A) if (B.has(x)) inter++;
  return inter / (new Set([...A, ...B]).size);
}
function splitSentences(t) {
  return (t.match(/[^。；！？\n]+[。；！？]?/g) || []).map((s) => s.trim()).filter(Boolean);
}

function renderText(el, text, highlightAgainst) {
  el.innerHTML = "";
  if (highlightAgainst === undefined) { el.append(text); return; }
  const origSents = splitSentences(highlightAgainst);
  const newSents = splitSentences(text);
  const doHighlight = origSents.length <= 50 && newSents.length <= 50;  // 大文本跳过逐句高亮，避免渲染卡顿
  for (const s of newSents) {
    const span = document.createElement("span");
    span.className = "sentence";
    span.textContent = s;
    if (doHighlight) {
      const best = origSents.length ? Math.max(...origSents.map((o) => sim(s, o))) : 0;
      if (best < 0.55) span.classList.add("hot");
    }
    el.appendChild(span);
  }
}

function renderDiag(data) {
  const el = $("diag");
  if (data.mode !== "pipeline" || !data.diagnostics) { el.hidden = true; return; }
  el.hidden = false;
  const labels = data.diagnostics.map((d) => LABEL_ZH[d.label] || d.label);
  const retries = data.diagnostics.reduce((s, d) => s + d.retries, 0);
  const repaired = data.diagnostics.filter((d) => d.repaired).length;
  const missing = data.diagnostics.flatMap((d) => d.still_missing || []);
  const stageStr = (data.stages || []).map((s) => STAGE_ZH[s] || s).join(" → ");
  const disc = data.discipline && data.discipline !== "auto" ? `学科：${DISC_ZH[data.discipline] || data.discipline} <span class="sep">·</span> ` : "";
  el.innerHTML =
    `<span class="badge">管线</span> ${stageStr} <span class="sep">·</span> ${disc}段落类型：${labels.join(" / ")}` +
    ` <span class="sep">·</span> 重试 ${retries} · 修复 ${repaired}` +
    (missing.length ? ` <span class="warn">⚠ 仍有标记未保留：${missing.join(", ")}</span>` : "");
}

function showResult(data, origText, taskOverride) {
  currentOutput = data.rewrite;
  lastReport = { task: taskOverride || task, orig_text: origText, result: data };
  document.querySelector(".metrics").style.display = (data.mode === "english" && data.sub === "translate") ? "none" : "";
  $("m-cov").textContent = Math.round(data.coverage * 100) + "%";
  $("m-sim").textContent = Math.round(data.similarity * 100) + "%";
  $("m-len").textContent = origText.length + " → " + data.rewrite.length;
  $("result").hidden = false;
  renderText($("orig"), origText);
  renderText($("out"), data.rewrite, origText);
  renderDiag(data);
  if (data.quota) setQuota(data.quota);
}

// ── 报告降重：上传 → 解析预览 → 提交改写 → 结果渲染 ───────────────────────
let reportStructure = null;
let reportStrength = "medium";

$("report-strength").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  document.querySelectorAll("#report-strength button").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  reportStrength = b.dataset.v;
});

$("report-upload").addEventListener("click", () => $("report-file").click());
$("report-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  e.target.value = "";
  $("err").hidden = true;
  const btn = $("report-upload");
  btn.disabled = true; btn.textContent = "解析中…";
  busy = true; document.querySelector('.tabs').classList.add('locked');
  try {
    const fd = new FormData();
    fd.append("file", f);
    const r = await fetch("/api/report-parse", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) { showError(d.error || ("HTTP " + r.status)); return; }
    reportStructure = d.structure;
    const mins = Math.max(1, Math.ceil(d.red_count / 8 / 2 * 0.3));
    document.querySelector(".report-stats").innerHTML =
      `<span class="pill">报告来源：${d.brand_guess}</span>` +
      `<span class="pill">标红 <b>${d.red_count}</b> 句</span>` +
      `<span class="pill"><b>${d.red_chars}</b> 字需改写</span>` +
      `<span class="pill">全文 ${d.total_chars} 字</span>` +
      `<span class="pill">预计 ${mins} 分钟</span>`;
    $("red-sent-list").innerHTML = d.red_sents.map((s) => `
      <div class="match">
        <div class="match-sent">${s.text}</div>
        <div class="match-meta"><span class="match-ov err">${s.chars} 字</span> · 第 ${s.page || "?"} 页</div>
      </div>`).join("");
    $("report-go").textContent = `提交降重（${d.red_count} 句标红，预计 ${mins} 分钟）`;
    $("report-preview").hidden = false;
  } catch (err) {
    showError(err.message || String(err));
  } finally {
    busy = false; document.querySelector('.tabs').classList.remove('locked');
    btn.disabled = false; btn.textContent = "重新上传报告";
  }
});

$("report-go").addEventListener("click", async () => {
  if (!reportStructure) return;
  $("err").hidden = true;
  const btn = $("report-go");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "改写中…标红句逐句处理，请勿关闭页面";
  busy = true; document.querySelector('.tabs').classList.add('locked');
  const slowTimer = setTimeout(() => { btn.textContent = "改写中…AI 偶尔会慢，马上好"; }, 8000);
  try {
    const r = await fetch("/api/report-rewrite", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ structure: reportStructure, strength: reportStrength }),
    });
    const data = await r.json();
    if (!r.ok) {
      if (r.status === 402) handleQuotaError(data);
      else showError(data.error || ("HTTP " + r.status));
      return;
    }
    showReportResult(data);
  } catch (e) {
    showError(e.message || String(e));
  } finally {
    clearTimeout(slowTimer);
    busy = false; document.querySelector('.tabs').classList.remove('locked');
    btn.disabled = false; btn.textContent = label;
  }
});

function showReportResult(data) {
  const origJoined = (data.rewrites || []).map((r) => r.orig).join("\n");
  const newJoined = (data.rewrites || []).map((r) => r.new).join("\n");
  currentOutput = data.full_text;
  lastReport = { task: "report", orig_text: data.orig_text || "", result: data };
  document.querySelector(".metrics").style.display = "";
  $("m-cov").textContent = Math.round((data.coverage || 0) * 100) + "%";
  $("m-sim").textContent = Math.round((data.similarity || 0) * 100) + "%";
  $("m-len").textContent = `${data.red_chars || 0} 字标红 → 已改 ${((data.rewrites || []).length - (data.failed || []).length)} 句`;
  $("result").hidden = false;
  $("check-result").hidden = true;
  renderText($("orig"), origJoined);
  renderText($("out"), newJoined, origJoined);
  $("diag").hidden = true;
  $("report-full").textContent = data.full_text || "";
  $("report-full-wrap").hidden = !data.full_text;
  if (data.quota) setQuota(data.quota);
  if ((data.failed || []).length) {
    showError(`有 ${data.failed.length} 句未改成功（已保留原文），可点「提交降重」重试`);
  }
}

$("copy-full").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(currentOutput); flash($("copy-full"), "已复制"); }
  catch { flash($("copy-full"), "复制失败"); }
});

$("download-full").addEventListener("click", () => $("download").click());

// ── AI 写作：大纲 → 全文 + 一键降AI联动 ────────────────────────────────────
const WRITE_KIND_ZH = { review: "文献综述", research: "研究报告", course: "课程报告",
                        speech: "演讲稿", summary: "工作总结", general: "通用文章" };
let writeResult = null;

function writeFormPayload() {
  return {
    topic: $("write-topic").value.trim(),
    kind: $("write-kind").value,
    words: +$("write-words").value,
    discipline: $("write-discipline").value,
    notes: $("write-notes").value.trim(),
  };
}

$("write-outline-btn").addEventListener("click", async () => {
  const p = writeFormPayload();
  if (!p.topic) { showError("请先填写题目"); $("write-topic").focus(); return; }
  $("err").hidden = true;
  const btn = $("write-outline-btn");
  btn.disabled = true; btn.textContent = "大纲生成中…";
  busy = true; document.querySelector('.tabs').classList.add('locked');
  try {
    const r = await fetch("/api/write-outline", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p),
    });
    const d = await r.json();
    if (!r.ok) {
      if (r.status === 402) handleQuotaError(d);
      else showError(d.error || ("HTTP " + r.status));
      return;
    }
    $("write-outline").value = d.outline;
    const mins = Math.max(1, Math.ceil(d.sections * 8 / 3 / 10));
    $("write-go").textContent = `生成全文（${d.sections} 节，预计 ${mins} 分钟）`;
    $("write-outline-wrap").hidden = false;
  } catch (e) {
    showError(e.message || String(e));
  } finally {
    busy = false; document.querySelector('.tabs').classList.remove('locked');
    btn.disabled = false; btn.textContent = "重新生成大纲";
  }
});

$("write-go").addEventListener("click", async () => {
  const outline = $("write-outline").value.trim();
  if (!outline) { showError("请先生成或填写大纲"); return; }
  $("err").hidden = true;
  const btn = $("write-go");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "生成中…逐节写作约 1-2 分钟，请勿关闭页面";
  busy = true; document.querySelector('.tabs').classList.add('locked');
  const slowTimer = setTimeout(() => { btn.textContent = "生成中…AI 偶尔会慢，马上好"; }, 15000);
  try {
    const r = await fetch("/api/write-generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...writeFormPayload(), outline }),
    });
    const data = await r.json();
    if (!r.ok) {
      if (r.status === 402) handleQuotaError(data);
      else showError(data.error || ("HTTP " + r.status));
      return;
    }
    writeResult = data;
    showWriteResult(data);
  } catch (e) {
    showError(e.message || String(e));
  } finally {
    clearTimeout(slowTimer);
    busy = false; document.querySelector('.tabs').classList.remove('locked');
    btn.disabled = false; btn.textContent = label;
  }
});

function showWriteResult(data) {
  writeResult = data;
  currentOutput = data.full_text || "";
  lastReport = { task: "write", orig_text: data.topic || "", result: data };
  $("write-stats").innerHTML =
    `<span class="pill">类型：${WRITE_KIND_ZH[data.kind] || data.kind}</span>` +
    `<span class="pill">目标 <b>${data.target_words}</b> 字</span>` +
    `<span class="pill">实际 <b>${data.actual_words}</b> 字</span>` +
    `<span class="pill">${data.sections_count} 节</span>`;
  $("write-title").textContent = data.topic || "";
  const wrap = $("write-sections");
  wrap.innerHTML = "";
  (data.sections || []).forEach((s, i) => {
    const box = document.createElement("div"); box.className = "write-section";
    const h = document.createElement("h4"); h.textContent = `${i + 1}、${s.title}`;
    const body = document.createElement("div"); body.className = "text";
    (s.text || "").split(/\n\s*\n/).forEach(p => {
      if (!p.trim()) return;
      const el = document.createElement("p");
      // 逐句 span：行内改写入口
      splitSentences(p.trim()).forEach(sent => {
        const sp = document.createElement("span");
        sp.className = "sent-click";
        sp.textContent = sent;
        sp.dataset.orig = sent;  // 应用改写后按此替换数据层，链式追踪
        el.appendChild(sp);
      });
      body.appendChild(el);
    });
    box.append(h, body);
    wrap.appendChild(box);
  });
  $("write-result").hidden = false;
  $("result").hidden = true;
  $("check-result").hidden = true;
  if (data.quota) setQuota(data.quota);
}

// ── 配套生成：摘要/关键词/致谢/开题思路 ────────────────────────────────────
document.querySelectorAll(".write-part-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    if (!writeResult || busy) return;
    $("err").hidden = true;
    const part = btn.dataset.part;
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = "生成中…";
    try {
      const r = await fetch("/api/write-part", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ part, topic: writeResult.topic, text: currentOutput }),
      });
      const d = await r.json();
      if (!r.ok) {
        if (r.status === 402) handleQuotaError(d);
        else showError(d.error || ("HTTP " + r.status));
        return;
      }
      $("write-part-name").textContent = d.label;
      $("write-part-text").textContent = d.text;
      $("write-part-result").hidden = false;
    } catch (e) {
      showError(e.message || String(e));
    } finally {
      btn.disabled = false; btn.textContent = old;
    }
  });
});

$("write-part-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("write-part-text").textContent); flash($("write-part-copy"), "已复制"); }
  catch { flash($("write-part-copy"), "复制失败"); }
});

// ── 句级改写 popover：点句子 → 多候选 → 换一换/应用 ────────────────────────
const sentPop = { el: null, span: null, loading: false };
let sentReqSeq = 0;  // 请求令牌：换句/关弹层后让在飞响应作废，防迟到覆盖串句

function closeSentPop() { sentReqSeq++; $("sent-pop").hidden = true; sentPop.span = null; }

function _applyCandidate(text) {
  const span = sentPop.span;
  if (!span) return;
  const secBody = span.closest(".write-section");
  const orig = span.dataset.orig || span.textContent;
  span.textContent = text;
  span.dataset.orig = text;  // 改后这句成为下次的原句
  span.classList.add("applied");
  closeSentPop();
  if (!secBody || !writeResult) return;
  const secs = [...document.querySelectorAll("#write-sections .write-section")];
  const idx = secs.indexOf(secBody);
  if (idx < 0 || !writeResult.sections[idx]) return;
  // 数据层同步：在 section.text 里做原句→新句替换（保段落结构），不依赖 innerText 反推
  const sec = writeResult.sections[idx];
  if (orig && sec.text.includes(orig)) {
    sec.text = sec.text.replace(orig, text);
  } else {
    sec.text = secBody.querySelector(".text").innerText.trim();  // 兜底
  }
  sec.words = sec.text.replace(/\s/g, "").length;
  writeResult.full_text = writeResult.sections.map(s => s.text).join("\n\n");
  writeResult.actual_words = writeResult.full_text.replace(/\s/g, "").length;
  currentOutput = writeResult.full_text;
  $("write-stats").innerHTML =
    `<span class="pill">类型：${WRITE_KIND_ZH[writeResult.kind] || writeResult.kind}</span>` +
    `<span class="pill">目标 <b>${writeResult.target_words}</b> 字</span>` +
    `<span class="pill">实际 <b>${writeResult.actual_words}</b> 字</span>` +
    `<span class="pill">${writeResult.sections_count} 节</span>`;
  lastReport = { task: "write", orig_text: writeResult.topic, result: writeResult };
}

async function _loadCandidates(sentence) {
  const my = ++sentReqSeq;
  const list = $("sent-pop-list");
  list.innerHTML = '<div class="sent-pop-loading">生成中…（约 3-8 秒）</div>';
  try {
    const r = await fetch("/api/sentence-rewrite", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sentence }),
    });
    const d = await r.json();
    if (my !== sentReqSeq) return;  // 迟到响应：用户已换句/关弹层，丢弃
    if (!r.ok) {
      if (r.status === 402) handleQuotaError(d);
      else list.innerHTML = `<div class="sent-pop-err">${d.error || "生成失败，请重试"}</div>`;
      return;
    }
    if (d.quota) setQuota(d.quota);
    list.innerHTML = "";
    d.candidates.forEach((c) => {
      const item = document.createElement("div");
      item.className = "sent-pop-item";
      const txt = document.createElement("div");
      txt.className = "sent-pop-txt"; txt.textContent = c.new;
      const meta = document.createElement("div");
      meta.className = "sent-pop-meta";
      meta.innerHTML = `改写幅度 <b>${Math.max(0, 100 - Math.round(c.sim * 100))}%</b>`;
      const use = document.createElement("button");
      use.type = "button"; use.className = "primary mini"; use.textContent = "应用";
      use.addEventListener("click", () => _applyCandidate(c.new));
      item.append(txt, meta, use);
      list.appendChild(item);
    });
  } catch (e) {
    if (my === sentReqSeq) list.innerHTML = `<div class="sent-pop-err">${e.message || String(e)}</div>`;
  }
}

document.addEventListener("click", (e) => {
  const sp = e.target.closest(".sent-click");
  if (!sp) {
    if (!e.target.closest("#sent-pop")) closeSentPop();
    return;
  }
  if (!writeResult || busy) return;
  if (sentPop.span === sp) { closeSentPop(); return; }
  sentReqSeq++;  // 换锚：作废上一个在飞请求
  sentPop.span = sp;
  const pop = $("sent-pop");
  const rect = sp.getBoundingClientRect();
  pop.hidden = false;
  const pw = Math.min(560, window.innerWidth - 24);
  pop.style.width = pw + "px";
  const popH = pop.offsetHeight;
  let left = Math.max(12, Math.min(rect.left + window.scrollX, window.scrollX + window.innerWidth - pw - 12));
  let top = rect.bottom + window.scrollY + 8;
  if (top + popH + 12 > window.scrollY + document.documentElement.clientHeight) {
    top = Math.max(window.scrollY + 8, rect.top + window.scrollY - popH - 8);  // 下放不下→上翻
  }
  pop.style.left = left + "px";
  pop.style.top = top + "px";
  _loadCandidates(sp.dataset.orig || sp.textContent);
});

$("sent-pop-close").addEventListener("click", closeSentPop);
$("sent-pop-refresh").addEventListener("click", () => {
  if (sentPop.span) _loadCandidates(sentPop.span.textContent, true);
});

$("write-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(currentOutput); flash($("write-copy"), "已复制"); }
  catch { flash($("write-copy"), "复制失败"); }
});

$("write-dl-docx").addEventListener("click", async () => {
  if (!writeResult) return;
  const t = writeResult;
  const text = t.topic + "\n\n" +
    t.sections.map((s, i) => `${i + 1}、${s.title}\n\n${s.text}`).join("\n\n");
  try {
    const r = await fetch("/api/make-docx", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) { const d = await r.json(); showError(d.error || ("HTTP " + r.status)); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = (t.topic.replace(/[\\/:*?"<>|]/g, "").slice(0, 20) || "AI写作") + ".docx";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    showError("下载失败：" + (e.message || e));
  }
});

$("write-dl-report").addEventListener("click", (e) => {
  if (lastReport) downloadReportPayload(lastReport, e.currentTarget);
});

$("write-humanize").addEventListener("click", async () => {
  if (!writeResult || !currentOutput || busy) return;
  $("err").hidden = true;
  const btn = $("write-humanize");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "降AI处理中…全文改写约 30-60 秒";
  busy = true; document.querySelector('.tabs').classList.add('locked');
  try {
    const r = await fetch("/api/humanize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: currentOutput, strength: "medium" }),
    });
    const data = await r.json();
    if (!r.ok) {
      if (r.status === 402) handleQuotaError(data);
      else showError(data.error || ("HTTP " + r.status));
      return;
    }
    $("write-result").hidden = true;
    showResult(data, writeResult.full_text, "humanize");
  } catch (e) {
    showError(e.message || String(e));
  } finally {
    busy = false; document.querySelector('.tabs').classList.remove('locked');
    btn.disabled = false; btn.textContent = label;
  }
});

// ── 降重历史 ──────────────────────────────────────────────────────────────
async function loadHistory() {
  const tbody = $("history-list");
  tbody.innerHTML = '<tr><td colspan="4" class="history-empty">加载中…</td></tr>';
  try {
    const r = await fetch("/api/history");
    const d = await r.json();
    if (!r.ok) { tbody.innerHTML = '<tr><td colspan="4" class="history-empty">加载失败</td></tr>'; return; }
    if (!(d.items || []).length) {
      tbody.innerHTML = '<tr><td colspan="4" class="history-empty">暂无记录，先去跑一个任务吧</td></tr>';
      return;
    }
    tbody.innerHTML = d.items.map((it) => `
      <tr>
        <td class="history-title" title="${it.title}">${it.title}</td>
        <td><span class="pill">${TASK_LABELS[it.task] || it.task}</span></td>
        <td class="history-time">${new Date(it.ts * 1000).toLocaleString("zh-CN", { hour12: false })}</td>
        <td class="history-ops">
          <button type="button" class="ghost mini" onclick="viewHistory('${it.id}')">查看</button>
          <button type="button" class="ghost mini" onclick="downloadHistory('${it.id}')">下载报告</button>
          <button type="button" class="ghost mini" onclick="deleteHistory('${it.id}')">删除</button>
        </td>
      </tr>`).join("");
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="4" class="history-empty">加载失败</td></tr>';
  }
}
$("history-refresh").addEventListener("click", loadHistory);

async function viewHistory(id) {
  try {
    const r = await fetch(`/api/history/${id}`);
    const rec = await r.json();
    if (!r.ok) { showError(rec.error || "记录不存在"); return; }
    const tabBtn = document.querySelector(`.tab[data-task="${rec.task}"]`);
    if (tabBtn && busy === false) tabBtn.click();
    if (rec.task !== "write") $("input").value = rec.orig_text || "";
    if (rec.task === "aigc") showAigcReport(rec.result);
    else if (rec.task === "plagiarism") showPlagiarismReport(rec.result);
    else if (rec.task === "report") showReportResult(rec.result);
    else if (rec.task === "write") showWriteResult(rec.result);
    else showResult(rec.result, rec.orig_text || "");
  } catch (e) {
    showError(e.message || String(e));
  }
}

async function downloadHistory(id) {
  try {
    const r = await fetch(`/api/history/${id}`);
    const rec = await r.json();
    if (!r.ok) { showError(rec.error || "记录不存在"); return; }
    await downloadReportPayload({ task: rec.task, orig_text: rec.orig_text || "", result: rec.result }, null);
  } catch (e) {
    showError(e.message || String(e));
  }
}

async function deleteHistory(id) {
  if (!confirm("确定删除这条记录？")) return;
  try {
    const r = await fetch(`/api/history/${id}`, { method: "DELETE" });
    if (!r.ok) { const d = await r.json(); showError(d.error || "删除失败"); return; }
    loadHistory();
  } catch (e) {
    showError(e.message || String(e));
  }
}

function showAigcReport(data) {
  const score = data.aigc_score;
  const color = data.color || "";
  lastReport = { task: "aigc", orig_text: $("input").value.trim(), result: data };
  $("aigc-score").textContent = score + "%";
  $("aigc-score").className = "gauge-num " + color;
  $("aigc-bar").style.width = score + "%";
  $("aigc-bar").className = color;
  $("aigc-verdict").textContent = data.verdict;
  $("aigc-verdict").className = "aigc-verdict " + color;
  $("aigc-signals").innerHTML = data.signals.map((s) => `
    <div class="signal">
      <div class="signal-head"><span class="signal-name">${s.name}</span><span class="signal-val">${s.value}</span></div>
      <div class="signal-bar"><span style="width:${s.score}%"></span></div>
      <div class="signal-hint">${s.hint}</div>
    </div>`).join("");
  $("aigc-note").textContent = data.note;
  document.querySelector(".aigc-report").hidden = false;
  document.querySelector(".plag-report").hidden = true;
  $("check-result").hidden = false;
  $("result").hidden = true;
  if (data.quota) setQuota(data.quota);
}

function showPlagiarismReport(data) {
  const score = data.similarity_score;
  const color = data.color || "";
  lastReport = { task: "plagiarism", orig_text: $("input").value.trim(), result: data };
  $("plag-score").textContent = score + "%";
  $("plag-score").className = "plag-score " + color;
  $("plag-bar").style.width = score + "%";
  $("plag-bar").className = color;
  $("plag-verdict").textContent = data.verdict;
  $("plag-verdict").className = "plag-verdict " + color;
  $("plag-meta").textContent = `检查了 ${data.checked_count} 个句子，发现 ${data.matched_count} 处网络雷同`;
  $("plag-matches").innerHTML = (data.matches || []).length
    ? data.matches.map((m) => `
        <div class="match">
          <div class="match-sent">${m.sentence}</div>
          <div class="match-meta"><span class="match-ov ${m.overlap >= 70 ? "err" : m.overlap >= 55 ? "warn" : "ok"}">${m.overlap}% 雷同</span> · <a href="${m.url}" target="_blank" rel="noopener">${m.title || m.url}</a></div>
        </div>`).join("")
    : '<div class="match-empty">未发现明显网络雷同 ✓</div>';
  $("plag-note").textContent = data.note;
  document.querySelector(".aigc-report").hidden = true;
  document.querySelector(".plag-report").hidden = false;
  $("check-result").hidden = false;
  $("result").hidden = true;
  if (data.quota) setQuota(data.quota);
}

function handleQuotaError(data) {
  // 402: not active / expired
  if (data && data.quota) setQuota(data.quota);
  showError((data && data.error) || "未激活，请输入兑换码");
  $("redeem-code").focus();
}

// ── upload ───────────────────────────────────────────────────────────────
$("upload").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  $("err").hidden = true;
  const btn = $("go");
  const goLabel = task === "humanize" ? "降 AIGC" : (task === "english" ? "英文修改" : "改写");
  btn.disabled = true; btn.textContent = "上传处理中…";
  busy = true; document.querySelector('.tabs').classList.add('locked');
  try {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("strength", strength);
    fd.append("task", task);
    fd.append("mode", $("pipe").checked ? "pipeline" : "simple");
    fd.append("discipline", $("discipline").value);
    fd.append("sub", englishSub);
    const r = await fetch("/api/rewrite-file", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) {
      if (r.status === 402) handleQuotaError(data);
      else showError(data.error || ("HTTP " + r.status));
      return;
    }
    showResult(data, data.orig_text || "");
  } catch (e) {
    showError(e.message || String(e));
  } finally {
    busy = false; document.querySelector('.tabs').classList.remove('locked');
    btn.disabled = false; btn.textContent = goLabel;
    e.target.value = "";
  }
});

// ── download ─────────────────────────────────────────────────────────────
$("dl-report").addEventListener("click", downloadReport);
$("dl-report-aigc").addEventListener("click", downloadReport);
$("dl-report-plag").addEventListener("click", downloadReport);

$("download").addEventListener("click", async () => {
  if (!currentOutput) return;
  try {
    const r = await fetch("/api/make-docx", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: currentOutput }),
    });
    if (!r.ok) { const d = await r.json(); showError(d.error || ("HTTP " + r.status)); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "rewrite.docx";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    showError("下载失败：" + (e.message || e));
  }
});

// ── rewrite (paste) ──────────────────────────────────────────────────────
$("go").addEventListener("click", async () => {
  const text = $("input").value.trim();
  $("err").hidden = true;
  if (text.length < 10) { showError("请至少输入 10 个字"); return; }

  const btn = $("go");
  const goLabel = TASK_LABELS[task] || "改写";
  let endpoint, payload, isCheck = false;
  if (task === "humanize") {
    endpoint = "/api/humanize"; payload = { text, strength };
  } else if (task === "english") {
    endpoint = "/api/edit-english"; payload = { text, strength, sub: englishSub };
  } else if (task === "aigc") {
    endpoint = "/api/aigc-check"; payload = { text }; isCheck = true;
  } else if (task === "plagiarism") {
    endpoint = "/api/plagiarism-check"; payload = { text }; isCheck = true;
  } else {
    endpoint = "/api/rewrite";
    payload = { text, strength, mode: $("pipe").checked ? "pipeline" : "simple", discipline: $("discipline").value };
  }
  btn.disabled = true;
  btn.textContent = goLabel + "中…" + (text.length > 10000 ? "（长文约 15-30 秒）" : "");
  busy = true; document.querySelector('.tabs').classList.add('locked');
  const slowTimer = setTimeout(() => { btn.textContent = goLabel + "中… AI 偶尔会慢，马上好"; }, 6000);
  try {
    const r = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) {
      if (r.status === 402) handleQuotaError(data);
      else showError(data.error || ("HTTP " + r.status));
      return;
    }
    if (task === "plagiarism") showPlagiarismReport(data);
    else if (isCheck) showAigcReport(data);
    else showResult(data, text);
  } catch (e) {
    showError(e.message || String(e));
  } finally {
    clearTimeout(slowTimer);
    busy = false; document.querySelector('.tabs').classList.remove('locked');
    btn.disabled = false;
    btn.textContent = goLabel;
  }
});

function showError(msg) {
  if (/Failed to fetch|NetworkError|load failed/i.test(msg || "")) {
    msg = "网络连接失败，请重试；仍不行就关掉「质量管线」开关用单模型（更快更稳）。";
  }
  const el = $("err");
  el.textContent = "⚠ " + msg;
  el.hidden = false;
}
