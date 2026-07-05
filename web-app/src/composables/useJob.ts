// src/composables/useJob.ts —— 单个 job 的轮询加载。status 到达终态
// （done/done_with_errors/failed）后自动停止轮询；调用方须在 onBeforeUnmount
// 里调用 stopPolling，避免离开页面后定时器继续对已卸载组件的响应式状态写入
// （与 useKbDocs 同模式）。
import { shallowRef, ref, type Ref } from "vue";
import { getJob, type Job } from "@/lib/api";

const TERMINAL_STATUSES = new Set(["done", "done_with_errors", "failed"]);

export function isTerminalStatus(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}

export function useJob(jobId: Ref<string | undefined>) {
  // shallowRef：Job 含 params: Record<string, unknown>，深度 UnwrapRef 对泛型
  // 索引签名递归展开在当前 TS/Vue 版本组合下会把 .value 类型坍缩成 never
  // （vue-tsc 误报 TS2339）。job 整体替换式更新（无需响应式深层追踪字段），
  // shallowRef 语义上也更贴切。
  const job = shallowRef<Job | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function loadJob() {
    if (!jobId.value) return;
    loading.value = true;
    try {
      job.value = await getJob(jobId.value);
      error.value = null;
      if (job.value && isTerminalStatus(job.value.status)) stopPolling();
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err);
      stopPolling();
    } finally {
      loading.value = false;
    }
  }

  function shouldKeepPolling(): boolean {
    const current = job.value;
    return !!current && !isTerminalStatus(current.status);
  }

  async function start() {
    stopPolling();
    job.value = null;
    error.value = null;
    if (!jobId.value) return;
    await loadJob();
    if (shouldKeepPolling()) {
      pollTimer = setInterval(loadJob, 3000);
    }
  }

  return { job, loading, error, start, stopPolling };
}
