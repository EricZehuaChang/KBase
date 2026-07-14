import { describe, expect, it } from "vitest";
import {
  validateParamsJson, paramsSummary, healthDot, licenseBannerInfo, isLastEnabledAdmin,
  buildProviderBody, keySourceLabel, PROVIDER_PRESETS,
} from "../settings-utils";

describe("buildProviderBody（M5-2 密钥字段规则）", () => {
  const form = {
    base_url: " https://x/v1 ", api_key_env: " ENV_K ", api_key: "",
    model: " m1 ", max_concurrency: 4, params: { a: 1 },
  };

  it("创建：api_key 留空不带字段（只用环境变量）", () => {
    const body = buildProviderBody(form, { editing: false });
    expect(body).toEqual({
      base_url: "https://x/v1", api_key_env: "ENV_K", model: "m1",
      max_concurrency: 4, params: { a: 1 },
    });
    expect("api_key" in body).toBe(false);
  });

  it("创建：填了 api_key 就带上（trim 后）", () => {
    const body = buildProviderBody({ ...form, api_key: " sk-abc " }, { editing: false });
    expect(body.api_key).toBe("sk-abc");
  });

  it("编辑：留空=不带 api_key 字段（后端 PATCH 语义不动密钥）", () => {
    const body = buildProviderBody(form, { editing: true });
    expect("api_key" in body).toBe(false);
  });

  it("编辑：填新值覆盖", () => {
    const body = buildProviderBody({ ...form, api_key: "sk-new" }, { editing: true });
    expect(body.api_key).toBe("sk-new");
  });

  it("编辑：clearKey 显式清除（api_key: \"\" → 后端置 NULL 回退环境变量）", () => {
    const body = buildProviderBody(form, { editing: true, clearKey: true });
    expect(body.api_key).toBe("");
  });
});

describe("keySourceLabel", () => {
  it("直配密钥优先展示", () => {
    expect(keySourceLabel({ has_api_key: true, api_key_hint: "****abcd", api_key_env: "K" }))
      .toBe("已配置 ****abcd");
  });
  it("无直配时展示环境变量名", () => {
    expect(keySourceLabel({ has_api_key: false, api_key_hint: null, api_key_env: "MY_KEY" }))
      .toBe("环境变量 MY_KEY");
  });
  it("两者皆无=未配置", () => {
    expect(keySourceLabel({ has_api_key: false, api_key_hint: null, api_key_env: "" }))
      .toBe("未配置");
  });
});

describe("PROVIDER_PRESETS", () => {
  it("key 唯一且 base_url 均为 https OpenAI 兼容端点", () => {
    const keys = PROVIDER_PRESETS.map((p) => p.key);
    expect(new Set(keys).size).toBe(keys.length);
    for (const p of PROVIDER_PRESETS) {
      expect(p.base_url).toMatch(/^https:\/\//);
      expect(p.models.length).toBeGreaterThan(0);
      expect(p.api_key_env).toMatch(/^[A-Z_0-9]+$/);
    }
  });
});

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

describe("licenseBannerInfo", () => {
  it("valid 状态不展示横幅", () => {
    expect(licenseBannerInfo({ status: "valid" })).toBeNull();
  });

  it("trial 状态展示提示色横幅", () => {
    const info = licenseBannerInfo({ status: "trial" });
    expect(info).not.toBeNull();
    expect(info?.tone).toBe("info");
    expect(info?.message).toContain("试用");
  });

  it("expired 状态展示警告色横幅并带到期日", () => {
    const info = licenseBannerInfo({ status: "expired", org: "acme", expires: "2026-01-01" });
    expect(info?.tone).toBe("warn");
    expect(info?.message).toContain("2026-01-01");
  });

  it("invalid 状态展示警告色横幅", () => {
    const info = licenseBannerInfo({ status: "invalid" });
    expect(info?.tone).toBe("warn");
    expect(info?.message).toContain("无效");
  });
});

describe("isLastEnabledAdmin", () => {
  const users = [
    { id: "1", username: "admin", role: "admin", disabled: false, created_at: "" },
    { id: "2", username: "alice", role: "editor", disabled: false, created_at: "" },
  ];

  it("唯一启用中的 admin 判定为 true", () => {
    expect(isLastEnabledAdmin(users, "1")).toBe(true);
  });

  it("非 admin 用户判定为 false", () => {
    expect(isLastEnabledAdmin(users, "2")).toBe(false);
  });

  it("存在另一个启用中的 admin 时判定为 false", () => {
    const withSecondAdmin = [...users,
      { id: "3", username: "admin2", role: "admin", disabled: false, created_at: "" }];
    expect(isLastEnabledAdmin(withSecondAdmin, "1")).toBe(false);
  });

  it("已禁用的其他 admin 不计入在场——仍判定为 true", () => {
    const withDisabledAdmin = [...users,
      { id: "3", username: "admin2", role: "admin", disabled: true, created_at: "" }];
    expect(isLastEnabledAdmin(withDisabledAdmin, "1")).toBe(true);
  });
});
