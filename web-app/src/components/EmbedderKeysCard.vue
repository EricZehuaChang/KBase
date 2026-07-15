<script setup lang="ts">
// 向量模型密钥卡片（设置页，admin）：cfg.embedders 中云端向量选项
// （openai-embed）的 API Key 页面维护。DB 覆盖 > 环境变量；保存/清除后
// 服务端丢弃缓存实例，下次摄取/检索按新密钥重建。原文永不回显（脱敏尾4位）。
import { onMounted, ref } from "vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  listEmbedderKeys, putEmbedderKey, deleteEmbedderKey, type EmbedderKeyItem,
} from "@/lib/api";

const items = ref<EmbedderKeyItem[]>([]);
const editingId = ref<string | null>(null);
const keyInput = ref("");
const busy = ref(false);

async function refresh() {
  try {
    items.value = (await listEmbedderKeys()).items;
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

onMounted(refresh);

function startEdit(id: string) {
  editingId.value = id;
  keyInput.value = "";
}

async function save(id: string) {
  if (!keyInput.value.trim()) return;
  busy.value = true;
  try {
    await putEmbedderKey(id, keyInput.value.trim());
    toast.success("密钥已保存，下次向量化/检索即生效");
    editingId.value = null;
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}

async function clearKey(id: string) {
  busy.value = true;
  try {
    await deleteEmbedderKey(id);
    toast.success("已清除页面密钥，回落到环境变量");
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-1 font-medium">向量模型密钥</div>
    <p class="mb-3 text-xs text-[var(--text-3)]">
      云端向量模型（openai-embed）的 API Key，页面配置优先于环境变量；保存后下次向量化即生效
    </p>
    <p v-if="!items.length" class="text-sm text-[var(--text-3)]">
      配置文件 embedders 清单中暂无云端向量模型选项
    </p>
    <div
      v-for="item in items"
      :key="item.id"
      class="mb-2 rounded-[var(--radius-ctl)] border border-[var(--border)] px-3 py-2"
    >
      <div class="flex items-center gap-2">
        <span class="text-sm font-medium">{{ item.id }}</span>
        <span class="text-xs text-[var(--text-3)]">{{ item.model }}</span>
        <Badge
          class="ml-auto"
          :class="item.has_db_key
            ? 'bg-[var(--ok-weak)] text-[var(--ok)]'
            : 'bg-[var(--surface-2)] text-[var(--text-3)]'"
        >
          {{ item.has_db_key ? `页面密钥 ${item.key_hint}` : `环境变量 ${item.api_key_env}` }}
        </Badge>
      </div>

      <div v-if="editingId === item.id" class="mt-2 flex items-center gap-2">
        <input
          v-model="keyInput"
          type="password"
          placeholder="粘贴 API Key（只写入，不回显）"
          class="flex-1 rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--accent)]"
          aria-label="向量模型 API Key"
          @keydown.enter="save(item.id)"
        />
        <Button size="sm" :disabled="busy || !keyInput.trim()" @click="save(item.id)">保存</Button>
        <Button size="sm" variant="outline" @click="editingId = null">取消</Button>
      </div>
      <div v-else class="mt-2 flex items-center gap-2">
        <Button size="sm" variant="outline" @click="startEdit(item.id)">
          {{ item.has_db_key ? "更新密钥" : "设置密钥" }}
        </Button>
        <Button
          v-if="item.has_db_key"
          size="sm"
          variant="outline"
          :disabled="busy"
          @click="clearKey(item.id)"
        >
          清除（回落环境变量）
        </Button>
      </div>
    </div>
  </article>
</template>
