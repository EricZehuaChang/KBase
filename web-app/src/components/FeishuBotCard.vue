<script setup lang="ts">
// 飞书群机器人卡片（设置页·连接器组，admin）：在飞书群 @机器人 或单聊
// 提问，KBase 检索后卡片回复（答案+引用来源）。配置项=事件回调凭据
// （verification token 必填 / encrypt key 可选，均只写不回显）+ 绑定
// 知识库 + 回答模型（对标 FastGPT：模型在管理侧绑定，群成员无感）。
import { computed, onMounted, ref } from "vue";
import { useI18n, I18nT } from "vue-i18n";
import { toast } from "vue-sonner";
import { Copy } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  getFeishuBot, putFeishuBot, listKbs, listProviders, getFeishuStatus,
  type FeishuBotStatus, type Kb,
} from "@/lib/api";

const { t } = useI18n();

const status = ref<FeishuBotStatus | null>(null);
const kbs = ref<Kb[]>([]);
const providers = ref<string[]>([]);
const verificationToken = ref("");
const encryptKey = ref("");
const kbId = ref("");
// Select 不收空串 value，用哨兵表达"系统默认"
const provider = ref("__default__");
const busy = ref(false);
// 一键授权用的 app_id：机器人与连接器复用同一自建应用，直接读飞书凭据
// 状态（未配凭据时为 null，链接不渲染）
const appId = ref<string | null>(null);

const eventsUrl = `${window.location.origin}/api/feishu/events`;

// 一键开通机器人权限链接（与文档导入对话框同一姿势）：只带机器人收发
// 两项 scope；跳转飞书后勾选发布即生效。事件订阅/回调地址仍需手动配
// （授权链接只能开权限，配不了事件订阅）。
const authUrl = computed(() =>
  appId.value
    ? `https://open.feishu.cn/app/${appId.value}/auth`
      + `?q=im:message,im:message:send_as_bot`
      + `&op_from=openapi&token_type=tenant`
    : null);

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
    appId.value = (await getFeishuStatus()).app_id;
  } catch {
    // 下拉清单/凭据状态拉不到不阻塞状态展示
  }
});

async function save() {
  if (!kbId.value) {
    toast.error(t("feishubot.need_kb"));
    return;
  }
  if (!status.value?.has_verification_token && !verificationToken.value.trim()) {
    toast.error(t("feishubot.need_token"));
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
    toast.success(t("feishubot.saved"));
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}

async function copyUrl() {
  await navigator.clipboard.writeText(eventsUrl);
  toast.success(t("feishubot.url_copied"));
}
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-1 flex items-center gap-2">
      <span class="font-medium">{{ t("feishubot.title") }}</span>
      <Badge
        :class="status?.configured
          ? 'bg-[var(--ok-weak)] text-[var(--ok)]'
          : 'bg-[var(--surface-2)] text-[var(--text-3)]'"
      >
        {{ status?.configured ? t("smtp.configured") : t("smtp.not_configured") }}
      </Badge>
    </div>
    <!-- 说明段含 code/一键授权动态链接/条件模板：用 <I18nT> 具名 slot 把 3 个
    权限码（im:message 等，英文 API 标识不译）与授权链接放槽，文案主体进 key。 -->
    <p class="mb-3 text-xs text-[var(--text-3)]">
      <I18nT keypath="feishubot.desc_intro" tag="span" scope="global">
        <template #scopeMsg><code class="rounded bg-black/5 px-1">im:message</code></template>
        <template #scopeSend><code class="rounded bg-black/5 px-1">im:message:send_as_bot</code></template>
        <template #eventKey><code class="rounded bg-black/5 px-1">im.message.receive_v1</code></template>
      </I18nT>
      <I18nT v-if="authUrl" keypath="feishubot.desc_authed" tag="span" scope="global">
        <template #oneClickBot>
          <a
            :href="authUrl" target="_blank" rel="noopener"
            class="text-[var(--accent-text)] underline"
          >{{ t("feishubot.one_click_bot") }}</a>
        </template>
      </I18nT>
      <template v-else>{{ t("feishubot.desc_no_creds") }}</template>
    </p>

    <div class="mb-3 flex items-center gap-1.5">
      <span class="shrink-0 text-sm text-[var(--text-2)]">{{ t("feishubot.callback_url") }}</span>
      <code class="min-w-0 flex-1 truncate rounded bg-[var(--surface-2)] px-2 py-1 text-xs">
        {{ eventsUrl }}
      </code>
      <Button size="sm" variant="outline" @click="copyUrl">
        <Copy class="size-3" />
        {{ t("msg.copy") }}
      </Button>
    </div>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">
          Verification Token{{ status?.has_verification_token ? t("feishubot.keep_current") : "" }}
        </span>
        <Input
          v-model="verificationToken" type="password"
          :placeholder="status?.has_verification_token ? t('smtp.password_set') : t('feishubot.token_ph')"
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">
          {{ status?.has_encrypt_key ? t("feishubot.encrypt_label_keep") : t("feishubot.encrypt_label") }}
        </span>
        <Input
          v-model="encryptKey" type="password"
          :placeholder="status?.has_encrypt_key ? t('smtp.password_set') : t('feishubot.encrypt_ph')"
        />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("feishubot.answer_kb") }}</span>
        <Select v-model="kbId">
          <SelectTrigger><SelectValue :placeholder="t('portal.topbar.select_kb')" /></SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem v-for="kb in kbs" :key="kb.id" :value="kb.id">{{ kb.name }}</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("sharedlg.model") }}</span>
        <Select v-model="provider">
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="__default__">{{ t("sharedlg.default_model") }}</SelectItem>
              <SelectItem v-for="p in providers" :key="p" :value="p">{{ p }}</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </label>
    </div>

    <div class="mt-3">
      <Button size="sm" :disabled="busy || !kbId" @click="save">{{ t("common.save") }}</Button>
    </div>
  </article>
</template>
