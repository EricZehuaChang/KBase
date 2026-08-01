// 产品导览步骤完整性：每步的 i18n key 必须在 zh.json 里真实存在（title/desc，
// action 步还要 action key）——防止改步骤时漏配文案，运行时卡片出现裸 key。
// demo 演示动作同理：type 原语的 textKey 必须真实存在；锚点名必须能在源码
// 某个 data-tour 属性里找到——拼写错的锚点运行时只是静默跳过，肉眼难发现。
import { describe, expect, it } from "vitest";

import {
  TOUR_STEPS, clampPos, loadCardPos, samePath, saveCardPos, stepHref,
} from "../demo-tour";
import zh from "../../i18n/locales/zh.json";

describe("产品导览步骤配置", () => {
  const tour = (zh as Record<string, unknown>).tour as Record<
    string, Record<string, string>>;

  it("每步 app/path 合法，i18n key 齐全", () => {
    for (const s of TOUR_STEPS) {
      expect(["portal", "admin"]).toContain(s.app);
      expect(s.path.startsWith("/")).toBe(true);
      expect(tour[s.key]?.title, `tour.${s.key}.title`).toBeTruthy();
      expect(tour[s.key]?.desc, `tour.${s.key}.desc`).toBeTruthy();
      if (s.action) expect(tour[s.key]?.action, `tour.${s.key}.action`).toBeTruthy();
    }
  });

  it("首末步都在使用端（登录落地页开始、干净页面收尾）", () => {
    expect(TOUR_STEPS[0].app).toBe("portal");
    expect(TOUR_STEPS[TOUR_STEPS.length - 1].app).toBe("portal");
  });

  it("stepHref：admin 步骤带 /admin 前缀；samePath 忽略查询串", () => {
    const adminStep = TOUR_STEPS.find((s) => s.app === "admin" && s.path.includes("?"))!;
    expect(stepHref(adminStep).startsWith("/admin/")).toBe(true);
    expect(samePath(adminStep.path.split("?")[0], adminStep)).toBe(true);
  });

  it("demo 锚点名必须存在于源码某个 data-tour 属性（拦拼写漂移）", () => {
    // Vite 原生 glob 把 src 全部 .vue 当原始文本拉进来收集 data-tour="xx"；
    // 锚点改名/删除而步骤配置没跟上时这里红，而不是运行时静默跳过
    const sources = import.meta.glob("../../**/*.vue", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>;
    const anchors = new Set<string>();
    for (const text of Object.values(sources)) {
      for (const m of text.matchAll(/data-tour="([^"]+)"/g)) anchors.add(m[1]);
    }
    for (const s of TOUR_STEPS) {
      for (const op of s.demo ?? []) {
        expect(anchors.has(op.target), `锚点 ${op.target}（tour.${s.key}）`).toBe(true);
        if (op.do === "click" && op.skipIf) {
          expect(anchors.has(op.skipIf), `skipIf 锚点 ${op.skipIf}`).toBe(true);
        }
        if (op.do === "type" && op.textFrom) {
          expect(anchors.has(op.textFrom), `textFrom 锚点 ${op.textFrom}`).toBe(true);
        }
      }
    }
  });

  it("demo 演示动作：target 非空，type 的 textKey 在 zh.json 真实存在", () => {
    // "tour.ask.sample" 之类的点号 key 逐级下钻 zh.json 嵌套对象
    const resolve = (key: string): unknown =>
      key.split(".").reduce<unknown>(
        (obj, part) => (obj as Record<string, unknown> | undefined)?.[part], zh);
    for (const s of TOUR_STEPS) {
      for (const op of s.demo ?? []) {
        expect(op.target, `tour.${s.key} demo target`).toBeTruthy();
        if (op.do === "click" && op.skipIf !== undefined) {
          expect(op.skipIf, `tour.${s.key} skipIf`).toBeTruthy();
        }
        if (op.do === "type") {
          expect(resolve(op.textKey), `${op.textKey} 缺失`).toBeTruthy();
        }
      }
    }
  });
});

describe("导览卡拖拽位置 clampPos", () => {
  it("视口内的位置原样保留", () => {
    expect(clampPos({ x: 100, y: 200 }, 400, 300, 1280, 800))
      .toEqual({ x: 100, y: 200 });
  });

  it("超出右/下边界夹回（保 8px 边距）", () => {
    expect(clampPos({ x: 2000, y: 2000 }, 400, 300, 1280, 800))
      .toEqual({ x: 1280 - 400 - 8, y: 800 - 300 - 8 });
  });

  it("负坐标夹回左上角边距", () => {
    expect(clampPos({ x: -50, y: -50 }, 400, 300, 1280, 800))
      .toEqual({ x: 8, y: 8 });
  });

  it("卡片比视口还大时贴左上角（保住拖拽把手）", () => {
    expect(clampPos({ x: 100, y: 100 }, 500, 400, 375, 300))
      .toEqual({ x: 8, y: 8 });
  });

  it("saveCardPos/loadCardPos 往返一致；脏数据回落 null", () => {
    saveCardPos({ x: 120, y: 260 });
    expect(loadCardPos()).toEqual({ x: 120, y: 260 });
    localStorage.setItem("kbase-tour-pos", "not-json");
    expect(loadCardPos()).toBeNull();
    localStorage.setItem("kbase-tour-pos", JSON.stringify({ x: "a", y: 2 }));
    expect(loadCardPos()).toBeNull();
    localStorage.removeItem("kbase-tour-pos");
    expect(loadCardPos()).toBeNull();
  });
});
