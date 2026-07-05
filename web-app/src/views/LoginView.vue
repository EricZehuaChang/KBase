<script setup lang="ts">
// 独立登录页（不套 AppShell，见 App.vue 按路由分流渲染）。成功后跳转到
// router.query.redirect 指定的原目标路径（redirectTarget 纯函数校验，防开
// 放重定向），无 redirect 时回首页。
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { login, clearSessionCache } from "@/lib/api";
import { redirectTarget } from "@/lib/auth-utils";

const route = useRoute();
const router = useRouter();

const username = ref("");
const password = ref("");
const error = ref<string | null>(null);
const submitting = ref(false);

async function submit() {
  if (!username.value.trim() || !password.value) {
    error.value = "请输入用户名和密码";
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
</script>

<template>
  <div class="flex h-screen w-full items-center justify-center bg-[var(--bg)] text-[var(--text)]">
    <form
      class="flex w-[320px] flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-6 shadow-[var(--shadow-drawer)]"
      @submit.prevent="submit"
    >
      <div class="text-center">
        <h1 class="text-lg font-semibold">KBase</h1>
        <p class="mt-1 text-sm text-[var(--text-2)]">登录以继续</p>
      </div>

      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">用户名</span>
        <Input v-model="username" autofocus placeholder="用户名" autocomplete="username" />
      </label>

      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">密码</span>
        <Input v-model="password" type="password" placeholder="密码" autocomplete="current-password" />
      </label>

      <p v-if="error" class="rounded-[var(--radius-ctl)] bg-[var(--err-weak)] px-3 py-2 text-sm text-[var(--err)]">
        {{ error }}
      </p>

      <Button type="submit" :disabled="submitting" class="mt-1">
        {{ submitting ? "登录中…" : "登录" }}
      </Button>
    </form>
  </div>
</template>
