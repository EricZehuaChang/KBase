<script setup lang="ts">
// 文档表格：文件名/状态 Badge（failed 时 tooltip 展示 error）/行操作
// （failed、pending_ocr 可重试；删除交由父组件处理二次确认 Dialog）。
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableEmpty,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import { RotateCw, Trash2, AlertCircle } from "@lucide/vue";
import { statusBadge, canRetryDoc } from "@/lib/kb-utils";
import type { DocumentItem } from "@/lib/api";

defineProps<{ docs: DocumentItem[]; loading: boolean }>();
const emit = defineEmits<{ retry: [doc: DocumentItem]; delete: [doc: DocumentItem] }>();
</script>

<template>
  <TooltipProvider>
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>文件名</TableHead>
          <TableHead>状态</TableHead>
          <TableHead class="w-32">操作</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableEmpty v-if="!loading && docs.length === 0" :colspan="3">
          暂无文档，拖拽或选择文件开始上传
        </TableEmpty>
        <TableRow v-for="doc in docs" :key="doc.id">
          <TableCell class="max-w-xs truncate">{{ doc.filename }}</TableCell>
          <TableCell>
            <div class="flex items-center gap-1.5">
              <Badge :class="statusBadge(doc.status).class">{{ statusBadge(doc.status).label }}</Badge>
              <Tooltip v-if="doc.status === 'failed' && doc.error">
                <TooltipTrigger as-child>
                  <AlertCircle class="size-3.5 text-[var(--err)]" />
                </TooltipTrigger>
                <TooltipContent>{{ doc.error }}</TooltipContent>
              </Tooltip>
            </div>
          </TableCell>
          <TableCell>
            <div class="flex items-center gap-1">
              <Button
                v-if="canRetryDoc(doc.status)"
                variant="ghost"
                size="icon-sm"
                aria-label="重试"
                @click="emit('retry', doc)"
              >
                <RotateCw class="size-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="删除"
                @click="emit('delete', doc)"
              >
                <Trash2 class="size-3.5" />
              </Button>
            </div>
          </TableCell>
        </TableRow>
      </TableBody>
    </Table>
  </TooltipProvider>
</template>
