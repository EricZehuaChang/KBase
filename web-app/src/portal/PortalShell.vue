<script setup lang="ts">
// 【使用端】根组件（Vite 入口 index.html 挂载）。/login 走独立居中卡片页
// （不带顶栏，逻辑与分端改造前的 App.vue 一致）；其余路由套一层极简顶栏——
// logo、用户名+角色徽章+登出、以及仅 editor/admin 可见的"进入工作台"入口
// （spec §4：viewer 只能用使用端）。F1 只搭这层壳，问答本身仍是现状
// ChatView（未重构），会话侧栏/内联引用/快捷问题/停止生成等体验项留给 F2。
//
// 关键耦合点：ChatView 内部用 Teleport to="#sidebar-slot" 把会话列表注入
// 分端改造前 AppShell 提供的挂载点——这里必须保留同名挂载点（下面的
// <aside id="sidebar-slot">），否则 Vue 找不到 Teleport 目标时只会静默
// warning、不报错，会话侧栏会被悄悄丢弃而不易发现。F2 重构 ChatView 时会
// 连带清理这层耦合，把会话状态收归 PortalShell 原生持有。
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { LogOut } from "@lucide/vue";
import { getSession, logout, type Me } from "@/lib/api";
import { roleLabel, roleBadgeClass, canManageContent } from "@/lib/auth-utils";
import { Toaster } from "@/components/ui/sonner";

const route = useRoute();
const router = useRouter();

const me = ref<Me | null>(null);
// 路由守卫已确保能到达非 /login 路由时会话必然存在，这里复用同一份缓存
// （getSession）读用户名/角色展示，不再单独发一次请求（与现状 AppShell 的
// 既有取舍一致）。
if (route.path !== "/login") {
  getSession().then((session) => { me.value = session; });
}

async function handleLogout() {
  try {
    await logout();
  } finally {
    await router.push("/login");
  }
}

function enterWorkbench() {
  // 整页跳转（不是 router.push）：管理端是另一个 Vite 入口的独立 bundle，
  // 没有共享的 router 实例可以导航过去。
  window.location.href = "/admin/";
}
</script>

<template>
  <router-view v-if="route.path === '/login'" />
  <div
    v-else
    class="flex h-screen w-full flex-col overflow-hidden bg-[var(--bg)] text-[var(--text)]"
  >
    <header class="flex h-14 shrink-0 items-center justify-between border-b border-[var(--border)] px-4">
      <div class="text-lg font-semibold tracking-tight">KBase</div>
      <div class="flex items-center gap-3">
        <button
          v-if="me && canManageContent(me.role)"
          type="button"
          class="rounded-[var(--radius-ctl)] border border-[var(--border)] px-3 py-1.5 text-sm text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
          @click="enterWorkbench"
        >
          进入工作台
        </button>
        <div v-if="me" class="flex items-center gap-2">
          <span class="text-sm text-[var(--text)]">{{ me.username }}</span>
          <span class="w-fit rounded-full px-1.5 py-0.5 text-xs" :class="roleBadgeClass(me.role)">
            {{ roleLabel(me.role) }}
          </span>
        </div>
        <button
          type="button"
          class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
          title="登出"
          @click="handleLogout"
        >
          <LogOut class="size-4" />
        </button>
      </div>
    </header>

    <div class="flex min-h-0 flex-1">
      <!-- ChatView 会话侧栏的 Teleport 挂载点，见上方脚本注释 -->
      <aside
        id="sidebar-slot"
        class="flex w-[208px] shrink-0 flex-col overflow-y-auto border-r border-[var(--border)] bg-[var(--surface)] px-2 py-2"
      />
      <main class="min-h-0 flex-1 overflow-y-auto">
        <router-view />
      </main>
    </div>
  </div>
  <Toaster />
</template>
