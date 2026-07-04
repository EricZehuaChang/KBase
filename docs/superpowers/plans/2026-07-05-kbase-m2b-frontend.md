# KBase M2 Plan B（设计感前端）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vue3 + shadcn-vue 四页前端（问答/知识库/检索分析/设置），设计令牌体系（亮/暗），替换 M1 零构建页面。

**Architecture:** 独立 Vite 项目 `web-app/`，构建产物输出 `web/`（FastAPI 静态托管不变）。无状态管理库（组合式函数够用，YAGNI）；SSE 解析为纯函数模块（vitest 可测）；shadcn-vue 组件按需复制自持有；设计令牌全 CSS 变量，暗色 = `[data-theme="dark"]` 覆盖。

**Tech Stack:** Vue 3.5 + TypeScript + Vite 6 + Tailwind CSS v4（@tailwindcss/vite）+ shadcn-vue（reka-ui 基座）+ vue-router + vitest。

**Spec:** `docs/superpowers/specs/2026-07-05-kbase-m2-design.md` §3
**基线：** main @ 6126227（后端 Plan A 全部落地，82 passed）。执行分支 `feature/m2-frontend`。
**后端接口契约（Plan A 实际落地）：** `/api/kb`(GET/POST)、`/api/kb/{id}/documents`(GET/POST/DELETE {doc})、`/api/documents/{id}/content|retry`、`/api/kb/{id}/retry-ocr`、`/api/kb/{id}/search{,?debug}`、`/api/conversations`(POST/GET) `/{id}/messages|query`、`/api/settings/providers`(CRUD+test)、`/api/settings/active-provider`、`/api/providers`、`/healthz`。SSE 事件：`citations`→`token`*→`done`。

**约定：** 工作目录 `D:\Claude Code\RAG`；npm 命令在 `web-app/` 下执行；若 npm 安装超时用 `--registry https://registry.npmmirror.com`；前端测试 `npm run test`（vitest run）；后端全量回归保持 82 passed, 3 deselected 不动。

---

## 文件结构（web-app/ 内）

```
web-app/
├── package.json / vite.config.ts / tsconfig.json / components.json
├── index.html
├── src/
│   ├── main.ts / App.vue / router.ts
│   ├── styles/tokens.css          # 设计令牌（亮/暗）
│   ├── styles/main.css            # tailwind 入口 + 基础样式
│   ├── lib/api.ts                 # 全端点 typed 客户端
│   ├── lib/sse.ts                 # SSE 纯函数解析器（accumulate-flush）
│   ├── lib/theme.ts               # 主题切换与持久化
│   ├── components/ui/…            # shadcn-vue 复制件
│   ├── components/AppShell.vue    # 侧栏+主区布局
│   ├── components/MessageStream.vue
│   ├── components/CitationDrawer.vue
│   ├── components/UploadZone.vue
│   ├── components/RetrievalTrace.vue
│   └── views/ChatView.vue / KbView.vue / AnalysisView.vue / SettingsView.vue
└── src/lib/__tests__/sse.test.ts
```

---

### Task B1: 脚手架、设计令牌与布局骨架

**Files:** Create 整个 `web-app/` 脚手架；`src/styles/tokens.css`、`src/router.ts`、`src/components/AppShell.vue`、四个空壳 View。

- [ ] **Step 1: 脚手架命令**（在 `D:\Claude Code\RAG` 下）

```powershell
npm create vite@latest web-app -- --template vue-ts
cd web-app
npm install
npm install vue-router@4 tailwindcss @tailwindcss/vite
npm install -D vitest @vue/test-utils jsdom
npx shadcn-vue@latest init   # style: default; base color: neutral; css vars: yes
npx shadcn-vue@latest add button input select dialog dropdown-menu tabs table badge switch tooltip sonner
```

（sonner 为 toast 组件。init 交互答案如上；若 CLI 版本交互项不同，按语义选择并在报告注明。）

- [ ] **Step 2: vite.config.ts**

```typescript
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: { outDir: "../web", emptyOutDir: true },
  server: { proxy: { "/api": "http://localhost:8100", "/healthz": "http://localhost:8100" } },
  test: { environment: "jsdom" },
});
```

- [ ] **Step 3: src/styles/tokens.css（设计令牌，完整写入）**

```css
:root {
  /* 中性暖灰骨架 */
  --bg: #FAFAF9;
  --surface: #FFFFFF;
  --surface-2: #F4F4F2;
  --border: #E5E4E0;
  --border-strong: #D0CFC9;
  --text: #1F1F1E;
  --text-2: #5F5E5A;
  --text-3: #8E8D87;
  /* 单强调色（可主题化：行业主题只覆盖这组） */
  --accent: #534AB7;
  --accent-weak: #EEEDFE;
  --accent-text: #3C3489;
  /* 语义色 */
  --ok: #1D9E75;  --ok-weak: #E1F5EE;
  --warn: #BA7517; --warn-weak: #FAEEDA;
  --err: #A32D2D;  --err-weak: #FCEBEB;
  /* 形状与节奏 */
  --radius-ctl: 8px;
  --radius-card: 12px;
  --shadow-drawer: 0 8px 32px rgba(0,0,0,.12);
}
[data-theme="dark"] {
  --bg: #141416;
  --surface: #1C1C1F;
  --surface-2: #232327;
  --border: #2E2E33;
  --border-strong: #3D3D44;
  --text: #ECECEA;
  --text-2: #A5A49E;
  --text-3: #6F6E68;
  --accent: #8B83E0;
  --accent-weak: #2A2650;
  --accent-text: #CECBF6;
  --ok: #5DCAA5;  --ok-weak: #0E3A2D;
  --warn: #EF9F27; --warn-weak: #3D2B0C;
  --err: #F09595;  --err-weak: #451414;
}
body { background: var(--bg); color: var(--text); font-family: system-ui, "Microsoft YaHei", sans-serif; font-size: 15px; line-height: 1.7; }
```

shadcn-vue 生成的 css 变量（--background/--primary 等）在 main.css 中映射到上述令牌（`--primary: var(--accent)` 等），保证复制组件吃同一套令牌。

- [ ] **Step 4: router.ts + AppShell.vue + 四个空壳 View**

router：`/`→ChatView、`/kb`→KbView、`/analysis`→AnalysisView、`/settings`→SettingsView。AppShell：左侧 208px 固定侧栏（顶部 logo「KBase」+ 中部插槽（问答页放会话列表）+ 底部导航四项 [message-circle/folder/scan-search/settings 图标，lucide-vue-next 随 shadcn 已装]），右侧 `<router-view>`。激活项样式 `--accent-weak` 底。暗色切换按钮在侧栏底部（theme.ts：localStorage 持久化 + `document.documentElement.dataset.theme`）。

- [ ] **Step 5: 构建验证**

`npm run build` → 产物落 `../web`（确认 index.html + assets/）。`.gitignore` 追加 `web-app/node_modules/`。**注意：`web/` 此刻被构建产物覆盖——M1 旧页面就此替换**，本任务后端不动，`git status` 应只见 web-app/ 新增与 web/ 变更。后端回归：`.venv\Scripts\python -m pytest` → 82 passed, 3 deselected。

- [ ] **Step 6: Commit** `feat(web): Vite+Vue3+shadcn-vue 脚手架与设计令牌`

---

### Task B2: API 客户端与 SSE 解析器（TDD）

**Files:** Create `src/lib/api.ts`、`src/lib/sse.ts`、`src/lib/__tests__/sse.test.ts`

- [ ] **Step 1: 失败测试（vitest）**

```typescript
// src/lib/__tests__/sse.test.ts
import { describe, expect, it } from "vitest";
import { parseSSE } from "../sse";

function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) { chunks.forEach(x => c.enqueue(enc.encode(x))); c.close(); },
  });
}

async function collect(stream: ReadableStream<Uint8Array>) {
  const events: { event: string; data: string }[] = [];
  const done = await parseSSE(stream.getReader(), (e, d) => events.push({ event: e, data: d }));
  return { events, done };
}

describe("parseSSE", () => {
  it("多 data 行按 SSE 规范以换行拼接", async () => {
    const { events } = await collect(streamOf(
      "event: token\r\ndata: 第一行\r\ndata: 第二行\r\n\r\n"));
    expect(events[0]).toEqual({ event: "token", data: "第一行\n第二行" });
  });
  it("跨 chunk 撕裂帧可重组（含多字节汉字截断）", async () => {
    const enc = new TextEncoder();
    const bytes = enc.encode("event: token\r\ndata: 汉字流\r\n\r\n");
    const a = bytes.slice(0, 17), b = bytes.slice(17);
    const stream = new ReadableStream<Uint8Array>({
      start(c) { c.enqueue(a); c.enqueue(b); c.close(); },
    });
    const { events } = await collect(stream);
    expect(events[0].data).toBe("汉字流");
  });
  it("done 事件返回 true；无 done 中断返回 false", async () => {
    const ok = await collect(streamOf("event: done\r\ndata: \r\n\r\n"));
    expect(ok.done).toBe(true);
    const cut = await collect(streamOf("event: token\r\ndata: 半截"));
    expect(cut.done).toBe(false);
    expect(cut.events).toEqual([{ event: "token", data: "半截" }]);  // 尾部无空行也 flush
  });
  it("citations JSON 单行完整解析", async () => {
    const { events } = await collect(streamOf(
      'event: citations\r\ndata: [{"index":1}]\r\n\r\n'));
    expect(JSON.parse(events[0].data)).toEqual([{ index: 1 }]);
  });
});
```

- [ ] **Step 2: 运行失败** `npm run test` → 找不到模块

- [ ] **Step 3: 实现 sse.ts（移植 M1 accumulate-flush 逻辑）**

```typescript
// src/lib/sse.ts —— SSE 纯函数解析器。事件内多 data 行以 \n 连接（SSE 规范）；
// 返回 Promise<boolean>：是否收到 done 事件（false = 流中断，调用方展示警示）。
export type SSEHandler = (event: string, data: string) => void;

export async function parseSSE(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: SSEHandler,
): Promise<boolean> {
  const dec = new TextDecoder();
  let buf = "", event = "", dataLines: string[] = [], gotDone = false;
  const flush = () => {
    if (!event && dataLines.length === 0) return;
    if (event === "done") gotDone = true;
    else if (event) onEvent(event, dataLines.join("\n"));
    event = ""; dataLines = [];
  };
  const handleLine = (line: string) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    else if (line === "") flush();
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split(/\r?\n/);
    buf = lines.pop() ?? "";
    lines.forEach(handleLine);
  }
  if (buf) handleLine(buf.replace(/\r$/, ""));
  flush();
  return gotDone;
}
```

- [ ] **Step 4: api.ts（typed 客户端，完整端点）**

`api.ts` 定义接口类型（KB/Doc/Conversation/Message/Citation/Provider/TraceStage）与方法：`listKbs/createKb/listDocs/uploadDocs(FormData)/deleteDoc/retryDoc/retryOcr/getDocContent/search(query,{topK,debug})/listConvs/createConv/listMessages/queryConv(convId,body)→Response(原始，供 SSE)/queryKb 同型/listProviders/settingsProviders CRUD/testProvider/setActive/healthz`。统一 `async function req<T>(path, init?): Promise<T>`：非 2xx 时 `throw new Error(详情.detail ?? text)`。（此文件为声明式代码，测试由使用它的组件测试间接覆盖，不单测。）

- [ ] **Step 5: 测试通过** `npm run test` → 4 passed；`npm run build` 无 TS 错误
- [ ] **Step 6: Commit** `feat(web): typed API 客户端与 SSE 解析器（规范拼接+撕裂帧重组）`

---

### Task B3: 问答页

**Files:** Create `src/components/MessageStream.vue`、`src/components/CitationDrawer.vue`、`src/views/ChatView.vue`（替换空壳）；AppShell 侧栏插槽接会话列表。

- [ ] **Step 1: 组件契约**

- **ChatView**：顶栏（KB 下拉 + Provider 下拉，来源 `listKbs`/`listProviders`）；中部消息区；底部输入条（textarea 单行自增 + 发送按钮，Enter 发送 Shift+Enter 换行）。侧栏（AppShell 插槽）：「+ 新会话」按钮 + 会话列表（`listConvs(kb)` 按 updated_at 分组：今天/7 天内/更早），点击切换加载 `listMessages`。
- **MessageStream**：props `messages`（含流式中的最后一条），用户消息右对齐浅底圆角块；助手消息直接排版（无气泡），流式中显示「思考中…」占位直到首 token；引用角标 `[n]` 渲染为可点击 accent 小圆片（正则替换 `\[(\d+)\]`），点击 emit `openCitation(n)`；中断（done=false）在尾部追加 `⚠️ 回答中断，请重试`；答案下操作条：引用数徽章、复制按钮（clipboard API + sonner toast）、「检索过程」链接（跳 /analysis 并预填问题）。
- **CitationDrawer**：右侧滑出 380px（shadcn Sheet 或自研 fixed 面板），显示当前引用的 doc_name/heading_path/snippet（snippet 高亮：`<mark>`包裹）、相关度分数、「查看文档全文」→ `getDocContent` 弹 Dialog 全文滚动区并锚定 heading_path 首次出现处高亮。

- [ ] **Step 2: 发送流程（组合函数 useChat）**

`send(question)`：无会话则 `createConv(kb)`；push 用户消息与空助手消息；`queryConv` 拿 Response → `parseSSE(reader, handler)`——citations 事件存入当前助手消息（含 index），token 追加正文；返回 false 时标记中断。**发送期间禁发**（streaming ref）。失败（HTTP 非 2xx）把错误文本写入助手消息（`⚠️ ` 前缀）。

- [ ] **Step 3: vitest 组件测试（2 个关键逻辑）**

`MessageStream` 的角标替换纯函数 `renderWithChips(text)` 抽出为可测导出：断言 `"见[1]与[2]"` 产出两个 chip 占位且文本完整；`useChat` 的中断路径：喂 mock reader（无 done）→ 最后一条消息含 `回答中断`。

- [ ] **Step 4: 手动核验** dev server（`npm run dev`，后端 8100 起）预览问答全流程可用
- [ ] **Step 5: Commit** `feat(web): 问答页（会话侧栏+流式渲染+引用抽屉+全文预览）`

---

### Task B4: 知识库管理页

**Files:** Create `src/components/UploadZone.vue`、`src/views/KbView.vue`

- KB 卡片网格（名称、文档数——由 listDocs 长度补充；「+ 新建知识库」卡片打开 Dialog 输入名称）；点卡片进入该库详情（同页右侧或路由 query `?kb=`）。
- 文档表格（shadcn Table）：文件名/状态 Badge（ready 绿、parsing 琥珀、pending_ocr 琥珀+「待OCR」、failed 红 + tooltip 显示 error）/上传时间/操作（failed→重试按钮调 retryDoc；删除按钮二次确认 Dialog 调 deleteDoc）。顶部「批量重试OCR」按钮（有 pending_ocr 时显示，调 retryOcr）。**状态轮询**：存在 parsing/pending 状态时每 3s 刷新，全 ready/failed 停止。
- UploadZone：拖拽高亮边框 + 点击选文件（multiple），上传后立即插入 parsing 行。
- KB 配置面板（Dialog）：chunk_size/chunk_overlap 数字输入 + 上下文增强 Switch——**注意后端目前无 KB config 写接口**：新增最小接口 `PUT /api/kb/{kb_id}/config`（body 直接存 KnowledgeBase.config JSON），在本任务实现（后端小改：api/main.py 一个端点 + tests/test_api.py 一条测试；这是 Plan A 的已知缺口，此处补上）。
- vitest：状态 Badge 映射纯函数测试。后端回归 83 passed。
- Commit：`feat(web): 知识库管理页（文档表格+拖拽上传+OCR重试+KB配置接口）`

---

### Task B5: 检索分析页

**Files:** Create `src/components/RetrievalTrace.vue`、`src/views/AnalysisView.vue`

- 顶部：KB 下拉 + 查询输入 + 「执行」按钮 → `search(query, {debug:true, topK:5})`。
- RetrievalTrace 三栏对比表：稠密路 top-10 / 关键词路 top-10 / RRF 融合 top-10，各列显示 chunk_id 短前缀 + 分数（保留 3 位）；融合列中双路命中的行加 accent 左边框。若 trace 含 reranked：第四列显示重排后 top-10，并对每个 id 标注名次变化（↑3/↓2/新进，比较对象为 fused 名次）。
- 底部：最终 blocks 卡片列表（doc_name/heading_path/score/text 折叠展示前 200 字）。
- 空态与错误态（KB 无文档 → 提示先去导入）。
- vitest：名次变化计算纯函数（fused vs reranked 排名差）测试。
- Commit：`feat(web): 检索分析页（双路/融合/重排名次对比可视化）`

---

### Task B6: 设置页

**Files:** Create `src/views/SettingsView.vue`

- Provider 卡片列表（name/model/base_url/并发/params 摘要；active 卡片 accent 边框 + 徽章）；卡片操作：设为默认（setActive）、编辑（Dialog 表单，PUT）、删除（确认后 DELETE，active 不可删）、「测试」按钮（testProvider → 成功显示延迟 ms 绿徽章 / 失败红 tooltip 错误详情，进行中 spinner）。
- 「+ 添加 Provider」Dialog：name/base_url/api_key_env/model/max_concurrency/params(JSON textarea，前端 JSON.parse 校验)。
- 系统状态面板：healthz 各组件状态点（ok 绿/degraded 琥珀/off 灰）。
- 主题切换（亮/暗 Segmented 控件，theme.ts 已有）。
- vitest：params JSON 校验纯函数测试。
- Commit：`feat(web): 设置页（Provider 管理+连通测试+健康面板+主题）`

---

### Task B7: 收尾与回归

- 暗色主题四页人工过检（dev server 切换核验，修补漏掉的硬编码色值——grep `#[0-9A-Fa-f]{6}` 于 src/ 应只出现在 tokens.css）。
- `npm run build` 产出 `web/`；确认 FastAPI 托管路径（create_app 的 web_dir 逻辑不变即可）；`git add web/ -f`？——**web/ 是构建产物：改为入库策略确认**：M1 的 web/ 在库里（零构建页）。M2 起 web/ 仍需入库（私有化交付不装 node）。保留入库，`.gitignore` 不排除 web/，但在 README 注明「web/ 为构建产物，修改前端请改 web-app/ 并重新 build」。
- README：前端开发章节（dev server/build/测试命令）、四页功能简介、主题定制说明（覆盖 tokens.css 变量）。
- 全量回归：后端 pytest 83 passed（含 B4 新增）+ 前端 vitest 全绿 + `npm run build` 成功。
- Commit：`chore(web): 暗色核验+构建产物入库+README 前端章节`

---

### Task B8: 端到端验收（控制器执行）

本任务由控制器（主会话）亲自执行，不派子代理：起后端（真实 data/）+ 构建产物模式（直接 8100 端口访问，不走 dev server），用浏览器预览工具走查四页全流程（新会话问答含引用抽屉与全文预览、上传/重试/删除、检索分析对比、Provider 测试与主题切换、多轮追问），截图存档；Plan A+B 全量终审子代理；merge main + push。

## 任务依赖

```
B1 → B2 → B3 → B4 → B5 → B6 → B7 → B8（顺序执行，B3-B6 共享布局与 api.ts）
```

## M3 候选事项（本计划不做，评审沉淀）

扁平标题 PDF 巨型父块的尺寸上限或标题深度下限；enrich_context 未参与重排的信息不对称；QueryRewrite；语义缓存/QA对检索器；conversations 分页；批量 OCR 重试的并发上限；providers 表与 YAML 的双向同步工具。
