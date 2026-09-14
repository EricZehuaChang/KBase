// 知识库与文档（管理端工作台）：建库 → 上传 → 等到状态就绪 → 列表里看得见。
// 这是"页面对接真实摄取链路"的冒烟：建库/上传/状态轮询/表格渲染，任何一环断了
// 用户就导不进文档。
import {
  createKbViaUi,
  docRows,
  expect,
  gotoAdmin,
  openKbFromGrid,
  SAMPLE_DOC_NAME,
  test,
  uniqueKbName,
  uploadSampleDocAndWaitReady,
} from "./fixtures";

test.describe("知识库与文档（工作台）", () => {
  test("新建知识库 → 上传 Markdown → 状态轮询到就绪", async ({ page }) => {
    const name = uniqueKbName();
    await createKbViaUi(page, name);

    // 建完自动进详情页：标题是库名，且新库还没有文档
    await expect(page.getByRole("heading", { name })).toBeVisible();
    await expect(page.getByText("暂无文档，拖拽或选择文件开始上传")).toBeVisible();

    // 上传走 UploadZone 的真实 file input；等待只在"状态变成就绪"上等
    await uploadSampleDocAndWaitReady(page);

    await expect(docRows(page)).toHaveCount(1);
    await expect(docRows(page)).toContainText(SAMPLE_DOC_NAME);
    await expect(docRows(page)).toContainText("就绪");
  });

  test("工作台卡片显示文档数，可进入详情并返回列表", async ({ page, seededKb }) => {
    await gotoAdmin(page);

    const card = page.locator('[data-tour="kb-card"]').filter({ hasText: seededKb.name });
    await expect(card).toContainText("1 篇文档");

    await card.click();
    await expect(page).toHaveURL(/[?&]kb=/);
    await expect(page.getByRole("heading", { name: seededKb.name })).toBeVisible();

    // 返回列表：回到网格（卡片 + 新建入口都在）
    await page.locator('[data-tour="kb-back"]').click();
    await expect(page.getByRole("button", { name: "新建知识库" })).toBeVisible();
    await expect(page).not.toHaveURL(/[?&]kb=/);
  });

  test("文档列表页显示刚上传的文档名与状态", async ({ page, seededKb }) => {
    await openKbFromGrid(page, seededKb.name);

    await expect(page.getByRole("columnheader", { name: "文件名" })).toBeVisible();
    await expect(docRows(page)).toHaveCount(1);
    await expect(docRows(page)).toContainText(SAMPLE_DOC_NAME);
    await expect(docRows(page).filter({ hasText: "就绪" })).toHaveCount(1);
  });
});
