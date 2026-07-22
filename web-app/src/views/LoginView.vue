<script setup lang="ts">
// 独立登录页（不套 AppShell，见 App.vue 按路由分流渲染）。成功后跳转到
// router.query.redirect 指定的原目标路径（redirectTarget 纯函数校验，防开
// 放重定向），无 redirect 时回首页。
// 三种模式：login（默认）/ forgot（忘记密码，输用户名或邮箱发重置邮件）/
// reset（邮件链接带 ?reset_token= 进来，设新密码）。
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import LanguagePicker from "@/components/LanguagePicker.vue";
import {
  login, clearSessionCache, getSsoStatus, forgotPassword, resetPassword,
} from "@/lib/api";
import { redirectTarget } from "@/lib/auth-utils";

const route = useRoute();
const router = useRouter();
const { t } = useI18n();

type Mode = "login" | "forgot" | "reset";
// 邮件链接落地：URL 带 reset_token 直接进重置模式
const resetToken = typeof route.query.reset_token === "string" ? route.query.reset_token : "";
const mode = ref<Mode>(resetToken ? "reset" : "login");

const username = ref("");
const password = ref("");
const error = ref<string | null>(null);
const submitting = ref(false);

// M6-8 企业 SSO：后端启用 OIDC 时显示企业账号入口（整页跳转到 IdP）
const ssoEnabled = ref(false);
onMounted(async () => {
  try {
    ssoEnabled.value = (await getSsoStatus()).enabled;
  } catch {
    // 探测失败不影响密码登录
  }
});

function ssoLogin() {
  window.location.href = "/api/auth/sso/login";
}

async function submit() {
  if (!username.value.trim() || !password.value) {
    error.value = t("login.enter_credentials");
    return;
  }
  submitting.value = true;
  error.value = null;
  try {
    await login(username.value.trim(), password.value);
    clearSessionCache(); // 登录成功后旧的"无会话"缓存已失效，下次探测重新取
    await router.replace(redirectTarget(route));
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    submitting.value = false;
  }
}

// ---- 忘记密码：输用户名或邮箱 → 发重置邮件 ----
const forgotAccount = ref("");
const forgotSent = ref<string | null>(null);   // 成功提示（防枚举统一文案）

async function submitForgot() {
  if (!forgotAccount.value.trim()) return;
  submitting.value = true;
  error.value = null;
  try {
    const r = await forgotPassword(forgotAccount.value.trim());
    forgotSent.value = r.message;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    submitting.value = false;
  }
}

function switchMode(m: Mode) {
  mode.value = m;
  error.value = null;
  forgotSent.value = null;
}

// ---- 重置密码（邮件链接 ?reset_token=）----
const newPassword = ref("");
const confirmPassword = ref("");
const resetDone = ref(false);
const resetValid = computed(
  () => newPassword.value.length >= 6 && newPassword.value === confirmPassword.value);

async function submitReset() {
  if (newPassword.value.length < 6) {
    error.value = t("login.pwd_min");
    return;
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = t("login.pwd_mismatch");
    return;
  }
  submitting.value = true;
  error.value = null;
  try {
    await resetPassword(resetToken, newPassword.value);
    resetDone.value = true;
    // 清掉 URL 里的一次性 token（已销毁，留着只会让刷新看到报错）
    router.replace({ query: {} });
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="relative flex h-screen w-full items-center justify-center bg-[var(--bg)] text-[var(--text)]">
    <!-- 登录前也能切语言：马来/英文客户第一屏即可选母语（顶栏切换器要登录后才有） -->
    <div class="absolute right-4 top-4">
      <LanguagePicker />
    </div>
    <!-- 登录 -->
    <form
      v-if="mode === 'login'"
      class="flex w-[320px] flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-6 shadow-[var(--shadow-drawer)]"
      @submit.prevent="submit"
    >
      <div class="text-center">
        <h1 class="text-lg font-semibold">KBase</h1>
        <p class="mt-1 text-sm text-[var(--text-2)]">{{ t("login.continue") }}</p>
      </div>

      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("login.username") }}</span>
        <Input v-model="username" autofocus :placeholder="t('login.username')" autocomplete="username" />
      </label>

      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("login.password") }}</span>
        <Input v-model="password" type="password" :placeholder="t('login.password')" autocomplete="current-password" />
      </label>

      <p v-if="error" class="rounded-[var(--radius-ctl)] bg-[var(--err-weak)] px-3 py-2 text-sm text-[var(--err)]">
        {{ error }}
      </p>

      <Button type="submit" :disabled="submitting" class="mt-1">
        {{ submitting ? t("login.logging_in") : t("login.login") }}
      </Button>

      <Button v-if="ssoEnabled" type="button" variant="outline" @click="ssoLogin">
        {{ t("login.sso") }}
      </Button>

      <button
        type="button"
        class="self-center text-xs text-[var(--text-3)] underline-offset-2 hover:text-[var(--text-2)] hover:underline"
        @click="switchMode('forgot')"
      >
        {{ t("login.forgot") }}
      </button>
    </form>

    <!-- 忘记密码 -->
    <form
      v-else-if="mode === 'forgot'"
      class="flex w-[320px] flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-6 shadow-[var(--shadow-drawer)]"
      @submit.prevent="submitForgot"
    >
      <div class="text-center">
        <h1 class="text-lg font-semibold">{{ t("login.forgot_title") }}</h1>
        <p class="mt-1 text-sm text-[var(--text-2)]">{{ t("login.forgot_hint") }}</p>
      </div>

      <template v-if="!forgotSent">
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("login.account") }}</span>
          <Input v-model="forgotAccount" autofocus :placeholder="t('login.account_placeholder')" @keydown.enter.prevent="submitForgot" />
        </label>

        <p v-if="error" class="rounded-[var(--radius-ctl)] bg-[var(--err-weak)] px-3 py-2 text-sm text-[var(--err)]">
          {{ error }}
        </p>

        <Button type="submit" :disabled="submitting || !forgotAccount.trim()">
          {{ submitting ? t("login.sending") : t("login.send_reset") }}
        </Button>
      </template>

      <p v-else class="rounded-[var(--radius-ctl)] bg-[var(--ok-weak)] px-3 py-2 text-sm text-[var(--ok)]">
        {{ forgotSent }}
      </p>

      <button
        type="button"
        class="self-center text-xs text-[var(--text-3)] underline-offset-2 hover:text-[var(--text-2)] hover:underline"
        @click="switchMode('login')"
      >
        {{ t("login.back_to_login") }}
      </button>
    </form>

    <!-- 重置密码（邮件链接落地） -->
    <form
      v-else
      class="flex w-[320px] flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-6 shadow-[var(--shadow-drawer)]"
      @submit.prevent="submitReset"
    >
      <div class="text-center">
        <h1 class="text-lg font-semibold">{{ t("login.reset_title") }}</h1>
        <p class="mt-1 text-sm text-[var(--text-2)]">{{ t("login.reset_hint") }}</p>
      </div>

      <template v-if="!resetDone">
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("login.new_pwd") }}</span>
          <Input v-model="newPassword" type="password" autofocus autocomplete="new-password" />
        </label>

        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("login.confirm_pwd") }}</span>
          <Input v-model="confirmPassword" type="password" autocomplete="new-password" @keydown.enter.prevent="submitReset" />
        </label>

        <p v-if="error" class="rounded-[var(--radius-ctl)] bg-[var(--err-weak)] px-3 py-2 text-sm text-[var(--err)]">
          {{ error }}
        </p>

        <Button type="submit" :disabled="submitting || !resetValid">
          {{ submitting ? t("login.submitting") : t("login.confirm_reset") }}
        </Button>
      </template>

      <template v-else>
        <p class="rounded-[var(--radius-ctl)] bg-[var(--ok-weak)] px-3 py-2 text-sm text-[var(--ok)]">
          {{ t("login.reset_done") }}
        </p>
        <Button type="button" @click="switchMode('login')">{{ t("login.go_login") }}</Button>
      </template>
    </form>
  </div>
</template>
