// 使用端问答与会话：答案文本、引用区（计数徽标 / 角标 / 引用抽屉）、会话历史。
// "引用"是本产品的核心卖点，也是唯一必须由浏览器断言的部分：SSE 事件 → 消息流
// 渲染 → [n] 角标 → 来源预览 → 引用抽屉，整条链只有真浏览器能走通。
import {
  ANSWER_TIMEOUT,
  answerBody,
  askQuestion,
  chatMessages,
  citationsBadge,
  CITATION_QUESTION,
  expect,
  gotoPortal,
  SAMPLE_DOC_NAME,
  SAMPLE_QUESTION,
  selectKbInTopbar,
  test,
} from "./fixtures";

test.describe("问答与会话（使用端）", () => {
  test("提问后答案文本出现，引用区出现", async ({ page, seededKb }) => {
    await gotoPortal(page);
    await selectKbInTopbar(page, seededKb.name);
    await askQuestion(page, SAMPLE_QUESTION);

    // 一轮问答 = 用户消息 + 助手消息
    await expect(chatMessages(page)).toHaveCount(2);
    await expect(chatMessages(page).first()).toContainText(SAMPLE_QUESTION);

    // 答案正文：流式第一片到达后正文块才渲染（此前是"思考中…"占位）
    await expect(answerBody(page)).toHaveText(/\S/, { timeout: ANSWER_TIMEOUT });
    await expect(answerBody(page)).not.toHaveText(/思考中/);

    // 引用区：计数徽标只在流结束且该轮 citations 非空时渲染，数据来自后端
    // 真实检索结果（不是模型正文里编的）
    await expect(citationsBadge(page)).toBeVisible({ timeout: ANSWER_TIMEOUT });
  });

  test("引用角标可点开来源预览，并打开引用抽屉看原文", async ({ page, seededKb }) => {
    await gotoPortal(page);
    await selectKbInTopbar(page, seededKb.name);
    // 提问里明确要求标注引用编号：E2E 桩模型（CI/无密钥环境）必带 [1]，
    // 真实 LLM 也几乎不会漏，角标断言因此是硬的。
    await askQuestion(page, CITATION_QUESTION);
    await expect(citationsBadge(page)).toBeVisible({ timeout: ANSWER_TIMEOUT });

    const chip = chatMessages(page).last().locator('[data-tour="citation-marker"]').first();
    await expect(chip).toBeVisible({ timeout: ANSWER_TIMEOUT });
    await chip.click();

    // 角标弹层：来源文档名 + 「查看原文」入口
    await expect(page.getByText(SAMPLE_DOC_NAME).first()).toBeVisible();
    await page.getByRole("button", { name: "查看原文" }).click();

    // 引用抽屉（aria-label="引用详情"）：命中文档名 + 相关度分数（真实检索数据）
    const drawer = page.getByLabel("引用详情");
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText(SAMPLE_DOC_NAME);
    await expect(drawer).toContainText("相关度");
    await expect(drawer).toContainText(/0\.\d{3}/);
  });

  test("新建会话并提问后，该轮出现在会话列表里，可点回历史", async ({ page, seededKb }) => {
    // 会话标题 = 首轮提问的前 20 字（后端 append_round），dev 数据目录是共享且
    // 长期存在的，所以带一个短后缀让标题唯一，选择器才不会有歧义。
    const question = `${SAMPLE_QUESTION}#${Date.now().toString(36).slice(-5)}`;

    await gotoPortal(page);
    await selectKbInTopbar(page, seededKb.name);
    await askQuestion(page, question);
    await expect(citationsBadge(page)).toBeVisible({ timeout: ANSWER_TIMEOUT });

    // 侧栏要等下一次列表刷新才有标题：新建会话发生在首轮落库之前，前端那一刻
    // 拉到的标题还是默认「新会话」。真实用户第二天打开应用就会看到——这里用
    // "重新载入页面"制造同样的刷新，而不是等固定时间。
    await gotoPortal(page);
    await selectKbInTopbar(page, seededKb.name);

    const entry = page
      .locator('[data-tour="session-sidebar"]')
      .getByRole("button", { name: question });
    await expect(entry).toBeVisible();

    // 点回这个会话：历史里这一轮还在（用户提问 + 助手回答 + 落库的引用）
    await entry.click();
    await expect(chatMessages(page).first()).toContainText(question);
    await expect(answerBody(page)).toHaveText(/\S/);
    await expect(citationsBadge(page)).toBeVisible();
  });
});
