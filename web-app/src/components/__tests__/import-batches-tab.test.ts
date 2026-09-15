// ImportBatchesTab（T18）冒烟：批次清单渲染 + 展开明细（含"只看失败"过滤）+
// 导出直链指向 CSV 端点。网络用 stub 顶掉——这里验的是渲染与请求参数，
// 后端契约由 tests/test_bulk_import.py 的接口测试钉住。
import { flushPromises, mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { afterEach, describe, expect, it, vi } from "vitest";

import ImportBatchesTab from "../ImportBatchesTab.vue";
import zh from "../../i18n/locales/zh.json";
// 角色门控（导出直链按"能管内容"显示，与页面其余运维动作同一把尺子）
import { currentRole } from "../../lib/api";

const BATCH = {
  id: "11111111-2222-3333-4444-555555555555",
  kb_id: "kb1",
  manifest_path: "import-kb1.jsonl",
  started_by: "ops",
  started_at: "2026-01-01T00:00:00",
  finished_at: "2026-01-01T00:05:00",
  status: "done_with_errors" as const,
  summary: { status: "done_with_errors" as const, total: 3, done: 2, failed: 1, elapsed_s: 12.5 },
};

const ENTRIES = {
  items: [
    { path: "/corpus/fail1.md", status: "failed", size: 3, mtime: 1, doc_id: "d1",
      doc_status: "failed", error: "模拟解析失败", ts: "2026-01-01T00:01:00" },
    { path: "/corpus/ok1.md", status: "done", size: 3, mtime: 1, doc_id: "d2",
      doc_status: "ready", error: null, ts: "2026-01-01T00:02:00" },
  ],
  total: 2, failures: 1, manifest_available: true, batch: BATCH,
};

function mountTab() {
  const i18n = createI18n({ legacy: false, locale: "zh", messages: { zh } });
  return mount(ImportBatchesTab, { props: { kbId: "kb1" }, global: { plugins: [i18n] } });
}

afterEach(() => {
  vi.unstubAllGlobals();
  currentRole.value = null;
});

describe("ImportBatchesTab（只读导入记录）", () => {
  it("渲染批次行：状态文案、计数、发起人", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(new Response(JSON.stringify(
        url.includes("/entries") ? ENTRIES : { items: [BATCH], total: 1 }),
        { status: 200, headers: { "content-type": "application/json" } })));
    vi.stubGlobal("fetch", fetchMock);

    const w = mountTab();
    await flushPromises();
    expect(w.text()).toContain("部分失败");       // 状态码本地化
    expect(w.text()).toContain("成功 2，失败 1，共 3");
    expect(w.text()).toContain("11111111");       // 批次短 id
    expect(w.text()).toContain("ops");
    // 只读：页面上没有任何触发导入的按钮文案
    expect(w.text()).not.toContain("开始导入");
  });

  it("展开明细 + 只看失败：请求带 failures_only 且只渲染失败行", async () => {
    const calls: string[] = [];
    const fetchMock = vi.fn((url: string) => {
      calls.push(String(url));
      const onlyFailures = String(url).includes("failures_only=true");
      const body = onlyFailures ? { ...ENTRIES, items: [ENTRIES.items[0]], total: 1 }
                                : ENTRIES;
      return Promise.resolve(new Response(JSON.stringify(
        String(url).includes("/entries") ? body : { items: [BATCH], total: 1 }),
        { status: 200, headers: { "content-type": "application/json" } }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const w = mountTab();
    await flushPromises();
    currentRole.value = "editor";                    // 导出直链的门控角色
    await flushPromises();
    await w.find("button[aria-expanded=false]").trigger("click");  // 展开批次行
    await flushPromises();
    expect(w.text()).toContain("/corpus/ok1.md");
    expect(w.text()).toContain("模拟解析失败");

    // 打开"只看失败"开关 → 重新请求并过滤
    await w.find("button[role=switch]").trigger("click");
    await flushPromises();
    expect(calls.some((u) => u.includes("failures_only=true"))).toBe(true);
    expect(w.text()).not.toContain("/corpus/ok1.md");
    expect(w.text()).toContain("/corpus/fail1.md");
    // 导出直链指向 CSV 端点（列完整由后端导出测试钉）
    const href = w.find("a").attributes("href");
    expect(href).toContain("/api/import-batches/11111111-2222-3333-4444-555555555555/export.csv");
  });

  it("没有批次时给空态提示（不报错）", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({ items: [], total: 0 }),
                                   { status: 200, headers: { "content-type": "application/json" } }))));
    const w = mountTab();
    await flushPromises();
    expect(w.text()).toContain("还没有批量导入记录");
  });
});
