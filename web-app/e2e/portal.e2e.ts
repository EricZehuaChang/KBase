// 使用端（index.html 挂在 /）：首屏外壳与语言切换。
// 这两件事只有真浏览器能验证——"外壳渲染没渲染"、"i18n 运行时换语言后界面文案
// 是不是真换了"，后端测试和 jsdom 单测都盖不到。
import { expect, gotoPortal, test } from "./fixtures";

test.describe("使用端", () => {
  test("首页渲染应用外壳（顶栏 + 会话侧栏 + 输入区），无致命控制台错误", async ({ page }) => {
    const fatal: string[] = [];
    // 崩在白屏之前的 JS 错误：未捕获异常 + console.error。真撞上崩溃时
    // 下面的可见性断言会先红，这里再补一条"具体错在哪"的证据。
    page.on("pageerror", (err) => fatal.push(`pageerror: ${err.message}`));
    page.on("console", (msg) => {
      if (msg.type() === "error") fatal.push(`console.error: ${msg.text()}`);
    });

    await gotoPortal(page);

    // 顶栏：产品名 + 知识库选择器 + 用户菜单
    await expect(page.locator("header")).toContainText("KBase");
    await expect(page.locator('[data-tour="kb-select"]')).toBeVisible();
    await expect(page.getByRole("button", { name: "账号与设置" })).toBeVisible();
    // 会话侧栏 + 空状态 + 输入区（新会话没有任何消息时才显示空状态）
    await expect(page.locator('[data-tour="session-sidebar"]')).toBeVisible();
    await expect(page.getByText("有什么可以帮你？")).toBeVisible();
    await expect(page.locator('textarea[data-tour="chat-input"]')).toBeVisible();
    await expect(page.locator('[data-tour="send"]')).toBeVisible();

    expect(fatal).toEqual([]);
  });

  test("语言切换：中文 → English → Bahasa Melayu，界面文案真的跟着换", async ({ page }) => {
    await gotoPortal(page);
    // 登录态下的语言入口在顶栏用户菜单的二级列表（登录页那个平铺切换器在
    // auth=off 下进不去：有会话时 /login 会直接跳回首页）。
    const input = page.locator('textarea[data-tour="chat-input"]');
    await expect(input).toHaveAttribute("placeholder", /向知识库提问/);

    await page.getByRole("button", { name: "账号与设置" }).click();
    await page.getByRole("button", { name: /^语言/ }).click();
    await page.getByRole("button", { name: "English", exact: true }).click();

    // 已知 key 的文本真的换了：lang.label「语言」→「Language」，<html lang> 同步；
    // 弹层外的 placeholder（portal.chat.input_placeholder）也跟着换。
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page.getByRole("button", { name: /^Language/ })).toBeVisible();
    await expect(input).toHaveAttribute("placeholder", /Enter your question for the knowledge base/);

    // 弹层保持打开（选完回到主视图），继续切马来语
    await page.getByRole("button", { name: /^Language/ }).click();
    await page.getByRole("button", { name: "Bahasa Melayu", exact: true }).click();

    await expect(page.locator("html")).toHaveAttribute("lang", "ms");
    await expect(page.getByRole("button", { name: /^Bahasa/ })).toBeVisible();
    await expect(input).toHaveAttribute("placeholder", /Masukkan soalan/);

    // 关掉弹层，看弹层之外的文案（会话侧栏按钮）也换了
    await page.keyboard.press("Escape");
    await expect(
      page.locator('[data-tour="session-sidebar"]').getByRole("button", { name: "Sembang baharu" }).first(),
    ).toBeVisible();
  });
});
