<script setup lang="ts">
// 消息流：用户消息右对齐浅底圆角块；助手消息为流动排版（无气泡），
// 流式中显示"思考中…"占位直到首个字符到达；引用角标 [n] 渲染为 <sup> 包裹
// 的可点击 chip（renderWithChips 纯函数切分，代码块/行内代码免疫见
// chat-utils.ts），点击先弹小 popover（文档名+片段），popover 里"查看原文"
// 再打开 CitationDrawer 抽屉看全文——两级展开，popover 快速预览，抽屉给
// 需要细看的人。悬浮操作条：复制、重新提问（把这条回答对应的提问回填进
// 输入框，交给父组件决定是否清空重发）。
import { ref } from "vue";
import { toast } from "vue-sonner";
import { Copy, ExternalLink, Image as ImageIcon, RotateCcw, ThumbsDown, ThumbsUp } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { renderWithChips } from "@/lib/chat-utils";
import type { ChatMessage } from "@/composables/useChat";
import type { Citation } from "@/lib/api";

const props = defineProps<{ messages: ChatMessage[] }>();
const emit = defineEmits<{
  openCitation: [index: number, messageId: string];
  reask: [question: string];
  // M6-4 反馈：父组件负责把 local id 解析成服务端消息 id 后提交
  feedback: [messageId: string, rating: 1 | -1];
}>();

// 本地已选反馈态（messageId → rating）：即时高亮，不等服务端回包；
// 会话切换时组件随消息列表重建，无需持久化。
const localFeedback = ref<Record<string, 1 | -1>>({});

function sendFeedback(messageId: string, rating: 1 | -1) {
  localFeedback.value = { ...localFeedback.value, [messageId]: rating };
  emit("feedback", messageId, rating);
}

function segmentsOf(content: string) {
  return renderWithChips(content);
}

/** 多模态回答（图片一期）：收集这条消息全部引用附带的插图，按 url 去重
 * （多条引用命中同一页时图片只展示一次），并带上来源文档名/页码给
 * 灯箱标题用。空数组=不渲染图片区。 */
interface AnswerImage { url: string; name: string; docName: string; page: number | null }

function imagesOf(message: ChatMessage): AnswerImage[] {
  const seen = new Set<string>();
  const out: AnswerImage[] = [];
  for (const c of message.citations) {
    for (const img of c.images ?? []) {
      if (!seen.has(img.url)) {
        seen.add(img.url);
        out.push({ url: img.url, name: img.name,
                   docName: c.doc_name, page: c.page ?? null });
      }
    }
  }
  return out;
}

// 灯箱：点缩略图站内放大看（比裸开新标签页体验好），保留"新标签页打开
// 原图"出口给需要另存/缩放的用户。
const lightbox = ref<AnswerImage | null>(null);

/** 按角标编号在这条消息自己的 citations 里查找——渲染层的越界兜底：
 * renderWithChips 纯函数不知道 citations 数组内容，找不到时（模型编号超出
 * 实际引用数、或历史消息 citations 载荷缺失）返回 undefined，popover 据此
 * 展示"引用不存在"而不是渲染一堆 undefined 字段。 */
function citationFor(message: ChatMessage, index: number): Citation | undefined {
  return message.citations.find((c) => c.index === index);
}

function lastQuestionFor(index: number): string {
  for (let i = index - 1; i >= 0; i -= 1) {
    if (props.messages[i].role === "user") return props.messages[i].content;
  }
  return "";
}

async function copyMessage(content: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(content);
    } else {
      // 回退路径：非安全上下文（如局域网 http 部署）拿不到 navigator.clipboard，
      // execCommand 虽已废弃但兼容性仍覆盖这类场景，作为兜底而不是唯一实现。
      const ta = document.createElement("textarea");
      ta.value = content;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    toast.success("已复制到剪贴板");
  } catch {
    toast.error("复制失败，请手动选择文本");
  }
}

function reask(index: number) {
  emit("reask", lastQuestionFor(index));
}
</script>

<template>
  <ol class="flex flex-col gap-6" aria-label="对话消息列表">
    <li
      v-for="(message, index) in messages"
      :key="message.id"
      class="flex"
      :class="message.role === 'user' ? 'justify-end' : 'justify-start'"
    >
      <div
        v-if="message.role === 'user'"
        class="max-w-[70%] rounded-2xl rounded-br-md bg-[var(--accent-weak)] px-4 py-2.5 text-[var(--text)]"
      >
        {{ message.content }}
      </div>

      <div v-else class="group/msg w-full max-w-[80ch]">
        <div v-if="message.streaming && !message.content" class="text-[var(--text-3)]">
          思考中…
        </div>
        <div v-else class="whitespace-pre-wrap text-[var(--text)]">
          <template v-for="(seg, si) in segmentsOf(message.content)" :key="si">
            <span v-if="seg.type === 'text'">{{ seg.text }}</span>
            <sup v-else class="mx-0.5 align-middle">
              <Popover>
                <PopoverTrigger as-child>
                  <button
                    type="button"
                    class="inline-flex size-4 items-center justify-center rounded-full bg-[var(--accent-weak)] text-[10px] font-medium text-[var(--accent-text)] hover:bg-[var(--accent)] hover:text-[var(--surface)]"
                    :aria-label="`查看引用 ${seg.index}`"
                  >
                    {{ seg.index }}
                  </button>
                </PopoverTrigger>
                <PopoverContent class="w-80 text-sm" side="top">
                  <template v-if="citationFor(message, seg.index)">
                    <div class="font-medium text-[var(--text)]">
                      {{ citationFor(message, seg.index)!.doc_name }}
                    </div>
                    <p class="mt-1 line-clamp-3 text-xs leading-relaxed text-[var(--text-2)]">
                      {{ citationFor(message, seg.index)!.snippet }}
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      class="mt-2"
                      @click="emit('openCitation', seg.index, message.id)"
                    >
                      查看原文
                    </Button>
                  </template>
                  <p v-else class="text-xs text-[var(--text-3)]">
                    引用 [{{ seg.index }}] 不存在（可能是历史消息或模型编号有误）
                  </p>
                </PopoverContent>
              </Popover>
            </sup>
          </template>
        </div>

        <!-- 多模态回答：引用命中页的文档插图区（标签行+卡片缩略图+灯箱）。
        懒加载防长会话一次性拉几十张图；最大高度限制防大图撑爆气泡。 -->
        <div v-if="!message.streaming && imagesOf(message).length" class="mt-3">
          <div class="mb-1.5 flex items-center gap-1.5 text-xs text-[var(--text-3)]">
            <ImageIcon class="size-3.5" />
            引用插图 · {{ imagesOf(message).length }} 张（点击放大）
          </div>
          <div class="flex flex-wrap gap-2">
            <button
              v-for="img in imagesOf(message)"
              :key="img.url"
              type="button"
              class="group/img relative block overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] shadow-sm transition-all duration-150 hover:-translate-y-0.5 hover:border-[var(--accent)] hover:shadow-md"
              :aria-label="`放大查看 ${img.docName} 第${img.page}页插图`"
              @click="lightbox = img"
            >
              <img
                :src="img.url"
                :alt="img.name"
                loading="lazy"
                class="max-h-40 w-auto max-w-60 object-contain"
              />
              <span
                class="absolute inset-x-0 bottom-0 truncate bg-black/55 px-2 py-1 text-left text-[11px] text-white opacity-0 transition-opacity group-hover/img:opacity-100"
              >
                {{ img.docName }}{{ img.page ? ` · 第${img.page}页` : "" }}
              </span>
            </button>
          </div>
        </div>

        <div
          v-if="!message.streaming && message.content"
          class="mt-2 flex items-center gap-3 text-sm text-[var(--text-3)]"
        >
          <span v-if="message.citations.length" class="rounded-full bg-[var(--surface-2)] px-2 py-0.5">
            {{ message.citations.length }} 条引用
          </span>
          <span v-if="message.stopped" class="text-[var(--warn)]">已停止</span>
          <!-- hover 才"看见"的操作条：常驻会让消息区显得拥挤，且这两个动作都
          不是高频操作。用 opacity 而不是 hidden/flex（display 切换）保留布局
          与可达性——键盘 Tab 聚焦到按钮、触屏点击、自动化测试的坐标点击都
          不依赖真的把鼠标悬停在这条消息上，见 SessionSidebar.vue 同款注释。 -->
          <span class="flex items-center gap-3 opacity-0 focus-within:opacity-100 group-hover/msg:opacity-100">
            <Button variant="ghost" size="sm" @click="copyMessage(message.content)">
              <Copy class="size-3.5" />
              复制
            </Button>
            <Button variant="ghost" size="sm" @click="reask(index)">
              <RotateCcw class="size-3.5" />
              重新提问
            </Button>
          </span>
          <!-- M6-4 反馈：与 hover 操作条分离——未选时随 hover 出现，
          已选的一侧常亮（用户要能看到自己评过什么） -->
          <span
            class="flex items-center gap-1 focus-within:opacity-100 group-hover/msg:opacity-100"
            :class="localFeedback[message.id] ? 'opacity-100' : 'opacity-0'"
          >
            <button
              type="button"
              class="rounded p-1 hover:text-[var(--ok)]"
              :class="localFeedback[message.id] === 1 ? 'text-[var(--ok)]' : ''"
              aria-label="点赞该回答"
              @click="sendFeedback(message.id, 1)"
            >
              <ThumbsUp class="size-3.5" />
            </button>
            <button
              type="button"
              class="rounded p-1 hover:text-[var(--err)]"
              :class="localFeedback[message.id] === -1 ? 'text-[var(--err)]' : ''"
              aria-label="点踩该回答"
              @click="sendFeedback(message.id, -1)"
            >
              <ThumbsDown class="size-3.5" />
            </button>
          </span>
        </div>
      </div>
    </li>
  </ol>

  <!-- 插图灯箱：站内放大查看，标题带来源文档/页码，保留新标签页原图出口 -->
  <Dialog :open="!!lightbox" @update:open="(v) => { if (!v) lightbox = null; }">
    <DialogContent class="max-w-3xl">
      <div class="flex items-center justify-between gap-3 pr-6 text-sm">
        <span class="truncate font-medium">
          {{ lightbox?.docName }}{{ lightbox?.page ? ` · 第${lightbox.page}页插图` : "" }}
        </span>
        <a
          v-if="lightbox"
          :href="lightbox.url"
          target="_blank"
          rel="noopener"
          class="flex shrink-0 items-center gap-1 text-xs text-[var(--accent-text)] hover:underline"
        >
          <ExternalLink class="size-3.5" />
          新标签页打开原图
        </a>
      </div>
      <img
        v-if="lightbox"
        :src="lightbox.url"
        :alt="lightbox.name"
        class="max-h-[70vh] w-full rounded-[var(--radius-ctl)] object-contain"
      />
    </DialogContent>
  </Dialog>
</template>
