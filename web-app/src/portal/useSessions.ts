// 会话侧栏数据层（M5-1 F2）：分页列表 + 重命名/删除的乐观更新。
//
// 乐观更新的取舍：重命名/删除都是用户在侧栏里的高频小动作，等服务端确认
// 再刷新列表会有明显的"点了没反应"延迟感（尤其连续操作多个会话时）。
// 这里改成"先改本地状态、再发请求，失败就把这一条精确回滚"——不是整页
// 重新拉取列表（那样会连带丢失同一时刻其他并发操作的乐观状态），只回滚
// 被这次操作影响的那一条。
import { ref, type Ref } from "vue";
import {
  listConvs, renameConv, deleteConv, type Conversation,
} from "@/lib/api";
import { appendConversationPage } from "@/lib/chat-utils";

const PAGE_SIZE = 30;

export function useSessions(kbId: Ref<string | undefined>) {
  const items = ref<Conversation[]>([]);
  const hasMore = ref(false);

  async function refresh(): Promise<void> {
    if (!kbId.value) {
      items.value = [];
      hasMore.value = false;
      return;
    }
    const page = await listConvs({ kbId: kbId.value, limit: PAGE_SIZE, offset: 0 });
    const acc = appendConversationPage([], page);
    items.value = acc.items;
    hasMore.value = acc.hasMore;
  }

  async function loadMore(): Promise<void> {
    if (!kbId.value) return;
    const page = await listConvs({
      kbId: kbId.value, limit: PAGE_SIZE, offset: items.value.length,
    });
    const acc = appendConversationPage(items.value, page);
    items.value = acc.items;
    hasMore.value = acc.hasMore;
  }

  /** 重命名：本地先改标题，失败时把这一条精确改回旧标题（不是整页重拉）。
   * 调用方（SessionSidebar）负责 catch 后 toast 报错——这里只管状态本身。 */
  async function rename(id: string, title: string): Promise<void> {
    const idx = items.value.findIndex((c) => c.id === id);
    if (idx === -1) return;
    const prevTitle = items.value[idx].title;
    items.value[idx] = { ...items.value[idx], title };
    try {
      await renameConv(id, title);
    } catch (err) {
      items.value[idx] = { ...items.value[idx], title: prevTitle };
      throw err;
    }
  }

  /** 删除：本地先移除，失败时插回原位置（用下标而不是 push 到末尾——
   * 回滚后会话应该出现在原来的分组位置，不是突兀地跳到列表最后）。 */
  async function remove(id: string): Promise<void> {
    const idx = items.value.findIndex((c) => c.id === id);
    if (idx === -1) return;
    const removed = items.value[idx];
    items.value = [...items.value.slice(0, idx), ...items.value.slice(idx + 1)];
    try {
      await deleteConv(id);
    } catch (err) {
      items.value = [...items.value.slice(0, idx), removed, ...items.value.slice(idx)];
      throw err;
    }
  }

  return { items, hasMore, refresh, loadMore, rename, remove };
}
