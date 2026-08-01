// 产品导览步骤完整性：每步的 i18n key 必须在 zh.json 里真实存在（title/desc，
// action 步还要 action key）——防止改步骤时漏配文案，运行时卡片出现裸 key。
import { describe, expect, it } from "vitest";

import { TOUR_STEPS, samePath, stepHref } from "../demo-tour";
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
});
