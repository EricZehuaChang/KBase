import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ref } from "vue";
import { useJob } from "../useJob";

function jobResponse(status: string, id = "job-1") {
  return new Response(JSON.stringify({
    id, kb_id: "kb-1", type: "proposal", status,
    params: null, progress: { steps: [] }, artifact_path: null,
    error: null, provider: null, created_at: "t", updated_at: "t",
  }), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("useJob", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    globalThis.fetch = originalFetch;
  });

  it("轮询 3s 一次，直到 status=done 才停止", async () => {
    const statuses = ["running", "running", "done"];
    let call = 0;
    const fetchMock = vi.fn(async () => jobResponse(statuses[Math.min(call++, statuses.length - 1)]));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const jobId = ref<string | undefined>("job-1");
    const { job, start, stopPolling } = useJob(jobId);

    await start();
    expect(job.value?.status).toBe("running");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(3000);
    expect(job.value?.status).toBe("running");
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(3000);
    expect(job.value?.status).toBe("done");
    expect(fetchMock).toHaveBeenCalledTimes(3);

    // 已停止：再推进时间不应有新请求
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(3);

    stopPolling();
  });

  it("done_with_errors 与 failed 均视为终态，立即停止（不发起第二次请求）", async () => {
    for (const status of ["done_with_errors", "failed"]) {
      const fetchMock = vi.fn(async () => jobResponse(status));
      globalThis.fetch = fetchMock as unknown as typeof fetch;

      const jobId = ref<string | undefined>("job-1");
      const { job, start } = useJob(jobId);
      await start();
      expect(job.value?.status).toBe(status);

      await vi.advanceTimersByTimeAsync(10000);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    }
  });

  it("stopPolling 可在卸载前手动调用，阻止后续轮询触发状态写入", async () => {
    const fetchMock = vi.fn(async () => jobResponse("running"));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const jobId = ref<string | undefined>("job-1");
    const { start, stopPolling } = useJob(jobId);
    await start();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    stopPolling();
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("请求失败时记录 error 并停止轮询", async () => {
    const fetchMock = vi.fn(async () => new Response("boom", { status: 500 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const jobId = ref<string | undefined>("job-1");
    const { job, error, start } = useJob(jobId);
    await start();

    expect(job.value).toBeNull();
    expect(error.value).toBeTruthy();

    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
