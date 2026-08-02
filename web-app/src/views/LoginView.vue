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
import HeroGlobe from "@/components/HeroGlobe.vue";
import FeatureCarousel from "@/components/FeatureCarousel.vue";
import {
  login, clearSessionCache, getSession, getSsoStatus, forgotPassword,
  resetPassword,
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
const remember = ref(false);
const error = ref<string | null>(null);
const submitting = ref(false);

// M6-8 企业 SSO：后端启用 OIDC 时显示企业账号入口（整页跳转到 IdP）
const ssoEnabled = ref(false);
onMounted(async () => {
  // 已登录访客直接跳走（带着有效会话打开 /login 没有意义；重置密码模式
  // 例外——邮件链接落地是明确意图，哪怕登录着也让人把密码改完）
  if (mode.value === "login") {
    try {
      if (await getSession()) {
        await router.replace(redirectTarget(route));
        return;
      }
    } catch {
      // 探测失败当作未登录，正常展示登录表单
    }
  }
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
    await login(username.value.trim(), password.value, remember.value);
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
  <!-- 登录页=单一深色世界（有意的单主题承诺：登录后才进应用的浅/暗双主题）。
  整页共用一套暗色令牌覆写——Input/Button/LanguagePicker 全部经由令牌别名链
  （--input: var(--border) 等）原生暗色化，组件零改动。消灭旧版"左黑右白
  断崖"：地球弧移到表单背后，玻璃卡浮在地球上，两个世界合为一个。 -->
  <div
    class="relative flex h-screen w-full overflow-hidden bg-[#0a0b14] text-white"
    style="--bg: #0a0b14; --surface: rgba(255,255,255,0.06); --surface-2: rgba(255,255,255,0.09);
           --border: rgba(255,255,255,0.14); --border-strong: rgba(255,255,255,0.28);
           --text: #fff; --text-2: rgba(255,255,255,0.72); --text-3: rgba(255,255,255,0.5);
           --accent-weak: rgba(101,92,255,0.22); --accent-text: #A7B0FF;
           --err: #f87171; --err-weak: rgba(248,113,113,0.12);
           --ok: #4ade80; --ok-weak: rgba(74,222,128,0.12)"
  >
    <!-- 全页辉光：accent 同色相两档（左上主光 + 表单区背光） -->
    <div
      class="pointer-events-none absolute inset-0"
      style="background:
        radial-gradient(50% 44% at 8% 0%, rgba(101,92,255,0.28), transparent 62%),
        radial-gradient(44% 52% at 82% 55%, rgba(56,89,255,0.16), transparent 62%)"
    />
    <!-- 签名构图：地球移到表单背后，玻璃卡浮在地球弧上 -->
    <div class="absolute right-[2%] top-1/2 aspect-square w-[46%] min-w-[26rem] -translate-y-1/2 opacity-80">
      <HeroGlobe />
    </div>

    <!-- 左：品牌内容列（lg+ 显示；陈述句压光学中心，轮播钉底） -->
    <aside class="relative z-10 hidden w-[55%] shrink-0 grid-rows-[auto_1fr_auto_1.2fr_auto] px-12 pb-12 pt-10 lg:grid">
      <div class="login-rise relative flex items-center gap-2.5">
        <span class="flex size-9 items-center justify-center rounded-xl bg-[var(--accent)] text-lg font-bold text-white shadow-lg shadow-indigo-950/50">K</span>
        <span class="text-xl font-semibold tracking-tight">KBase</span>
      </div>

      <div />

      <div class="login-rise relative z-10 max-w-[24rem]" style="animation-delay: 0.12s">
        <h2
          class="bg-gradient-to-r from-white to-[#A7B0FF] bg-clip-text text-[30px] font-semibold leading-[1.3] tracking-tight text-transparent"
          style="text-wrap: balance"
        >
          {{ t("login.hero_title") }}
        </h2>
        <p class="mt-4 text-sm leading-[1.75] text-white/65">
          {{ t("login.tagline") }}
        </p>
      </div>

      <div />

      <!-- 玻璃拟态轮播卡：backdrop-blur 压住地球边缘，层次分明 -->
      <div class="login-rise relative z-10 max-w-[24rem] rounded-2xl border border-white/10 bg-white/[0.06] p-5 backdrop-blur-md" style="animation-delay: 0.22s">
        <FeatureCarousel />
      </div>
    </aside>

    <!-- 右：表单列。玻璃卡浮在地球弧上（backdrop-blur 保证任何底纹下可读） -->
    <div class="relative z-10 flex min-w-0 flex-1 items-center justify-center">
    <!-- 登录前也能切语言：马来/英文客户第一屏即可选母语（顶栏切换器要登录后
    才有）。inline 文字行放页脚居中——比角落悬浮地球图标融入页面，且母语自称
    （中文 · English · Bahasa Melayu）对不识中文的访客一眼可认。 -->
    <div class="absolute inset-x-0 bottom-8 flex justify-center">
      <LanguagePicker />
    </div>
    <!-- 登录 -->
    <form
      v-if="mode === 'login'"
      class="login-rise relative z-10 flex w-[380px] max-w-[calc(100vw-2.5rem)] flex-col gap-4 rounded-2xl border border-white/10 bg-white/[0.07] p-8 shadow-2xl shadow-black/40 backdrop-blur-xl"
      @submit.prevent="submit"
    >
      <div class="mb-2">
        <div class="mb-5 flex items-center gap-2 lg:hidden">
          <span class="flex size-8 items-center justify-center rounded-lg bg-[var(--accent)] text-base font-bold text-white">K</span>
          <span class="text-lg font-semibold tracking-tight">KBase</span>
        </div>
        <h1 class="text-[22px] font-semibold tracking-tight">{{ t("login.welcome") }}</h1>
        <p class="mt-1.5 text-[13px] text-[var(--text-3)]">{{ t("login.continue") }}</p>
      </div>

      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("login.username") }}</span>
        <Input v-model="username" autofocus :placeholder="t('login.username')" autocomplete="username" />
      </label>

      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("login.password") }}</span>
        <Input v-model="password" type="password" :placeholder="t('login.password')" autocomplete="current-password" />
      </label>

      <!-- 记住登录（勾=30 天持久 Cookie，默认关浏览器即失效）与忘记密码同行：
      辅助动作各归一侧，不再让找回入口沉底居中 -->
      <div class="flex items-center justify-between">
        <label class="flex cursor-pointer items-center gap-2 text-[13px] text-[var(--text-2)]">
          <input v-model="remember" type="checkbox" class="accent-[var(--accent)]" />
          {{ t("login.remember") }}
        </label>
        <button
          type="button"
          class="text-[13px] text-[var(--text-3)] underline-offset-2 hover:text-[var(--accent-text)] hover:underline"
          @click="switchMode('forgot')"
        >
          {{ t("login.forgot") }}
        </button>
      </div>

      <p v-if="error" class="rounded-[var(--radius-ctl)] bg-[var(--err-weak)] px-3 py-2 text-sm text-[var(--err)]">
        {{ error }}
      </p>

      <Button type="submit" :disabled="submitting" class="mt-1 h-10 text-[15px]">
        {{ submitting ? t("login.logging_in") : t("login.login") }}
      </Button>

      <template v-if="ssoEnabled">
        <div class="flex items-center gap-3 text-xs text-[var(--text-3)]">
          <span class="h-px flex-1 bg-[var(--border)]" />
          {{ t("login.or") }}
          <span class="h-px flex-1 bg-[var(--border)]" />
        </div>
        <Button type="button" variant="outline" @click="ssoLogin">
          {{ t("login.sso") }}
        </Button>
      </template>
    </form>

    <!-- 忘记密码 -->
    <form
      v-else-if="mode === 'forgot'"
      class="login-rise relative z-10 flex w-[380px] max-w-[calc(100vw-2.5rem)] flex-col gap-4 rounded-2xl border border-white/10 bg-white/[0.07] p-8 shadow-2xl shadow-black/40 backdrop-blur-xl"
      @submit.prevent="submitForgot"
    >
      <div class="mb-2">
        <h1 class="text-[22px] font-semibold tracking-tight">{{ t("login.forgot_title") }}</h1>
        <p class="mt-1.5 text-[13px] text-[var(--text-3)]">{{ t("login.forgot_hint") }}</p>
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
      class="login-rise relative z-10 flex w-[380px] max-w-[calc(100vw-2.5rem)] flex-col gap-4 rounded-2xl border border-white/10 bg-white/[0.07] p-8 shadow-2xl shadow-black/40 backdrop-blur-xl"
      @submit.prevent="submitReset"
    >
      <div class="mb-2">
        <h1 class="text-[22px] font-semibold tracking-tight">{{ t("login.reset_title") }}</h1>
        <p class="mt-1.5 text-[13px] text-[var(--text-3)]">{{ t("login.reset_hint") }}</p>
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
  </div>
</template>

<style scoped>
/* 入场动效：品牌面板与表单卡片淡入上移（动效敏感用户直接呈现终态） */
.login-rise,
form {
  animation: login-rise 0.5s ease-out both;
}
@keyframes login-rise {
  from {
    opacity: 0;
    transform: translateY(14px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@media (prefers-reduced-motion: reduce) {
  .login-rise,
  form {
    animation: none;
  }
}
</style>
