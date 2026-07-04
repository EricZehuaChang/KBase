// src/composables/useKbDocs.ts —— 单个知识库的文档列表加载 + 状态轮询。
// 存在 parsing/pending/pending_ocr 状态时每 3s 刷新一次，全部 ready/failed 后
// 自动停止；调用方须在 onBeforeUnmount 里调用 stopPolling，避免离开页面/
// 切换路由后定时器继续对已卸载组件的响应式状态写入。
import { ref, watch, type Ref } from "vue";
import { listDocs, type DocumentItem } from "@/lib/api";
import { hasPollingStatus } from "@/lib/kb-utils";

export function useKbDocs(kbId: Ref<string | undefined>) {
  const docs = ref<DocumentItem[]>([]);
  const loading = ref(false);
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function loadDocs() {
    if (!kbId.value) return;
    loading.value = true;
    try {
      docs.value = await listDocs(kbId.value);
    } finally {
      loading.value = false;
    }
  }

  function ensurePolling() {
    if (pollTimer) return;
    if (!hasPollingStatus(docs.value)) return;
    pollTimer = setInterval(async () => {
      await loadDocs();
      if (!hasPollingStatus(docs.value)) stopPolling();
    }, 3000);
  }

  watch(docs, () => ensurePolling(), { deep: true });

  watch(kbId, async (id) => {
    stopPolling();
    docs.value = [];
    if (id) {
      await loadDocs();
      ensurePolling();
    }
  }, { immediate: true });

  return { docs, loading, loadDocs, stopPolling };
}
