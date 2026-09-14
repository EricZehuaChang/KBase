// 管理端（admin.html 挂在 /admin）：壳层渲染 + 关键页面可达不白屏。
// 管理端是另一个 Vite 入口的独立 bundle，路由与使用端完全隔离——bundle 能不能
// 起来、深链接会不会白屏，只有浏览器能回答。
import { adminBreadcrumb, adminSidebar, expect, gotoAdmin, test } from "./fixtures";

// 侧栏导航项 → 真实路由（web-app/src/admin/router.ts，base "/admin"）与页面
// 自身的文案标记。路由路径按 router.ts 核对过，不是猜的。
const ADMIN_PAGES = [
  { nav: "检索分析", path: "/admin/analysis", marker: "检索试跑" },
  { nav: "生成", path: "/admin/generate", marker: "方案生成" },
  { nav: "设置", path: "/admin/settings", marker: "模型 Provider、用户与密钥、许可证与系统状态" },
  { nav: "多语言", path: "/admin/translations", marker: "编辑各语言界面译文" },
];

test.describe("管理端", () => {
  test("首页可达：分组侧栏 + 顶部面包屑 + 知识库网格", async ({ page }) => {
    await gotoAdmin(page);

    // 侧栏：三组分组标题 + 内容组的知识库入口
    const sidebar = adminSidebar(page);
    await expect(sidebar).toContainText("内容");
    await expect(sidebar).toContainText("分析");
    await expect(sidebar).toContainText("系统");
    await expect(sidebar.getByRole("button", { name: "知识库" })).toBeVisible();
    await expect(sidebar).toContainText("返回问答");

    // 顶栏面包屑：管理工作台 / 知识库（当前激活导航项）
    const breadcrumb = adminBreadcrumb(page);
    await expect(breadcrumb).toContainText("管理工作台");
    await expect(breadcrumb).toContainText("知识库");

    // 主区不是白屏：页面标题 + 说明
    await expect(page.getByRole("heading", { name: "知识库" })).toBeVisible();
    await expect(page.getByText("管理企业知识库、文档导入与访问权限")).toBeVisible();
  });

  // 深链直达：**生产契约**。生产由 kbase/api/static.py 的 SPAStaticFiles 把
  // /admin/** 全部交给 admin.html，用户刷新/收藏夹直达/分享链接都走这条路——
  // 每个管理页都必须能独立冷启动渲染，不能依赖"先从首页点进来"。
  //
  // 为什么不用侧栏点击来测这条：Vite dev 的多页回退只在**无扩展名的 /admin**
  // 上命中 admin.html，/admin/analysis 这类深链接会被回退成使用端 index.html
  // （使用端有 catch-all 路由，于是渲染出使用端的"请先选择知识库"空态）。
  // 于是"点侧栏→若发生整页加载→页面变成使用端"，测出来的是 Vite dev 的怪癖
  // 而不是产品行为——本机上偶然全绿、Linux runner 冷启动下就红（2026-09-14
  // CI 实测）。侧栏点击的连通性另由下一条用例覆盖。
  for (const target of ADMIN_PAGES) {
    test(`深链直达不白屏：${target.path}`, async ({ page }) => {
      await gotoAdmin(page, target.path);
      await expect(page).toHaveURL(new RegExp(`${target.path}$`));
      await expect(adminBreadcrumb(page)).toContainText(target.nav);
      // 白屏判据：主区里该页自己的文案必须在
      await expect(page.locator("main")).toContainText(target.marker);
      // 壳层没被路由卸载
      await expect(adminSidebar(page)).toBeVisible();
    });
  }

  test("侧栏点击能切页（站内导航连通性）", async ({ page }) => {
    await gotoAdmin(page);
    const first = ADMIN_PAGES[0];
    await adminSidebar(page).getByRole("button", { name: first.nav }).click();
    // 只断言"导航发生且目标页渲染出来"——用 admin 壳层仍在 + 该页文案出现
    // 双重判据，避免把 dev 回退行为写进断言。
    await expect(adminSidebar(page)).toBeVisible();
    await expect(page.locator("main")).toContainText(first.marker);
  });
});
