<script setup lang="ts">
// 飞书连接器凭据卡片（设置页·连接器组，admin）：自建应用 app_id/app_secret
// 页面维护。app_id 是公开标识明文展示；secret 只写不回显（脱敏尾4位），
// 与 Provider/向量密钥同规矩。配好后库详情页"从飞书导入"即可用。
import { onMounted, ref } from "vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  getFeishuStatus, putFeishuCredentials, deleteFeishuCredentials,
  type FeishuStatus,
} from "@/lib/api";

const status = ref<FeishuStatus | null>(null);
const editing = ref(false);
const appId = ref("");
const appSecret = ref("");
const busy = ref(false);

async function refresh() {
  try {
    status.value = await getFeishuStatus();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

onMounted(refresh);

function startEdit() {
  editing.value = true;
  appId.value = status.value?.app_id ?? "";
  appSecret.value = "";
}

async function save() {
  if (!appId.value.trim() || !appSecret.value.trim()) return;
  busy.value = true;
  try {
    await putFeishuCredentials(appId.value.trim(), appSecret.value.trim());
    toast.success("飞书凭据已保存，可在库详情页使用「从飞书导入」");
    editing.value = false;
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}

async function clearCredentials() {
  busy.value = true;
  try {
    await deleteFeishuCredentials();
    toast.success("已清除飞书凭据");
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
    <div class="mb-1 flex items-center gap-2">
      <span class="font-medium">飞书知识库</span>
      <Badge
        v-if="status"
        :class="status.configured
          ? 'bg-[var(--ok-weak)] text-[var(--ok)]'
          : 'bg-[var(--surface-2)] text-[var(--text-3)]'"
      >
        {{ status.configured ? "已配置" : "未配置" }}
      </Badge>
    </div>
    <p class="mb-2 text-xs text-[var(--text-3)]">
      配置后可在库详情页从飞书 wiki 导入文档，层级结构自动保留进引用溯源
    </p>
    <!-- 三步配置指引常驻：权限体系是飞书侧最高频的卡点，必须事前讲清 -->
    <ol class="mb-3 list-decimal rounded-[var(--radius-ctl)] bg-[var(--surface-2)] py-2 pl-6 pr-3 text-xs leading-relaxed text-[var(--text-2)]">
      <li>
        <a href="https://open.feishu.cn/app" target="_blank" rel="noopener" class="text-[var(--accent-text)] underline">飞书开放平台</a>
        创建企业自建应用，取得 App ID / App Secret
      </li>
      <li>
        权限管理<b>一次性开通全部只读权限</b>
        <a
          v-if="status?.configured"
          :href="`https://open.feishu.cn/app/${status.app_id}/auth?q=wiki:wiki:readonly,docx:document:readonly,drive:drive:readonly,docs:doc:readonly,sheets:spreadsheet:readonly,bitable:app:readonly&op_from=openapi&token_type=tenant`"
          target="_blank" rel="noopener" class="ml-1 text-[var(--accent-text)] underline"
        >（一键开通）</a>：
        <code class="rounded bg-black/5 px-1">wiki:wiki:readonly</code>（知识库树）、
        <code class="rounded bg-black/5 px-1">docx:document:readonly</code>（文档正文）、
        <code class="rounded bg-black/5 px-1">drive:drive:readonly</code>（文档内图片）、
        <code class="rounded bg-black/5 px-1">docs:doc:readonly</code>（旧版文档）、
        <code class="rounded bg-black/5 px-1">sheets:spreadsheet:readonly</code>（电子表格）、
        <code class="rounded bg-black/5 px-1">bitable:app:readonly</code>（多维表格）。
        应用身份权限需管理员审核，<b>建议一次开全避免反复审批</b>；
        开通后<b>创建版本发布</b>——只勾选不发布不生效
      </li>
      <li>目标知识库 → 设置 → 成员 → <b>添加该应用为成员</b>——仅有权限未入库读不到内容</li>
    </ol>

    <div v-if="status?.configured && !editing" class="mb-3 flex items-center gap-3 text-sm">
      <span class="text-[var(--text-2)]">App ID：{{ status.app_id }}</span>
      <span class="text-[var(--text-3)]">Secret：{{ status.secret_hint }}</span>
    </div>

    <div v-if="editing" class="mb-3 flex flex-col gap-2">
      <Input v-model="appId" placeholder="App ID（cli_ 开头）" />
      <Input v-model="appSecret" type="password" placeholder="App Secret（只写入，不回显）" />
      <div class="flex gap-2">
        <Button size="sm" :disabled="busy || !appId.trim() || !appSecret.trim()" @click="save">保存</Button>
        <Button size="sm" variant="outline" @click="editing = false">取消</Button>
      </div>
    </div>
    <div v-else class="flex gap-2">
      <Button size="sm" variant="outline" @click="startEdit">
        {{ status?.configured ? "更新凭据" : "配置凭据" }}
      </Button>
      <Button
        v-if="status?.configured"
        size="sm"
        variant="outline"
        :disabled="busy"
        @click="clearCredentials"
      >
        清除
      </Button>
    </div>
  </article>
</template>
