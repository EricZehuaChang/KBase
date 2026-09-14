// E2E 共用夹具与操作助手。
//
// 原则：**造数据的动作全走真实 UI**（点按钮、选文件、按 Enter），只有收尾清理
// 走 API（见 global-teardown.ts）——清理不是被测对象，重跑一遍 UI 只会引入
// 与用例无关的抖动。断言一律等真实条件（元素/文案/状态），不用固定 sleep。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base, expect, type Locator, type Page } from "@playwright/test";

export { expect };

const HERE = path.dirname(fileURLToPath(import.meta.url)); // web-app/e2e
const CREATED_KB_FILE = path.join(HERE, "..", "test-results", "created-kbs.json");

// 上传用的样例文档：内容写死，问题围绕它，这样"引用里出现这份文档"是可以硬断言的。
export const SAMPLE_DOC_NAME = "e2e-smoke.md";
export const SAMPLE_DOC_CONTENT = [
  "# KBase 冒烟文档",
  "",
  "## 报销流程",
  "",
  "员工提交报销单后，需在 5 个工作日内完成审批。",
  "",
  "## 年假制度",
  "",
  "入职满一年可享受 10 天年假。",
  "",
].join("\n");

// 提问文案：样例文档里有答案（保证后端能检索到引用），且不超过会话标题的
// 截断长度（后端 append_round 取 question[:20]），会话列表用例可直接用全文断言。
export const SAMPLE_QUESTION = "报销单几个工作日内审批？";
// 引用角标用例专用：明确要求标编号——桩模型必带 [1]，真实模型也几乎不会漏。
export const CITATION_QUESTION =
  "报销单需要在几个工作日内完成审批？请依据资料原文作答，并在句末标注引用编号。";

// 等"答案 + 引用"落地的上限：桩模型毫秒级返回，复用本机真实 LLM 的 dev 栈时
// 一次生成要几秒到十几秒，留足余量。
export const ANSWER_TIMEOUT = 30_000;
// 文档状态由页面每 3s 轮询刷新（useKbDocs），假向量下小 Markdown 几秒就绪。
export const DOC_READY_TIMEOUT = 60_000;

/**
 * 首登邮箱引导弹窗（EmailPromptDialog）在"账号没绑邮箱"时会自动弹出并盖住页面，
 * dev 的合成 actor 恰好就是没绑邮箱——用 sessionStorage 标记在应用启动前抑制它，
 * 效果与用户点一次"稍后再说"一致（真实用户也是这么绕开的），省掉每个用例都要
 * 先关弹窗的噪声。
 */
async function suppressEmailPrompt(page: Page) {
  await page.addInitScript(() => {
    sessionStorage.setItem("kbase_email_prompt_dismissed", "1");
  });
}

/** 使用端首屏（问答页）。等会话侧栏出现 = 应用外壳渲染完成。 */
export async function gotoPortal(page: Page) {
  await suppressEmailPrompt(page);
  await page.goto("/");
  await expect(page.locator('[data-tour="session-sidebar"]')).toBeVisible();
}

/** 管理端首屏（知识库网格）。等"新建知识库"入口出现 = 网格渲染完成。 */
export async function gotoAdmin(page: Page) {
  await suppressEmailPrompt(page);
  // 路径是 /admin（没有尾斜杠）：Vite dev 的多页回退只在"无扩展名的 /admin"上
  // 命中 admin.html，/admin/ 会回退成使用端 index.html（生产由
  // kbase/api/static.py 的 SPAStaticFiles 处理 /admin/** 深链接，无此问题）。
  await page.goto("/admin");
  // Vite dev 的冷启动回退：源码变更让 bundled-dev 的内存文件集失效之后，第一次
  // 请求 /admin 有可能拿不到 admin.html 而回退成使用端 index.html——页面能打开，
  // 但渲染出来的是使用端外壳（外壳里没有"管理工作台"）。这里按真实判据（管理端
  // 标识在不在）重载一次：第一次请求已经把 admin.html 编译进内存，重载必然命中。
  try {
    await page.getByText("管理工作台").first().waitFor({ state: "visible", timeout: 2_000 });
  } catch {
    await page.reload();
  }
  await expect(page.getByRole("button", { name: "新建知识库" })).toBeVisible();
}

/** 独一无二的知识库名：dev 数据目录是共享且长期存在的，重名会让选择器歧义。 */
export function uniqueKbName(): string {
  return `E2E 冒烟库-${Date.now().toString(36)}`;
}

/** 管理端侧栏 / 顶栏面包屑：两个 SPA 的壳层结构，主区视图里另有 aside/header，
 * 壳层的永远在 DOM 里排第一。 */
export function adminSidebar(page: Page): Locator {
  return page.locator("aside").first();
}
export function adminBreadcrumb(page: Page): Locator {
  return page.locator("header").first();
}

/** 走管理端 UI 建库，返回新库 id（前端建完会跳到 /admin/?kb=<id>，从 URL 取，
 * 不再打一次 API）。 */
export async function createKbViaUi(page: Page, name: string): Promise<string> {
  await gotoAdmin(page);
  await page.getByRole("button", { name: "新建知识库" }).click();
  await page.getByPlaceholder("知识库名称").fill(name);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page).toHaveURL(/[?&]kb=/);
  const id = new URL(page.url()).searchParams.get("kb") ?? "";
  recordCreatedKb(id);
  return id;
}

/** 在知识库详情页用 UploadZone 的真实 file input 上传样例 Markdown（不调 API），
 * 然后等页面自己的状态轮询把这一行刷成"就绪"。 */
export async function uploadSampleDocAndWaitReady(page: Page) {
  await page.locator('[data-tour="upload-zone"] input[type="file"]').setInputFiles({
    name: SAMPLE_DOC_NAME,
    mimeType: "text/markdown",
    buffer: Buffer.from(SAMPLE_DOC_CONTENT, "utf-8"),
  });
  // 上传瞬间会先插一条乐观行（同文件名、状态"解析中"），就绪行的条数从 0 变
  // 成 1 才是真实状态落地；顺带断言没有重复行（乐观行已被告知结果替换）。
  await expect(docRows(page).filter({ hasText: "就绪" })).toHaveCount(1, {
    timeout: DOC_READY_TIMEOUT,
  });
}

/** 文档表格里按文件名取行（上传后每次请求的文档列表都只有这一个文件）。 */
export function docRows(page: Page): Locator {
  return page.getByRole("row").filter({ hasText: SAMPLE_DOC_NAME });
}

/** 从管理端网格点进某个知识库的详情页。 */
export async function openKbFromGrid(page: Page, kbName: string) {
  await gotoAdmin(page);
  await page.locator('[data-tour="kb-card"]').filter({ hasText: kbName }).click();
  await expect(page).toHaveURL(/[?&]kb=/);
}

/** 在使用端顶栏的知识库下拉里选中指定库（真实下拉交互，不是改 URL）。 */
export async function selectKbInTopbar(page: Page, kbName: string) {
  await page.locator('[data-tour="kb-select"]').click();
  await page.getByRole("option", { name: kbName }).click();
  await expect(page.locator('[data-tour="kb-select"]')).toContainText(kbName);
  // 选中库之后输入框才可用（kbId 为空时 textarea 是 disabled 的）
  await expect(chatInput(page)).toBeEnabled();
}

export function chatInput(page: Page): Locator {
  return page.locator('textarea[data-tour="chat-input"]');
}

/** 消息列表（ol）里的每条消息。 */
export function chatMessages(page: Page): Locator {
  return page.locator('ol[aria-label="对话消息列表"] > li');
}

/** 最后一条助手消息的正文容器（MessageStream 里流式追加文本的那个 div）。 */
export function answerBody(page: Page): Locator {
  return chatMessages(page).last().locator("div.whitespace-pre-wrap");
}

/** 引用计数徽标（"N 条引用"）：只有流结束且该轮 citations 非空时才渲染。 */
export function citationsBadge(page: Page): Locator {
  return chatMessages(page).last().getByText(/\d+ 条引用/);
}

/** 提问：填输入框 + Enter（页面提示的发送方式），等这一轮的消息进入列表。 */
export async function askQuestion(page: Page, question: string) {
  const input = chatInput(page);
  await input.fill(question);
  await input.press("Enter");
  await expect(chatMessages(page).filter({ hasText: question }).first()).toBeVisible();
}

/** 创建出来的知识库记到清单里，全套跑完由 global-teardown.ts 删掉。 */
export function recordCreatedKb(id: string) {
  if (!id) return;
  mkdirSync(path.dirname(CREATED_KB_FILE), { recursive: true });
  const known: string[] = existsSync(CREATED_KB_FILE)
    ? (JSON.parse(readFileSync(CREATED_KB_FILE, "utf-8")) as string[])
    : [];
  writeFileSync(CREATED_KB_FILE, JSON.stringify([...new Set([...known, id])], null, 2));
}

export interface SeededKb {
  id: string;
  name: string;
}

// 一个 worker 内共享的"有文档且已就绪"的知识库：建库 + 上传 + 等就绪全程走 UI，
// 只在第一个用到它的用例里做一次（模块级 promise 缓存），后续用例复用——假向量
// 下摄取也要几秒，没必要每个用例都重来。
let seededKbPromise: Promise<SeededKb> | null = null;

async function seedKb(page: Page): Promise<SeededKb> {
  const name = uniqueKbName();
  const id = await createKbViaUi(page, name);
  await uploadSampleDocAndWaitReady(page);
  return { id, name };
}

export const test = base.extend<{ seededKb: SeededKb }>({
  seededKb: async ({ page }, use) => {
    seededKbPromise ??= seedKb(page);
    await use(await seededKbPromise);
  },
});
