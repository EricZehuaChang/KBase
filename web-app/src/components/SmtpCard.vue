<script setup lang="ts">
// 发件箱设置卡片（设置页·系统组，admin）：SMTP 配置页面维护。
// 用途：账号创建通知邮件（建号填了邮箱且此处已配置即自动发送），
// 后续找回密码等系统邮件同用这套配置。密码只写不回显（编辑留空=保留）。
import { onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { getSmtpSettings, putSmtpSettings, testSmtp } from "@/lib/api";

const { t } = useI18n();

const form = reactive({ host: "", port: 465, user: "", password: "",
                        from_addr: "", from_name: "KBase" });
const configured = ref(false);
const hasPassword = ref(false);
const busy = ref(false);
const testTo = ref("");
const testing = ref(false);

async function refresh() {
  try {
    const st = await getSmtpSettings();
    configured.value = st.configured;
    hasPassword.value = st.has_password;
    form.host = st.host ?? "";
    form.port = st.port;
    form.user = st.user ?? "";
    form.from_addr = st.from_addr ?? "";
    form.from_name = st.from_name ?? "KBase";
    form.password = "";
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

onMounted(refresh);

async function save() {
  if (!form.host.trim() || !form.user.trim()) return;
  if (!hasPassword.value && !form.password) {
    toast.error(t("smtp.password_required"));
    return;
  }
  busy.value = true;
  try {
    await putSmtpSettings({
      host: form.host.trim(), port: Number(form.port) || 465,
      user: form.user.trim(),
      password: form.password || null,        // 留空=保留旧密码
      from_addr: form.from_addr.trim() || form.user.trim(),
      from_name: form.from_name.trim() || "KBase",
    });
    toast.success(t("smtp.saved"));
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}

async function sendTest() {
  if (!testTo.value.trim()) return;
  testing.value = true;
  try {
    await testSmtp(testTo.value.trim());
    toast.success(t("smtp.test_sent", { to: testTo.value }));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    testing.value = false;
  }
}
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-1 flex items-center gap-2">
      <span class="font-medium">{{ t("smtp.title") }}</span>
      <Badge
        :class="configured
          ? 'bg-[var(--ok-weak)] text-[var(--ok)]'
          : 'bg-[var(--surface-2)] text-[var(--text-3)]'"
      >
        {{ configured ? t("smtp.configured") : t("smtp.not_configured") }}
      </Badge>
    </div>
    <p class="mb-3 text-xs text-[var(--text-3)]">
      {{ t("smtp.desc") }}
    </p>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("smtp.host") }}</span>
        <Input v-model="form.host" placeholder="smtp.163.com" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("smtp.port") }}</span>
        <Input v-model.number="form.port" type="number" placeholder="465" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("smtp.user") }}</span>
        <Input v-model="form.user" placeholder="notify@company.com" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("smtp.password") }}{{ hasPassword ? t("smtp.password_keep") : "" }}</span>
        <Input v-model="form.password" type="password" :placeholder="hasPassword ? t('smtp.password_set') : t('smtp.password_ph')" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("smtp.from_addr") }}</span>
        <Input v-model="form.from_addr" placeholder="notify@company.com" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("smtp.from_name") }}</span>
        <Input v-model="form.from_name" :placeholder="t('smtp.from_name_ph')" />
      </label>
    </div>

    <div class="mt-3 flex flex-wrap items-center gap-2">
      <Button size="sm" :disabled="busy || !form.host.trim() || !form.user.trim()" @click="save">
        {{ t("common.save") }}
      </Button>
      <div class="ml-2 flex items-center gap-2">
        <Input
          v-model="testTo"
          class="w-56"
          :placeholder="t('smtp.test_to_ph')"
          @keydown.enter="sendTest"
        />
        <Button size="sm" variant="outline" :disabled="testing || !configured || !testTo.trim()" @click="sendTest">
          {{ testing ? t("smtp.sending") : t("smtp.test") }}
        </Button>
      </div>
    </div>
  </article>
</template>
