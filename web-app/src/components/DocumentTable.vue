<script setup lang="ts">
// 文档表格：文件名/状态 Badge（failed 时 tooltip 展示 error）/行操作
// （failed、pending_ocr 可重试；删除交由父组件处理二次确认 Dialog）。
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableEmpty,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import { RotateCw, Trash2, AlertCircle, Download, Blocks, ClipboardCheck } from "@lucide/vue";
import { statusBadge, canRetryDoc } from "@/lib/kb-utils";
import { docOriginalUrl, type DocumentItem } from "@/lib/api";

withDefaults(defineProps<{ docs: DocumentItem[]; loading: boolean; canManage?: boolean }>(), {
  canManage: true,
});
const emit = defineEmits<{
  retry: [doc: DocumentItem];
  delete: [doc: DocumentItem];
  chunks: [doc: DocumentItem];
  review: [doc: DocumentItem];
}>();
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
              <!-- 下载原始文件：viewer 也可用（后端 require_viewer），
                   浏览器原生下载，文件名恢复上传原名（M5-2） -->
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="下载原文件"
                as="a"
                :href="docOriginalUrl(doc.id)"
              >
                <Download class="size-3.5" />
              </Button>
              <template v-if="canManage">
                <!-- VLM 识别校验（F）：对照原图确认识别文本后才向量化入库 -->
                <Button
                  v-if="doc.status === 'pending_review'"
                  variant="outline"
                  size="sm"
                  @click="emit('review', doc)"
                >
                  <ClipboardCheck class="size-3.5" />
                  校验确认
                </Button>
                <!-- 分块管理（M6-1）：查看/启停/编辑本文档的分块 -->
                <Button
                  v-if="doc.status === 'ready'"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="分块管理"
                  @click="emit('chunks', doc)"
                >
                  <Blocks class="size-3.5" />
                </Button>
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
              </template>
            </div>
          </TableCell>
        </TableRow>
      </TableBody>
    </Table>
  </TooltipProvider>
</template>
