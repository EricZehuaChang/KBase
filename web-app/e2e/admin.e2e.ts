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

  test("关键页面逐个可达且不白屏（检索分析 / 生成 / 设置 / 多语言）", async ({ page }) => {
    await gotoAdmin(page);

    for (const target of ADMIN_PAGES) {
      // 走侧栏真实导航（不是直接 goto 深链接）：Vite dev 的多页回退只在
      // "/admin" 上命中 admin.html，/admin/analysis 这类深链接会被回退成使用端
      // index.html；生产由 SPAStaticFiles 处理，开发期靠站内导航。
      await adminSidebar(page).getByRole("button", { name: target.nav }).click();

      await expect(page).toHaveURL(new RegExp(`${target.path}$`));
      // 面包屑跟着走（当前导航项的 i18n 文案）
      await expect(adminBreadcrumb(page)).toContainText(target.nav);
      // 白屏判据：主区里该页自己的文案必须在
      await expect(page.locator("main")).toContainText(target.marker);
      // 侧栏还在（不是整页被替换掉/路由把壳层也卸载了）
      await expect(adminSidebar(page)).toBeVisible();
    }
  });
});
