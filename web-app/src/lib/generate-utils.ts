// src/lib/generate-utils.ts —— 生成页纯函数（可测，不依赖 DOM/组件实例）。
// 大纲编辑器（OutlineEditor）的增删移位逻辑，均为不可变操作（返回新数组，
// 不修改入参），便于组件里配合 v-model 数组直接替换使用。

export interface OutlineSection {
  title: string;
  brief: string;
}

/** 在指定位置之后插入一个空白节（默认追加到末尾）。 */
export function addSection(sections: OutlineSection[], afterIndex?: number): OutlineSection[] {
  const next = [...sections];
  const blank: OutlineSection = { title: "", brief: "" };
  const pos = afterIndex === undefined ? next.length : afterIndex + 1;
  next.splice(pos, 0, blank);
  return next;
}

/** 删除指定下标的节；越界不做任何改动（返回等值新数组）。 */
export function removeSection(sections: OutlineSection[], index: number): OutlineSection[] {
  if (index < 0 || index >= sections.length) return [...sections];
  const next = [...sections];
  next.splice(index, 1);
  return next;
}

/** 上下移动指定节；已在边界（首节上移/末节下移）时原样返回，不越界。 */
export function moveSection(
  sections: OutlineSection[],
  index: number,
  direction: "up" | "down",
): OutlineSection[] {
  const target = direction === "up" ? index - 1 : index + 1;
  if (index < 0 || index >= sections.length || target < 0 || target >= sections.length) {
    return [...sections];
  }
  const next = [...sections];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/** job 状态 → 展示信息（标签 + 语义色 class），供任务历史徽章使用；
 * 未知状态兜底为中性灰色展示原始值（与 kb-utils.statusBadge 同策略）。 */
export function jobStatusBadge(status: string): { label: string; class: string } {
  const map: Record<string, { label: string; class: string }> = {
    pending: { label: "等待中", class: "bg-[var(--surface-2)] text-[var(--text-2)]" },
    running: { label: "进行中", class: "bg-[var(--warn-weak)] text-[var(--warn)]" },
    done: { label: "已完成", class: "bg-[var(--ok-weak)] text-[var(--ok)]" },
    done_with_errors: { label: "部分完成", class: "bg-[var(--warn-weak)] text-[var(--warn)]" },
    failed: { label: "失败", class: "bg-[var(--err-weak)] text-[var(--err)]" },
  };
  return map[status] ?? { label: status, class: "bg-[var(--surface-2)] text-[var(--text-2)]" };
}

/** job 类型 → 中文标签。 */
export function jobTypeLabel(type: string): string {
  return type === "proposal" ? "方案生成" : type === "digest" ? "定期汇编" : type;
}

/** 该 job 是否已到达可加载产物的终态（含失败步骤但仍有产出的 done_with_errors）。 */
export function jobHasArtifact(status: string): boolean {
  return status === "done" || status === "done_with_errors";
}
