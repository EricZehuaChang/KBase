<script setup lang="ts">
// KB 配置 Dialog：chunk_size/chunk_overlap 数字输入 + enrich Switch +
// 检索策略区块（M6-1.5）。打开时按 props.kb.config 回填（无配置则用后端
// 默认值 512/64/关闭，见 kbase/plugins/chunkers/structure.py 构造函数默认值）；
// 保存调用 putKbConfig 并在成功后 emit saved，父组件负责刷新 KB 列表。
//
// 检索策略三态语义："default"=跟随全局默认（保存时不写该键，后端解析层
// 继承全局配置）；"on"/"off"=本库显式开关。只能关闭部署已安装的能力，
// 开不出部署里没有的路（服务端 retriever 按实例存在性做最终门控）。
import { ref, watch } from "vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  putKbConfig, listEmbedders, rebindEmbedder, currentRole,
  type Kb, type KbRetrievalConfig, type EmbeddersCatalog,
} from "@/lib/api";
import { canAdminister } from "@/lib/auth-utils";
import { computed } from "vue";

const props = defineProps<{ open: boolean; kb: Kb | null }>();
const emit = defineEmits<{ "update:open": [value: boolean]; saved: [] }>();

// ---- 换绑向量模型（admin，重操作走确认区） ----
const isAdmin = computed(() => canAdminister(currentRole.value ?? ""));
const rebindOpen = ref(false);
const rebindTarget = ref("");
const rebinding = ref(false);
const catalog = ref<EmbeddersCatalog | null>(null);

const rebindOptions = computed(() => {
  if (!catalog.value) return [];
  const current = props.kb?.config?.embedder ?? "default";
  return [
    { id: "default", model: catalog.value.default.model },
    ...catalog.value.options,
  ].filter((o) => o.id !== current);
});

async function openRebind() {
  rebindOpen.value = true;
  rebindTarget.value = "";
  try {
    catalog.value = await listEmbedders();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

async function doRebind() {
  if (!props.kb || !rebindTarget.value) return;
  rebinding.value = true;
  try {
    await rebindEmbedder(props.kb.id, rebindTarget.value);
    toast.success("换绑成功，向量后台重建中（文档多时需等待片刻）");
    rebindOpen.value = false;
    emit("saved");                  // 父组件刷新 KB 列表（config 已变）
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    rebinding.value = false;
  }
}

const chunkSize = ref(512);
const chunkOverlap = ref(64);
const enrichEnabled = ref(false);
const saving = ref(false);

// ---- 检索策略（三态："default" | "on" | "off"；rewrite 为四态） ----
const hybridMode = ref("default");
const rerankMode = ref("default");
const rewriteMode = ref("default");     // default | off | conditional | always
const candidatesText = ref("");         // 空=跟随全局

function triState(v: boolean | null | undefined): string {
  return v == null ? "default" : v ? "on" : "off";
}

function fromTriState(v: string): boolean | undefined {
  return v === "default" ? undefined : v === "on";
}

// 每次打开都按当前 kb.config 重新回填，避免残留上一次编辑的未保存值
watch(() => props.open, (isOpen) => {
  if (!isOpen) return;
  const cfg = props.kb?.config;
  chunkSize.value = cfg?.chunk_size ?? 512;
  chunkOverlap.value = cfg?.chunk_overlap ?? 64;
  enrichEnabled.value = cfg?.enrich?.enabled ?? false;
  const r = cfg?.retrieval;
  hybridMode.value = triState(r?.hybrid);
  rerankMode.value = triState(r?.rerank);
  rewriteMode.value = r?.rewrite ?? "default";
  candidatesText.value = r?.candidates ? String(r.candidates) : "";
});

function buildRetrieval(): KbRetrievalConfig | undefined {
  const out: KbRetrievalConfig = {};
  const hybrid = fromTriState(hybridMode.value);
  const rerank = fromTriState(rerankMode.value);
  if (hybrid !== undefined) out.hybrid = hybrid;
  if (rerank !== undefined) out.rerank = rerank;
  if (rewriteMode.value !== "default") {
    out.rewrite = rewriteMode.value as KbRetrievalConfig["rewrite"];
  }
  const cand = parseInt(candidatesText.value, 10);
  if (!Number.isNaN(cand) && cand > 0) out.candidates = cand;
  // 全部跟随默认时不写 retrieval 段（"通用方式"），配置保持干净
  return Object.keys(out).length ? out : undefined;
}

async function save() {
  if (!props.kb) return;
  saving.value = true;
  try {
    await putKbConfig(props.kb.id, {
      chunk_size: chunkSize.value,
      chunk_overlap: chunkOverlap.value,
      enrich: { enabled: enrichEnabled.value },
      retrieval: buildRetrieval(),
    });
    toast.success("配置已保存");
    emit("update:open", false);
    emit("saved");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-h-[90vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>知识库配置</DialogTitle>
        <DialogDescription>分块与增强仅影响后续新上传文档；检索策略即时生效</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-4">
        <!-- 向量模型绑定（M5-2 建库定死 → 现支持显式换绑）：换绑=全库向量
             作废并按新模型后台重嵌入（基于存量文本，不重新解析/不产生OCR费），
             重操作走确认流程，admin 才见入口。 -->
        <div class="flex items-center justify-between text-sm">
          <span class="text-[var(--text-2)]">向量模型</span>
          <span class="flex items-center gap-2">
            <span class="text-[var(--text-3)]">{{ kb?.config?.embedder ?? "默认" }}</span>
            <button
              v-if="isAdmin && !rebindOpen"
              type="button"
              class="text-xs text-[var(--accent-text)] hover:underline"
              @click="openRebind"
            >
              更换…
            </button>
          </span>
        </div>
        <div v-if="rebindOpen" class="rounded-[var(--radius-ctl)] border border-[var(--warn)] bg-[var(--warn-weak)] p-3">
          <p class="mb-2 text-xs text-[var(--warn)]">
            换绑后该库全部向量作废，将按新模型后台重新嵌入（基于已解析文本，
            不重新解析文件）。文档多时耗时较长，期间检索结果可能不完整。
          </p>
          <div class="flex items-center gap-2">
            <Select v-model="rebindTarget">
              <SelectTrigger class="w-56"><SelectValue placeholder="选择新向量模型" /></SelectTrigger>
              <SelectContent>
                <SelectItem
                  v-for="opt in rebindOptions"
                  :key="opt.id"
                  :value="opt.id"
                >
                  {{ opt.id === "default" ? "默认" : opt.id }}（{{ opt.model }}）
                </SelectItem>
              </SelectContent>
            </Select>
            <Button size="sm" :disabled="rebinding || !rebindTarget" @click="doRebind">
              {{ rebinding ? "提交中…" : "确认换绑并重建" }}
            </Button>
            <Button size="sm" variant="outline" @click="rebindOpen = false">取消</Button>
          </div>
        </div>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">分块大小 chunk_size（64-4096）</span>
          <Input v-model.number="chunkSize" type="number" min="64" max="4096" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">分块重叠 chunk_overlap（0-512，需小于分块大小）</span>
          <Input v-model.number="chunkOverlap" type="number" min="0" max="512" />
        </label>
        <label class="flex items-center justify-between">
          <span class="text-sm text-[var(--text-2)]">上下文增强 enrich</span>
          <Switch v-model="enrichEnabled" />
        </label>

        <!-- 检索策略（M6-1.5）：默认=通用方式（跟随全局），按库灵活组合 -->
        <div class="border-t border-[var(--border)] pt-3">
          <div class="mb-2 text-sm font-medium">检索策略</div>
          <div class="flex flex-col gap-3">
            <label class="flex items-center justify-between gap-2">
              <span class="text-sm text-[var(--text-2)]">多路召回（关键词+向量）</span>
              <Select v-model="hybridMode">
                <SelectTrigger class="w-36"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">跟随全局</SelectItem>
                  <SelectItem value="on">开启</SelectItem>
                  <SelectItem value="off">关闭（仅向量）</SelectItem>
                </SelectContent>
              </Select>
            </label>
            <label class="flex items-center justify-between gap-2">
              <span class="text-sm text-[var(--text-2)]">重排 rerank</span>
              <Select v-model="rerankMode">
                <SelectTrigger class="w-36"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">跟随全局</SelectItem>
                  <SelectItem value="on">开启</SelectItem>
                  <SelectItem value="off">关闭</SelectItem>
                </SelectContent>
              </Select>
            </label>
            <label class="flex items-center justify-between gap-2">
              <span class="text-sm text-[var(--text-2)]">多轮查询改写</span>
              <Select v-model="rewriteMode">
                <SelectTrigger class="w-36"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">跟随全局</SelectItem>
                  <SelectItem value="off">关闭</SelectItem>
                  <SelectItem value="conditional">条件触发</SelectItem>
                  <SelectItem value="always">总是改写</SelectItem>
                </SelectContent>
              </Select>
            </label>
            <label class="flex items-center justify-between gap-2">
              <span class="text-sm text-[var(--text-2)]">召回候选数 candidates</span>
              <Input
                v-model="candidatesText" type="number" min="1" max="100"
                class="w-36" placeholder="默认"
              />
            </label>
          </div>
        </div>
      </div>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">取消</Button>
        <Button :disabled="saving" @click="save">保存</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
