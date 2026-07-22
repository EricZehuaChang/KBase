<script setup lang="ts">
// 向量模型密钥卡片（设置页，admin）：cfg.embedders 中云端向量选项
// （openai-embed）的 API Key 页面维护。DB 覆盖 > 环境变量；保存/清除后
// 服务端丢弃缓存实例，下次摄取/检索按新密钥重建。原文永不回显（脱敏尾4位）。
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  listEmbedderKeys, putEmbedderKey, deleteEmbedderKey, type EmbedderKeyItem,
} from "@/lib/api";

const { t } = useI18n();

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
    toast.success(t("embedder.saved"));
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
    toast.success(t("embedder.cleared"));
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
    <div class="mb-1 font-medium">{{ t("embedder.title") }}</div>
    <p class="mb-3 text-xs text-[var(--text-3)]">
      {{ t("embedder.desc") }}
    </p>
    <p v-if="!items.length" class="text-sm text-[var(--text-3)]">
      {{ t("embedder.no_options") }}
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
          {{ item.has_db_key ? t("embedder.db_key", { hint: item.key_hint }) : t("embedder.env_key", { env: item.api_key_env }) }}
        </Badge>
      </div>

      <div v-if="editingId === item.id" class="mt-2 flex items-center gap-2">
        <input
          v-model="keyInput"
          type="password"
          :placeholder="t('embedder.key_placeholder')"
          class="flex-1 rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--accent)]"
          :aria-label="t('embedder.key_label')"
          @keydown.enter="save(item.id)"
        />
        <Button size="sm" :disabled="busy || !keyInput.trim()" @click="save(item.id)">{{ t("common.save") }}</Button>
        <Button size="sm" variant="outline" @click="editingId = null">{{ t("common.cancel") }}</Button>
      </div>
      <div v-else class="mt-2 flex items-center gap-2">
        <Button size="sm" variant="outline" @click="startEdit(item.id)">
          {{ item.has_db_key ? t("embedder.update_key") : t("embedder.set_key") }}
        </Button>
        <Button
          v-if="item.has_db_key"
          size="sm"
          variant="outline"
          :disabled="busy"
          @click="clearKey(item.id)"
        >
          {{ t("embedder.clear_key") }}
        </Button>
      </div>
    </div>
  </article>
</template>
