<script setup lang="ts">
// 发件箱设置卡片（设置页·系统组，admin）：SMTP 配置页面维护。
// 用途：账号创建通知邮件（建号填了邮箱且此处已配置即自动发送），
// 后续找回密码等系统邮件同用这套配置。密码只写不回显（编辑留空=保留）。
import { onMounted, reactive, ref } from "vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { getSmtpSettings, putSmtpSettings, testSmtp } from "@/lib/api";

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
    toast.error("首次配置需填写 SMTP 密码/授权码");
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
    toast.success("发件箱已保存，建议发一封测试邮件确认连通");
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
    toast.success(`测试邮件已发送至 ${testTo.value}，请查收（含垃圾箱）`);
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
      <span class="font-medium">发件箱设置</span>
      <Badge
        :class="configured
          ? 'bg-[var(--ok-weak)] text-[var(--ok)]'
          : 'bg-[var(--surface-2)] text-[var(--text-3)]'"
      >
        {{ configured ? "已配置" : "未配置" }}
      </Badge>
    </div>
    <p class="mb-3 text-xs text-[var(--text-3)]">
      系统通知邮件的发送账号：配置后，新建用户（填写了邮箱）时会自动发送
      账号开通通知。163/QQ 等个人邮箱的"密码"填 SMTP 授权码，不是登录密码
    </p>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">SMTP 主机</span>
        <Input v-model="form.host" placeholder="smtp.163.com" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">SMTP 端口（465=SSL，587=STARTTLS）</span>
        <Input v-model.number="form.port" type="number" placeholder="465" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">SMTP 用户</span>
        <Input v-model="form.user" placeholder="notify@company.com" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">SMTP 密码/授权码{{ hasPassword ? "（留空=保留当前）" : "" }}</span>
        <Input v-model="form.password" type="password" :placeholder="hasPassword ? '••••••••（已配置）' : '授权码'" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">默认发件人地址（留空=同 SMTP 用户）</span>
        <Input v-model="form.from_addr" placeholder="notify@company.com" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">默认发件人显示名称</span>
        <Input v-model="form.from_name" placeholder="KBase 通知" />
      </label>
    </div>

    <div class="mt-3 flex flex-wrap items-center gap-2">
      <Button size="sm" :disabled="busy || !form.host.trim() || !form.user.trim()" @click="save">
        保存
      </Button>
      <div class="ml-2 flex items-center gap-2">
        <Input
          v-model="testTo"
          class="w-56"
          placeholder="收件地址（测试用）"
          @keydown.enter="sendTest"
        />
        <Button size="sm" variant="outline" :disabled="testing || !configured || !testTo.trim()" @click="sendTest">
          {{ testing ? "发送中…" : "测试邮件设置" }}
        </Button>
      </div>
    </div>
  </article>
</template>
