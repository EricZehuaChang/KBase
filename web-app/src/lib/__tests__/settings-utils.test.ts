import { describe, expect, it } from "vitest";
import { validateParamsJson, paramsSummary, healthDot } from "../settings-utils";

describe("validateParamsJson", () => {
  it("空字符串视为空对象", () => {
    expect(validateParamsJson("")).toEqual({ ok: true, value: {} });
    expect(validateParamsJson("   ")).toEqual({ ok: true, value: {} });
  });

  it("合法 JSON 对象解析成功", () => {
    expect(validateParamsJson('{"temperature":0.7,"top_p":0.9}')).toEqual({
      ok: true,
      value: { temperature: 0.7, top_p: 0.9 },
    });
  });

  it("非法 JSON 报错并附带原始错误信息", () => {
    const r = validateParamsJson("{temperature:0.7}");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain("JSON 格式错误");
  });

  it("JSON 顶层非对象（数组/字符串/数字）报错", () => {
    expect(validateParamsJson("[1,2,3]").ok).toBe(false);
    expect(validateParamsJson('"hello"').ok).toBe(false);
    expect(validateParamsJson("42").ok).toBe(false);
    expect(validateParamsJson("null").ok).toBe(false);
  });
});

describe("paramsSummary", () => {
  it("空/null/undefined 均返回占位符", () => {
    expect(paramsSummary(null)).toBe("—");
    expect(paramsSummary(undefined)).toBe("—");
    expect(paramsSummary({})).toBe("—");
  });

  it("多字段按 key=value 逗号拼接", () => {
    expect(paramsSummary({ temperature: 0.7, top_p: 0.9 })).toBe("temperature=0.7, top_p=0.9");
  });
});

describe("healthDot", () => {
  it("ok/on 映射绿色", () => {
    expect(healthDot("ok")).toEqual({ label: "ok", class: "bg-[var(--ok)]" });
    expect(healthDot("on")).toEqual({ label: "on", class: "bg-[var(--ok)]" });
  });

  it("degraded 映射琥珀色", () => {
    expect(healthDot("degraded")).toEqual({ label: "degraded", class: "bg-[var(--warn)]" });
  });

  it("off 映射灰色", () => {
    expect(healthDot("off")).toEqual({ label: "off", class: "bg-[var(--text-3)]" });
  });

  it("类名字符串（embedder/vectorstore）非空视为正常绿色", () => {
    expect(healthDot("LocalEmbedder")).toEqual({ label: "LocalEmbedder", class: "bg-[var(--ok)]" });
  });

  it("空字符串兜底灰色占位", () => {
    expect(healthDot("")).toEqual({ label: "—", class: "bg-[var(--text-3)]" });
  });
});
