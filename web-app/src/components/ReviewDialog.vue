<script setup lang="ts">
// VLM 识别校验对话框（F）：左侧原图预览、右侧识别 Markdown 可编辑——
// 管理员对照修正（VLM 幻觉在此拦截）后「确认入库」，此刻才分块向量化。
//
// 布局是"校对工作台"式：头部（标题+说明）/ 双栏主体（各带工具条）/ 底部
// 操作栏。三处可调：①右下角手柄拖拽改窗口大小（首次拖拽把窗口从居中
// translate 定位钉到当前位置，之后宽高跟手；尺寸跨多次打开保留，位置
// 每次打开回居中——防止上次拖到角落这次开在屏外）；②中缝拖拽改左右
// 分栏比例（宽图给左边多点，长文给右边多点）；③原图缩放（适应窗口/
// 实际大小/加减档），看清流程图小字是校对的关键。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { CheckCircle2, Maximize2, Minus, Plus } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import {
  docOriginalUrl, getDocContent, reviewDocument, type DocumentItem,
} from "@/lib/api";

const props = defineProps<{ open: boolean; doc: DocumentItem | null }>();
const emit = defineEmits<{ "update:open": [value: boolean]; approved: [] }>();
const { t } = useI18n();

const markdown = ref("");
const loading = ref(false);
const saving = ref(false);
const loadError = ref<string | null>(null);

watch(() => props.open, async (isOpen) => {
  if (!isOpen || !props.doc) {
    pinned.value = null;         // 关闭即解除钉定，下次打开回居中（尺寸保留）
    return;
  }
  zoom.value = "fit";
  loading.value = true;
  loadError.value = null;
  markdown.value = "";
  try {
    markdown.value = (await getDocContent(props.doc.id)).markdown;
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
});

async function approve() {
  if (!props.doc || !markdown.value.trim()) return;
  saving.value = true;
  try {
    await reviewDocument(props.doc.id, markdown.value);
    toast.success(t("review.approved"));
    emit("update:open", false);
    emit("approved");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}

// ---- 窗口拖拽缩放 ----

const dlgW = ref<number | null>(null);          // null=默认尺寸（CSS min() 表达式）
const dlgH = ref<number | null>(null);
const pinned = ref<{ left: number; top: number } | null>(null);

const dialogStyle = computed(() => ({
  width: dlgW.value ? `${dlgW.value}px` : "min(72rem, calc(100vw - 3rem))",
  height: dlgH.value ? `${dlgH.value}px` : "min(85vh, 56rem)",
  maxWidth: "calc(100vw - 1.5rem)",
  maxHeight: "calc(100vh - 1.5rem)",
  // 同时压 transform 与 translate：Tailwind v4 的 -translate-x-1/2 生成的是
  // 独立属性 translate，只写 transform:none 盖不住，钉定会残留 -50% 平移
  ...(pinned.value
    ? { left: `${pinned.value.left}px`, top: `${pinned.value.top}px`,
        transform: "none", translate: "none" }
    : {}),
}));

function startResize(e: PointerEvent) {
  const el = (e.currentTarget as HTMLElement).closest<HTMLElement>(
    '[data-slot="dialog-content"]');
  if (!el) return;
  const r = el.getBoundingClientRect();
  // 钉住当前左上角再改宽高——保持 translate 居中的话，右下角只会以鼠标
  // 一半的速度移动（中心锚定，两边同时长），跟手感是错的
  pinned.value = { left: r.left, top: r.top };
  const move = (ev: PointerEvent) => {
    dlgW.value = Math.min(Math.max(560, ev.clientX - r.left),
      window.innerWidth - r.left - 12);
    dlgH.value = Math.min(Math.max(420, ev.clientY - r.top),
      window.innerHeight - r.top - 12);
  };
  const stop = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", stop);
    window.removeEventListener("pointercancel", stop);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", stop, { once: true });
  window.addEventListener("pointercancel", stop, { once: true });
  e.preventDefault();
  cleanupFns.push(stop);
}

// ---- 左右分栏比例拖拽 ----

const leftPct = ref(50);

function startDivider(e: PointerEvent) {
  const container = (e.currentTarget as HTMLElement).parentElement;
  if (!container) return;
  const r = container.getBoundingClientRect();
  const move = (ev: PointerEvent) => {
    leftPct.value = Math.min(75, Math.max(25, (ev.clientX - r.left) / r.width * 100));
  };
  const stop = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", stop);
    window.removeEventListener("pointercancel", stop);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", stop, { once: true });
  window.addEventListener("pointercancel", stop, { once: true });
  e.preventDefault();
  cleanupFns.push(stop);
}

// 对话框中途销毁（Esc 关闭等）时兜底摘掉 window 监听
const cleanupFns: (() => void)[] = [];
onBeforeUnmount(() => { for (const fn of cleanupFns) fn(); });

// ---- 原图缩放 ----

const zoom = ref<"fit" | number>("fit");
const naturalW = ref(0);

function onImgLoad(e: Event) {
  naturalW.value = (e.target as HTMLImageElement).naturalWidth;
}

const zoomLabel = computed(() =>
  zoom.value === "fit" ? t("review.zoom_fit") : `${Math.round(zoom.value * 100)}%`);

function zoomBy(factor: number) {
  const cur = zoom.value === "fit" ? 1 : zoom.value;
  zoom.value = Math.min(4, Math.max(0.25, cur * factor));
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent
      class="flex flex-col gap-0 overflow-hidden p-0"
      :style="dialogStyle"
    >
      <!-- 头部：标题 + 一句话说明（右上 X 由 DialogContent 自带） -->
      <div class="shrink-0 border-b border-[var(--border)] py-3 pl-5 pr-14">
        <DialogTitle class="text-[15px] font-semibold leading-snug">
          {{ t("review.title", { name: doc?.filename }) }}
        </DialogTitle>
        <DialogDescription class="mt-0.5 text-xs text-[var(--text-3)]">
          {{ t("review.desc") }}
        </DialogDescription>
      </div>

      <!-- 主体：左原图 | 可拖中缝 | 右识别稿 -->
      <div class="flex min-h-0 flex-1">
        <section
          class="flex min-w-0 flex-col"
          :style="{ width: `${leftPct}%` }"
        >
          <div class="flex h-9 shrink-0 items-center justify-between border-b border-[var(--border)] bg-[var(--surface)] pl-4 pr-2">
            <span class="text-xs font-medium text-[var(--text-2)]">{{ t("review.pane_image") }}</span>
            <div class="flex items-center gap-0.5">
              <button
                type="button"
                class="rounded p-1 text-[var(--text-3)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
                :title="t('review.zoom_out')" @click="zoomBy(1 / 1.25)"
              >
                <Minus class="size-3.5" />
              </button>
              <span class="min-w-14 text-center text-[11px] tabular-nums text-[var(--text-3)]">
                {{ zoomLabel }}
              </span>
              <button
                type="button"
                class="rounded p-1 text-[var(--text-3)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
                :title="t('review.zoom_in')" @click="zoomBy(1.25)"
              >
                <Plus class="size-3.5" />
              </button>
              <button
                type="button"
                class="rounded p-1 text-[var(--text-3)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
                :title="t('review.zoom_fit')" @click="zoom = 'fit'"
              >
                <Maximize2 class="size-3.5" />
              </button>
            </div>
          </div>
          <div class="flex min-h-0 flex-1 overflow-auto bg-[var(--surface-2)] p-3">
            <img
              v-if="doc"
              :src="docOriginalUrl(doc.id, { inline: true })"
              :alt="doc.filename"
              class="m-auto rounded-sm shadow-sm"
              :class="zoom === 'fit' ? 'max-h-full max-w-full object-contain'
                : 'max-w-none shrink-0'"
              :style="zoom !== 'fit' && naturalW
                ? { width: `${naturalW * zoom}px` } : undefined"
              @load="onImgLoad"
            />
          </div>
        </section>

        <div
          class="w-1 shrink-0 cursor-col-resize bg-[var(--border)] transition-colors hover:bg-[var(--accent)]"
          :title="t('review.drag_divider')"
          @pointerdown="startDivider"
        />

        <section class="flex min-w-0 flex-1 flex-col">
          <div class="flex h-9 shrink-0 items-center border-b border-[var(--border)] bg-[var(--surface)] px-4">
            <span class="text-xs font-medium text-[var(--text-2)]">{{ t("review.pane_markdown") }}</span>
          </div>
          <p v-if="loading" class="p-4 text-sm text-[var(--text-3)]">{{ t("review.loading") }}</p>
          <p v-else-if="loadError" class="p-4 text-sm text-[var(--err)]">⚠️ {{ loadError }}</p>
          <textarea
            v-else
            v-model="markdown"
            class="min-h-0 flex-1 resize-none bg-[var(--surface)] px-4 py-3 font-mono text-[13px] leading-relaxed text-[var(--text)] outline-none"
          />
        </section>
      </div>

      <!-- 底部操作栏 -->
      <div class="flex shrink-0 items-center justify-end gap-2 border-t border-[var(--border)] px-5 py-3">
        <Button variant="outline" @click="emit('update:open', false)">{{ t("review.later") }}</Button>
        <Button :disabled="saving || loading || !markdown.trim()" @click="approve">
          <CheckCircle2 class="size-3.5" />
          {{ t("review.approve") }}
        </Button>
      </div>

      <!-- 右下角窗口缩放手柄（三道斜纹提示可拖） -->
      <div
        class="absolute bottom-0 right-0 flex size-4 cursor-nwse-resize items-end justify-end p-0.5 text-[var(--text-3)]"
        :title="t('review.resize')"
        @pointerdown="startResize"
      >
        <svg viewBox="0 0 8 8" class="size-2.5" fill="none" stroke="currentColor" stroke-width="1">
          <path d="M7 1L1 7M7 4L4 7M7 7L7 7" />
        </svg>
      </div>
    </DialogContent>
  </Dialog>
</template>
