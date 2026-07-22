<script setup lang="ts">
// 【使用端】会话侧栏：ChatHome 原生持有的子组件（M5-1 F2），替代 F1 遗留的
// Teleport to="#sidebar-slot" 耦合——不再依赖 PortalShell 提供挂载点，
// ChatHome 直接把这个组件摆进自己的 flex 布局里，父子关系是显式的模板
// 嵌套，不是跨组件树的隐式注入。
//
// 窄屏折叠：collapsed 由父组件（ChatHome）持有并通过 v-model 传入——折叠
// 状态要和"要不要给中间消息区腾地方"联动，这个决策权自然属于持有整体布局
// 的父组件，侧栏自己只负责展示折叠后的窄条 + 提供展开按钮。
import { computed, nextTick, ref } from "vue";
import { Plus, Pencil, Trash2, PanelLeftClose, PanelLeftOpen, Check, X } from "@lucide/vue";
import { useI18n } from "vue-i18n";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import type { Conversation } from "@/lib/api";
import type { TimeGroup } from "@/lib/chat-utils";

const { t } = useI18n();

const props = defineProps<{
  groups: { label: TimeGroup; items: Conversation[] }[];
  activeId: string | null;
  hasMore: boolean;
  collapsed: boolean;
}>();

const emit = defineEmits<{
  "update:collapsed": [boolean];
  new: [];
  select: [Conversation];
  rename: [id: string, title: string];
  delete: [id: string];
  loadMore: [];
}>();

// ---- 内联重命名 ----
const editingId = ref<string | null>(null);
const editingValue = ref("");
const inputRefs = new Map<string, HTMLInputElement>();

function setInputRef(id: string, el: unknown) {
  if (el instanceof HTMLInputElement) inputRefs.set(id, el);
  else inputRefs.delete(id);
}

async function startRename(conv: Conversation) {
  editingId.value = conv.id;
  editingValue.value = conv.title ?? t("portal.session.new");
  await nextTick();
  inputRefs.get(conv.id)?.focus();
  inputRefs.get(conv.id)?.select();
}

function cancelRename() {
  editingId.value = null;
}

function commitRename(conv: Conversation) {
  const id = editingId.value;
  if (id !== conv.id) return;   // 已经被取消/切走，避免 blur 事件迟到误提交
  const title = editingValue.value.trim();
  editingId.value = null;
  if (!title || title === conv.title) return;   // 空标题或未改动：不发请求
  emit("rename", id, title);
}

// ---- 删除确认（沿用 KbView.vue 的 Dialog 确认模式，而不是原生 confirm()——
// 与其余删除操作的交互一致） ----
const deleteTarget = ref<Conversation | null>(null);

function requestDelete(conv: Conversation) {
  deleteTarget.value = conv;
}

function confirmDelete() {
  if (!deleteTarget.value) return;
  emit("delete", deleteTarget.value.id);
  deleteTarget.value = null;
}

const isEmpty = computed(() => props.groups.every((g) => g.items.length === 0));
</script>

<template>
  <!-- 折叠态：窄条只留展开按钮，不占用消息区宽度 -->
  <aside
    v-if="collapsed"
    class="flex w-10 shrink-0 flex-col items-center border-r border-[var(--border)] bg-[var(--surface)] py-2"
  >
    <button
      type="button"
      class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
      :aria-label="t('portal.session.expand')"
      @click="emit('update:collapsed', false)"
    >
      <PanelLeftOpen class="size-4" />
    </button>
  </aside>

  <aside
    v-else
    class="flex w-[220px] shrink-0 flex-col overflow-hidden border-r border-[var(--border)] bg-[var(--surface)]"
    :aria-label="t('portal.session.list')"
  >
    <div class="flex items-center gap-1 px-2 pt-2">
      <Button variant="outline" size="sm" class="flex-1 justify-start" @click="emit('new')">
        <Plus class="size-3.5" />
        {{ t("portal.session.new_btn") }}
      </Button>
      <button
        type="button"
        class="shrink-0 rounded-[var(--radius-ctl)] p-1.5 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
        :aria-label="t('portal.session.collapse')"
        @click="emit('update:collapsed', true)"
      >
        <PanelLeftClose class="size-4" />
      </button>
    </div>

    <div class="flex-1 overflow-y-auto px-2 py-2">
      <p v-if="isEmpty" class="px-2 py-4 text-center text-xs text-[var(--text-3)]">
        {{ t("portal.session.empty") }}
      </p>

      <div v-for="group in groups" :key="group.label" class="mb-2">
        <div v-if="group.items.length" class="px-2 py-1 text-xs font-medium text-[var(--text-3)]">
          {{ t(`portal.timegroup.${group.label}`) }}
        </div>
        <div
          v-for="conv in group.items"
          :key="conv.id"
          class="group/item flex items-center gap-1 rounded-[var(--radius-ctl)] px-2 py-1.5"
          :class="activeId === conv.id
            ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]'
            : 'text-[var(--text-2)] hover:bg-[var(--surface-2)]'"
        >
          <template v-if="editingId === conv.id">
            <input
              :ref="(el) => setInputRef(conv.id, el)"
              v-model="editingValue"
              type="text"
              class="min-w-0 flex-1 rounded-sm border border-[var(--accent)] bg-[var(--bg)] px-1 py-0.5 text-sm text-[var(--text)] outline-none"
              :aria-label="t('portal.session.title_input')"
              maxlength="200"
              @keydown.enter="commitRename(conv)"
              @keydown.escape="cancelRename"
              @blur="commitRename(conv)"
            />
            <button
              type="button"
              class="shrink-0 rounded-sm p-1 text-[var(--text-2)] hover:bg-[var(--surface-2)]"
              :aria-label="t('portal.session.confirm_rename')"
              @mousedown.prevent="commitRename(conv)"
            >
              <Check class="size-3.5" />
            </button>
            <button
              type="button"
              class="shrink-0 rounded-sm p-1 text-[var(--text-2)] hover:bg-[var(--surface-2)]"
              :aria-label="t('portal.session.cancel_rename')"
              @mousedown.prevent="cancelRename"
            >
              <X class="size-3.5" />
            </button>
          </template>
          <template v-else>
            <button
              type="button"
              class="min-w-0 flex-1 truncate text-left text-sm"
              @click="emit('select', conv)"
            >
              {{ conv.title || t("portal.session.new") }}
            </button>
            <!-- hover 才"看见"的操作按钮：用 opacity 而不是 hidden/block（display
            切换）——display:none 的元素没有布局盒、会被整体摘出无障碍树，键盘
            Tab 也够不到，鼠标之外的操作方式（键盘导航、触屏、自动化测试的
            坐标点击）全部失效。opacity-0 保留布局与可达性，只是视觉上淡出；
            focus-visible 时同样显现，键盘聚焦到按钮本身也能操作，不依赖真的
            把鼠标悬停在这一行上。 -->
            <button
              type="button"
              class="shrink-0 rounded-sm p-1 text-[var(--text-3)] opacity-0 hover:bg-[var(--surface-2)] focus-visible:opacity-100 group-hover/item:opacity-100"
              :aria-label="t('portal.session.rename')"
              @click.stop="startRename(conv)"
            >
              <Pencil class="size-3.5" />
            </button>
            <button
              type="button"
              class="shrink-0 rounded-sm p-1 text-[var(--text-3)] opacity-0 hover:bg-[var(--err)] hover:text-[var(--surface)] focus-visible:opacity-100 group-hover/item:opacity-100"
              :aria-label="t('portal.session.delete')"
              @click.stop="requestDelete(conv)"
            >
              <Trash2 class="size-3.5" />
            </button>
          </template>
        </div>
      </div>

      <Button
        v-if="hasMore"
        variant="ghost"
        size="sm"
        class="w-full justify-start text-[var(--text-3)]"
        @click="emit('loadMore')"
      >
        {{ t("portal.session.load_more") }}
      </Button>
    </div>
  </aside>

  <!-- 删除会话确认：沿用 KbView.vue 删除知识库的 Dialog 交互模式 -->
  <Dialog :open="!!deleteTarget" @update:open="(v) => { if (!v) deleteTarget = null; }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("portal.session.delete") }}</DialogTitle>
        <DialogDescription>
          {{ t("portal.session.delete_confirm", { title: deleteTarget?.title || t("portal.session.new") }) }}
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="deleteTarget = null">{{ t("common.cancel") }}</Button>
        <Button variant="destructive" @click="confirmDelete">{{ t("common.confirm_delete") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
