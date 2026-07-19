<script setup lang="ts">
// 飞书群机器人卡片（设置页·连接器组，admin）：在飞书群 @机器人 或单聊
// 提问，KBase 检索后卡片回复（答案+引用来源）。配置项=事件回调凭据
// （verification token 必填 / encrypt key 可选，均只写不回显）+ 绑定
// 知识库 + 回答模型（对标 FastGPT：模型在管理侧绑定，群成员无感）。
import { onMounted, ref } from "vue";
import { toast } from "vue-sonner";
import { Copy } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  getFeishuBot, putFeishuBot, listKbs, listProviders,
  type FeishuBotStatus, type Kb,
} from "@/lib/api";

const status = ref<FeishuBotStatus | null>(null);
const kbs = ref<Kb[]>([]);
const providers = ref<string[]>([]);
const verificationToken = ref("");
const encryptKey = ref("");
const kbId = ref("");
// Select 不收空串 value，用哨兵表达"系统默认"
const provider = ref("__default__");
const busy = ref(false);

const eventsUrl = `${window.location.origin}/api/feishu/events`;

async function refresh() {
  try {
    status.value = await getFeishuBot();
    kbId.value = status.value.kb_id ?? "";
    provider.value = status.value.provider || "__default__";
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

onMounted(async () => {
  await refresh();
  try {
    kbs.value = await listKbs();
    providers.value = (await listProviders()).providers;
  } catch {
    // 下拉清单拉不到不阻塞状态展示
  }
});

async function save() {
  if (!kbId.value) {
    toast.error("请选择机器人回答依据的知识库");
    return;
  }
  if (!status.value?.has_verification_token && !verificationToken.value.trim()) {
    toast.error("首次配置需填写 Verification Token（飞书后台事件订阅页）");
    return;
  }
  busy.value = true;
  try {
    await putFeishuBot({
      verification_token: verificationToken.value.trim() || null,  // 空=保留
      encrypt_key: encryptKey.value.trim() || null,
      kb_id: kbId.value,
      provider: provider.value === "__default__" ? null : provider.value,
    });
    verificationToken.value = "";
    encryptKey.value = "";
    toast.success("机器人配置已保存");
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}

async function copyUrl() {
  await navigator.clipboard.writeText(eventsUrl);
  toast.success("回调地址已复制");
}
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-1 flex items-center gap-2">
      <span class="font-medium">飞书群机器人</span>
      <Badge
        :class="status?.configured
          ? 'bg-[var(--ok-weak)] text-[var(--ok)]'
          : 'bg-[var(--surface-2)] text-[var(--text-3)]'"
      >
        {{ status?.configured ? "已配置" : "未配置" }}
      </Badge>
    </div>
    <p class="mb-3 text-xs text-[var(--text-3)]">
      在飞书群里 @机器人（或单聊）提问，自动用所选知识库检索并卡片回复（含引用来源）。
      使用上方同一个自建应用：需开启「机器人」能力、开通
      <code class="rounded bg-black/5 px-1">im:message</code> 与
      <code class="rounded bg-black/5 px-1">im:message:send_as_bot</code> 权限，
      事件订阅添加 <code class="rounded bg-black/5 px-1">im.message.receive_v1</code>
      并把下方回调地址填入「请求地址」。
    </p>

    <div class="mb-3 flex items-center gap-1.5">
      <span class="shrink-0 text-sm text-[var(--text-2)]">事件回调地址</span>
      <code class="min-w-0 flex-1 truncate rounded bg-[var(--surface-2)] px-2 py-1 text-xs">
        {{ eventsUrl }}
      </code>
      <Button size="sm" variant="outline" @click="copyUrl">
        <Copy class="size-3" />
        复制
      </Button>
    </div>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">
          Verification Token{{ status?.has_verification_token ? "（留空=保留当前）" : "" }}
        </span>
        <Input
          v-model="verificationToken" type="password"
          :placeholder="status?.has_verification_token ? '••••••••（已配置）' : '飞书后台·事件订阅页'"
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">
          Encrypt Key（可选{{ status?.has_encrypt_key ? "，留空=保留当前" : "" }}）
        </span>
        <Input
          v-model="encryptKey" type="password"
          :placeholder="status?.has_encrypt_key ? '••••••••（已配置）' : '启用了事件加密才需要'"
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">回答依据的知识库</span>
        <Select v-model="kbId">
          <SelectTrigger><SelectValue placeholder="选择知识库" /></SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem v-for="kb in kbs" :key="kb.id" :value="kb.id">{{ kb.name }}</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">回答模型</span>
        <Select v-model="provider">
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="__default__">系统默认</SelectItem>
              <SelectItem v-for="p in providers" :key="p" :value="p">{{ p }}</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </label>
    </div>

    <div class="mt-3">
      <Button size="sm" :disabled="busy || !kbId" @click="save">保存</Button>
    </div>
  </article>
</template>
