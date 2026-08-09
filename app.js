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
const TASK_LABELS = { rewrite: "改写", humanize: "降 AIGC", english: "英文修改", aigc: "查 AIGC 率", plagiarism: "查重" };
document.querySelector(".tabs").addEventListener("click", (e) => {
  const t = e.target.closest(".tab");
  if (!t || t.disabled) return;
  if (busy) return;  // 操作进行中不允许切 tab，避免按钮状态/结果串台
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  task = t.dataset.task;
  const isRewrite = task === "rewrite";
  const isCheck = task === "aigc" || task === "plagiarism";
  document.querySelector(".pipe-toggle").style.display = isRewrite ? "" : "none";
  document.querySelector(".field").style.display = isRewrite ? "" : "none";
  document.getElementById("strength").style.display = isCheck ? "none" : "";
  document.getElementById("upload").style.display = isCheck ? "none" : "";
  document.getElementById("english-mode").style.display = (task === "english") ? "" : "none";
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
  aigc: "AIGC检测报告.docx", plagiarism: "查重报告.docx",
};

async function downloadReport() {
  if (!lastReport) return;
  const btn = $("dl-report") || $("dl-report-aigc") || $("dl-report-plag");
  try {
    const r = await fetch("/api/make-report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lastReport),
    });
    if (r.status === 402) { const d = await r.json(); handleQuotaError(d); return; }
    if (!r.ok) { let m = "HTTP " + r.status; try { const d = await r.json(); m = d.error || m; } catch {} showError(m); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const cd = r.headers.get("Content-Disposition") || "";
    let dlName = REPORT_NAMES[lastReport.task] || "报告.docx";
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

function showResult(data, origText) {
  currentOutput = data.rewrite;
  lastReport = { task, orig_text: origText, result: data };
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
