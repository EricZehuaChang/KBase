<script setup lang="ts">
// 文档表格：文件名/状态 Badge（failed 时 tooltip 展示 error）/行操作
// （failed、pending_ocr 可重试；删除交由父组件处理二次确认 Dialog）。
import { useI18n } from "vue-i18n";
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
const { t } = useI18n();

// 状态标签本地化：statusBadge 只取配色 class，文案按 doc.status.<code> 查 i18n；
// 未知状态（后端将来新增）回落原始 code，与 statusBadge 的 fallback 语义一致。
function statusLabel(status: string): string {
  const key = `doc.status.${status}`;
  const translated = t(key);
  return translated !== key ? translated : status;
}
</script>

<template>
  <TooltipProvider>
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{{ t("doc.filename") }}</TableHead>
          <TableHead>{{ t("doc.status_label") }}</TableHead>
          <TableHead class="w-32">{{ t("common.actions") }}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableEmpty v-if="!loading && docs.length === 0" :colspan="3">
          {{ t("doc.empty") }}
        </TableEmpty>
        <TableRow v-for="doc in docs" :key="doc.id">
          <TableCell class="max-w-xs truncate">{{ doc.filename }}</TableCell>
          <TableCell>
            <div class="flex items-center gap-1.5">
              <Badge :class="statusBadge(doc.status).class">{{ statusLabel(doc.status) }}</Badge>
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
                :aria-label="t('doc.download')"
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
                  {{ t("doc.review_confirm") }}
                </Button>
                <!-- 分块管理（M6-1）：查看/启停/编辑本文档的分块 -->
                <Button
                  v-if="doc.status === 'ready'"
                  variant="ghost"
                  size="icon-sm"
                  :aria-label="t('doc.chunks')"
                  @click="emit('chunks', doc)"
                >
                  <Blocks class="size-3.5" />
                </Button>
                <Button
                  v-if="canRetryDoc(doc.status)"
                  variant="ghost"
                  size="icon-sm"
                  :aria-label="t('common.retry')"
                  @click="emit('retry', doc)"
                >
                  <RotateCw class="size-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  :aria-label="t('common.delete')"
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
