import { describe, expect, it } from "vitest";
import {
  validateParamsJson, paramsSummary, healthDot, licenseBannerInfo, isLastEnabledAdmin,
  auditDailyCounts, parseAuditTs,
  buildProviderBody, keySource, PROVIDER_PRESETS, vendorBadge,
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

describe("keySource（返回 i18n key + 参数）", () => {
  it("直配密钥优先展示", () => {
    expect(keySource({ has_api_key: true, api_key_hint: "****abcd", api_key_env: "K" }))
      .toEqual({ key: "provider.key_configured", params: { hint: "****abcd" } });
  });
  it("无直配时展示环境变量名", () => {
    expect(keySource({ has_api_key: false, api_key_hint: null, api_key_env: "MY_KEY" }))
      .toEqual({ key: "provider.key_env", params: { env: "MY_KEY" } });
  });
  it("两者皆无=未配置", () => {
    expect(keySource({ has_api_key: false, api_key_hint: null, api_key_env: "" }))
      .toEqual({ key: "provider.key_none" });
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

  it("非法 JSON 报错（i18n key + 原因参数）", () => {
    const r = validateParamsJson("{temperature:0.7}");
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.errorKey).toBe("provider.params_json_error");
      expect(r.errorParams?.msg).toBeTruthy();
    }
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

  it("valid 但距到期超过 30 天不提醒", () => {
    expect(licenseBannerInfo(
      { status: "valid", expires: "2026-12-31" }, new Date("2026-07-15"))).toBeNull();
  });

  it("valid 且 30 天内到期展示续期警示（E）", () => {
    const info = licenseBannerInfo(
      { status: "valid", expires: "2026-08-01" }, new Date("2026-07-15"));
    expect(info?.tone).toBe("warn");
    expect(info?.messageKey).toBe("license.banner_expiring");
    expect(info?.messageParams).toEqual({ date: "2026-08-01", days: 17 });
  });

  it("valid 但 expires 非法不提醒（过期由后端 expired 态兜底）", () => {
    expect(licenseBannerInfo(
      { status: "valid", expires: "not-a-date" }, new Date("2026-07-15"))).toBeNull();
  });

  it("trial 状态展示提示色横幅", () => {
    const info = licenseBannerInfo({ status: "trial" });
    expect(info).not.toBeNull();
    expect(info?.tone).toBe("info");
    expect(info?.messageKey).toBe("license.banner_trial");
  });

  it("expired 状态展示警告色横幅并带到期日", () => {
    const info = licenseBannerInfo({ status: "expired", org: "acme", expires: "2026-01-01" });
    expect(info?.tone).toBe("warn");
    expect(info?.messageKey).toBe("license.banner_expired");
    expect(info?.messageParams).toEqual({ date: "2026-01-01" });
  });

  it("invalid 状态展示警告色横幅", () => {
    const info = licenseBannerInfo({ status: "invalid" });
    expect(info?.tone).toBe("warn");
    expect(info?.messageKey).toBe("license.banner_invalid");
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

  // ---- 超管层级引入后：护"最后一个启用 superadmin"；有超管在场时普通
  // admin 不再触发锁（后端不变量同步，见 kbase/api/routes/admin.py）
  const withSuper = [
    { id: "s1", username: "root", role: "superadmin", disabled: false, created_at: "" },
    { id: "1", username: "admin", role: "admin", disabled: false, created_at: "" },
  ];

  it("唯一启用中的 superadmin 判定为 true", () => {
    expect(isLastEnabledAdmin(withSuper, "s1")).toBe(true);
  });

  it("有超管在场时，唯一 admin 不再是最后防线——false", () => {
    expect(isLastEnabledAdmin(withSuper, "1")).toBe(false);
  });

  it("存在另一个启用中的 superadmin 时判定为 false", () => {
    const two = [...withSuper,
      { id: "s2", username: "root2", role: "superadmin", disabled: false, created_at: "" }];
    expect(isLastEnabledAdmin(two, "s1")).toBe(false);
  });
});

describe("auditDailyCounts / parseAuditTs（审计趋势图）", () => {
  it("无时区的 UTC 时间戳补 Z 解析", () => {
    const d = parseAuditTs("2026-07-26T01:30:00.123456");
    expect(d.getTime()).toBe(Date.parse("2026-07-26T01:30:00.123Z"));
  });

  it("带时区标记的时间戳原样解析", () => {
    expect(parseAuditTs("2026-07-26T01:30:00Z").getTime())
      .toBe(Date.parse("2026-07-26T01:30:00Z"));
  });

  it("近 N 天逐日归组：窗口固定长度、今天在末位、越界旧行忽略", () => {
    const now = new Date(2026, 6, 26, 12, 0, 0);   // 本地 2026-07-26 中午
    const items = [
      { ts: "2026-07-26T01:00:00" },   // 今天（UTC 早晨→本地同日或前日，见下）
      { ts: "2026-07-25T23:00:00" },
      { ts: "2026-07-01T00:00:00" },   // 越界（>14 天前），忽略
    ];
    const out = auditDailyCounts(items, 14, now);
    expect(out).toHaveLength(14);
    expect(out[13].date).toBe("2026-07-26");
    const totalCounted = out.reduce((s, d) => s + d.count, 0);
    expect(totalCounted).toBe(2);      // 只有窗口内两条计入
  });

  it("空输入返回全零窗口", () => {
    const out = auditDailyCounts([], 7, new Date(2026, 6, 26));
    expect(out).toHaveLength(7);
    expect(out.every((d) => d.count === 0)).toBe(true);
  });
});

describe("vendorBadge 厂商识别", () => {
  it("按 base_url 域名识别主流厂商并给官方 logo 资产名", () => {
    expect(vendorBadge("https://open.bigmodel.cn/api/paas/v4").icon).toBe("zhipu");
    expect(vendorBadge("https://dashscope.aliyuncs.com/compatible-mode/v1").icon).toBe("qwen");
    expect(vendorBadge("https://api.deepseek.com/v1").icon).toBe("deepseek");
    expect(vendorBadge("https://api.openai.com/v1").icon).toBe("openai");
    expect(vendorBadge("https://api.moonshot.cn/v1").icon).toBe("kimi");
    expect(vendorBadge("https://api.siliconflow.cn/v1").icon).toBe("siliconcloud");
  });

  it("icon 资产名与 assets/vendors 实际文件一一对应（拦资产漂移）", () => {
    const files = Object.keys(import.meta.glob("../../assets/vendors/*.svg"))
      .map((p) => p.split("/").pop()!.replace(/(-color)?\.svg$/, ""));
    for (const url of [
      "https://open.bigmodel.cn/api/paas/v4",
      "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "https://api.deepseek.com/v1",
      "https://api.openai.com/v1",
      "https://api.moonshot.cn/v1",
      "https://api.siliconflow.cn/v1",
    ]) {
      expect(files, url).toContain(vendorBadge(url).icon);
    }
  });

  it("未知域名（企业自有兼容平台）无 icon，回落中性 API 徽章", () => {
    const v = vendorBadge("https://llm.internal.corp/v1");
    expect(v.icon).toBeUndefined();
    expect(v.short).toBe("API");
    expect(v.color).toBeTruthy();
  });
});
