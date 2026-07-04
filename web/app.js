const $ = (id) => document.getElementById(id);

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function loadKbs() {
  const kbs = await api("/api/kb");
  $("kbSelect").innerHTML = kbs.map(k => `<option value="${k.id}">${k.name}</option>`).join("");
  if (kbs.length) loadDocs();
}

async function loadProviders() {
  const p = await api("/api/providers");
  $("providerSelect").innerHTML = p.providers.map(
    n => `<option ${n === p.active ? "selected" : ""}>${n}</option>`).join("");
}

async function loadDocs() {
  const kb = $("kbSelect").value;
  if (!kb) return;
  const docs = await api(`/api/kb/${kb}/documents`);
  $("docList").innerHTML = docs.map(d =>
    `<div class="doc"><span title="${esc(d.error || "")}">${esc(d.filename)}</span>
     <span class="status-${esc(d.status)}">${esc(d.status)}</span></div>`).join("");
}

$("createKb").onclick = async () => {
  const name = $("newKbName").value.trim();
  if (!name) return;
  await api("/api/kb", { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify({ name }) });
  $("newKbName").value = "";
  await loadKbs();
};

$("uploadBtn").onclick = async () => {
  const kb = $("kbSelect").value;
  const fd = new FormData();
  for (const f of $("fileInput").files) fd.append("files", f);
  await api(`/api/kb/${kb}/documents`, { method: "POST", body: fd });
  setTimeout(loadDocs, 1000);   // 解析是后台任务，稍后刷新
};

$("refreshDocs").onclick = loadDocs;
$("kbSelect").onchange = loadDocs;

function addMsg(cls, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.textContent = text;
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
  return div;
}

function addCitations(cits) {
  for (const c of cits) {
    const div = document.createElement("div");
    div.className = "cite";
    div.innerHTML = `[${esc(c.index)}] ${esc(c.heading_path)}（相关度 ${esc(c.score)}）
      <div class="snippet">${esc(c.snippet)}</div>`;
    div.onclick = () => div.classList.toggle("open");
    $("chat").appendChild(div);
  }
}

let streaming = false;

async function send() {
  if (streaming) return;   // 防止流式过程中重复提交
  const q = $("question").value.trim();
  const kb = $("kbSelect").value;
  if (!q || !kb) return;
  $("question").value = "";
  addMsg("user", q);
  const bot = addMsg("bot", "");

  streaming = true;
  $("sendBtn").disabled = true;
  let gotDone = false;
  try {
    const resp = await fetch(`/api/kb/${kb}/query`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, provider: $("providerSelect").value }),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "", event = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      // sse-starlette 用 \r\n 换行；统一按 \r?\n 切分，避免 \r 残留污染 data/event 内容
      const lines = buf.split(/\r?\n/);
      buf = lines.pop();
      for (const line of lines) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) {
          const data = line.slice(5).replace(/^ /, "");
          if (event === "token") bot.textContent += data;
          else if (event === "citations") addCitations(JSON.parse(data));
          else if (event === "done") gotDone = true;
        }
        $("chat").scrollTop = $("chat").scrollHeight;
      }
    }
  } finally {
    if (!gotDone) bot.textContent += "\n⚠️ 回答中断，请重试";
    streaming = false;
    $("sendBtn").disabled = false;
    $("chat").scrollTop = $("chat").scrollHeight;
  }
}

$("sendBtn").onclick = send;
$("question").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });

loadKbs();
loadProviders();
