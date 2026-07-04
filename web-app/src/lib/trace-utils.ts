// src/lib/trace-utils.ts —— 检索分析页纯函数（可测，不依赖 DOM/组件实例）。
// 后端 trace 各阶段均为 [chunk_id, score][]，已按相关性降序排列。

/** 单个 chunk_id 相对 fused 名次的变化标注：
 * ↑n = 相比 fused 名次上升 n 位；↓n = 下降 n 位；新进 = fused 中不存在（如
 * 关键词路独有候选，只在融合/重排后出现）；— = 名次不变。 */
export type RankChange = `↑${number}` | `↓${number}` | "新进" | "—";

/** 计算 reranked 相对 fused 的名次变化，key 为 chunk_id。名次从 1 开始计数
 * （数组下标 + 1），与 UI 展示的"第 n 位"语义一致。 */
export function rankChanges(
  fused: [string, number][],
  reranked: [string, number][],
): Record<string, RankChange> {
  const fusedRank = new Map<string, number>();
  fused.forEach(([id], i) => fusedRank.set(id, i + 1));

  const result: Record<string, RankChange> = {};
  reranked.forEach(([id], i) => {
    const newRank = i + 1;
    const oldRank = fusedRank.get(id);
    if (oldRank === undefined) {
      result[id] = "新进";
    } else if (oldRank > newRank) {
      result[id] = `↑${oldRank - newRank}`;
    } else if (oldRank < newRank) {
      result[id] = `↓${newRank - oldRank}`;
    } else {
      result[id] = "—";
    }
  });
  return result;
}

/** chunk_id 短前缀（8 字符），用于表格紧凑展示；短于 8 字符原样返回。 */
export function shortChunkId(chunkId: string): string {
  return chunkId.slice(0, 8);
}

/** ?q= 变化时是否应重新执行检索。newQ 来自 route.query.q（vue-router 类型为
 * string | string[] | null | undefined，重复 query 参数会成为数组）：仅当它是
 * 非空字符串且与当前输入框内容不同时才触发——同值导航、清空参数、数组形态
 * 都不触发。抽成纯函数以便 vitest 直接断言（AnalysisView 的 watch 经由它守卫）。 */
export function shouldRerunForQuery(newQ: unknown, currentQ: string): newQ is string {
  return typeof newQ === "string" && newQ !== "" && newQ !== currentQ;
}
