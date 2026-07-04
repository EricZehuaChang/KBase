<script setup lang="ts">
// Provider 卡片：信息展示（model/base_url/并发/params 摘要）+ 操作条
// （设为默认/编辑/删除/测试）。active 卡片 accent 边框 + 默认徽章，且删除
// 按钮禁用（tooltip 提示先切换默认，与后端 409 防护对应）。测试状态由
// 父组件按 provider 名持有（testState prop），本组件只负责渲染
// spinner / 绿延迟徽章 / 红失败 tooltip，按钮点击仅上抛事件。
import { Loader2, Pencil, Trash2 } from "@lucide/vue";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import { paramsSummary, type ProviderTestState } from "@/lib/settings-utils";
import type { Provider } from "@/lib/api";

defineProps<{ provider: Provider; isActive: boolean; testState?: ProviderTestState }>();
const emit = defineEmits<{ setActive: []; edit: []; delete: []; test: [] }>();
</script>

<template>
  <TooltipProvider>
    <article
      class="rounded-[var(--radius-card)] border bg-[var(--surface)] p-4"
      :class="isActive ? 'border-[var(--accent)]' : 'border-[var(--border)]'"
    >
      <div class="flex items-center justify-between gap-2">
        <div class="truncate font-medium">{{ provider.name }}</div>
        <Badge v-if="isActive" class="bg-[var(--accent-weak)] text-[var(--accent-text)]">默认</Badge>
      </div>
      <dl class="mt-2 flex flex-col gap-1 text-sm text-[var(--text-2)]">
        <div class="truncate"><dt class="inline text-[var(--text-3)]">模型：</dt>{{ provider.model }}</div>
        <div class="truncate"><dt class="inline text-[var(--text-3)]">base_url：</dt>{{ provider.base_url }}</div>
        <div><dt class="inline text-[var(--text-3)]">并发：</dt>{{ provider.max_concurrency }}</div>
        <div class="truncate" :title="paramsSummary(provider.params)">
          <dt class="inline text-[var(--text-3)]">params：</dt>{{ paramsSummary(provider.params) }}
        </div>
      </dl>

      <div class="mt-3 flex items-center gap-1.5">
        <Button variant="outline" size="sm" :disabled="isActive" @click="emit('setActive')">
          设为默认
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label="编辑" @click="emit('edit')">
          <Pencil class="size-3.5" />
        </Button>

        <Tooltip v-if="isActive">
          <TooltipTrigger as-child>
            <span>
              <Button variant="ghost" size="icon-sm" aria-label="删除" disabled>
                <Trash2 class="size-3.5" />
              </Button>
            </span>
          </TooltipTrigger>
          <TooltipContent>默认 provider 不可删除，请先切换默认</TooltipContent>
        </Tooltip>
        <Button v-else variant="ghost" size="icon-sm" aria-label="删除" @click="emit('delete')">
          <Trash2 class="size-3.5" />
        </Button>

        <Button
          variant="outline" size="sm" class="ml-auto"
          :disabled="testState?.status === 'testing'"
          @click="emit('test')"
        >
          <Loader2 v-if="testState?.status === 'testing'" class="size-3.5 animate-spin" />
          测试
        </Button>

        <Badge v-if="testState?.status === 'ok'" class="bg-[var(--ok-weak)] text-[var(--ok)]">
          {{ Math.round(testState.latencyMs ?? 0) }}ms
        </Badge>
        <Tooltip v-else-if="testState?.status === 'fail'">
          <TooltipTrigger as-child>
            <Badge class="cursor-default bg-[var(--err-weak)] text-[var(--err)]">失败</Badge>
          </TooltipTrigger>
          <TooltipContent>{{ testState.error }}</TooltipContent>
        </Tooltip>
      </div>
    </article>
  </TooltipProvider>
</template>
