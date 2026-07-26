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

let task = "rewrite";
document.querySelector(".tabs").addEventListener("click", (e) => {
  const t = e.target.closest(".tab");
  if (!t || t.disabled) return;
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  task = t.dataset.task;
  const isRewrite = task === "rewrite";
  document.querySelector(".pipe-toggle").style.display = isRewrite ? "" : "none";
  document.querySelector(".field").style.display = isRewrite ? "" : "none";
  $("go").textContent = task === "humanize" ? "降 AIGC" : (task === "english" ? "英文修改" : "改写");
  $("result").hidden = true;
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

// ── quota（时间制：active + 剩余秒数）────────────────────────────────────
function fmtDuration(sec) {
  if (sec <= 0) return "已到期";
  const h = Math.floor(sec / 3600), m = Math.ceil((sec % 3600) / 60);
  return h > 0 ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}
function setQuota(summary) {
  if (!summary) return;
  const chip = document.querySelector(".quota-chip");
  chip.innerHTML = summary.active
    ? `剩余 <b>${fmtDuration(summary.remaining_seconds)}</b>`
    : `<b>未激活</b> · 需兑换码`;
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
  for (const s of splitSentences(text)) {
    const span = document.createElement("span");
    span.className = "sentence";
    span.textContent = s;
    const best = origSents.length ? Math.max(...origSents.map((o) => sim(s, o))) : 0;
    if (best < 0.55) span.classList.add("hot");
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
  $("m-cov").textContent = Math.round(data.coverage * 100) + "%";
  $("m-sim").textContent = Math.round(data.similarity * 100) + "%";
  $("m-len").textContent = origText.length + " → " + data.rewrite.length;
  $("result").hidden = false;
  renderText($("orig"), origText);
  renderText($("out"), data.rewrite, origText);
  renderDiag(data);
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
  try {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("strength", strength);
    fd.append("task", task);
    fd.append("mode", $("pipe").checked ? "pipeline" : "simple");
    fd.append("discipline", $("discipline").value);
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
    btn.disabled = false; btn.textContent = goLabel;
    e.target.value = "";
  }
});

// ── download ─────────────────────────────────────────────────────────────
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
  const goLabel = task === "humanize" ? "降 AIGC" : (task === "english" ? "英文修改" : "改写");
  let endpoint, payload;
  if (task === "humanize") {
    endpoint = "/api/humanize"; payload = { text, strength };
  } else if (task === "english") {
    endpoint = "/api/edit-english"; payload = { text, strength };
  } else {
    endpoint = "/api/rewrite";
    payload = { text, strength, mode: $("pipe").checked ? "pipeline" : "simple", discipline: $("discipline").value };
  }
  btn.disabled = true;
  btn.textContent = goLabel + "中…";
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
    showResult(data, text);
  } catch (e) {
    showError(e.message || String(e));
  } finally {
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
