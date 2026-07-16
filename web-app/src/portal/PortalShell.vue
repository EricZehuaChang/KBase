<script setup lang="ts">
// 【使用端】根组件（Vite 入口 index.html 挂载）。/login 走独立居中卡片页
// （不带顶栏，逻辑与分端改造前的 App.vue 一致）；其余路由套一层顶栏——
// logo、KB/模型选择器、主题切换、用户名+角色徽章+登出，以及仅 editor/admin
// 可见的"进入工作台"入口（spec §4：viewer 只能用使用端）。
//
// M5-1 F2：KB/模型选择器从 ChatHome（原 ChatView）搬到这里——两个原因：
// ①这两个选择器语义上是"当前使用端会话的上下文"，理应跨页面（哪怕使用端
// 目前只有问答一个页面）常驻在顶栏，而不是绑死在某个路由视图里；
// ②F1 遗留的 #sidebar-slot Teleport 挂载点在这版一并移除——ChatHome 的
// 会话侧栏已经改成原生子组件（见 ChatHome.vue/SessionSidebar.vue），不再
// 需要跨组件树注入，PortalShell 不必再为它开一个挂载点。
import { ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { KeyRound, LogOut, Sun, Moon } from "@lucide/vue";
import ChangePasswordDialog from "@/components/ChangePasswordDialog.vue";
import EmailPromptDialog from "@/components/EmailPromptDialog.vue";
import { getSession, logout, type Me } from "@/lib/api";
import { roleLabel, roleBadgeClass, canManageContent } from "@/lib/auth-utils";
import { theme, toggleTheme } from "@/lib/theme";
import { kbs, kbId, providers, provider, extraKbIds, ensureTopbarLoaded } from "./topbar-state";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Toaster } from "@/components/ui/sonner";

const route = useRoute();
const router = useRouter();

const me = ref<Me | null>(null);
// 路由守卫已确保能到达非 /login 路由时会话必然存在，这里复用同一份缓存
// （getSession）读用户名/角色展示，不再单独发一次请求（与现状 AppShell 的
// 既有取舍一致）。KB/Provider 列表同理在这里一并触发加载（ensureTopbarLoaded
// 内部幂等，见 topbar-state.ts），比在 ChatHome 里 onMounted 拉更符合"顶栏
// 持有这份状态"的归属。
//
// 用 watch(route.path) 而不是组件顶层一次性 if：PortalShell 是整个使用端
// bundle 唯一常驻的根组件——LoginView 登录成功后走 router.replace()（SPA
// 内导航，不整页刷新，见 LoginView.vue），PortalShell 不会重新 mount，
// 顶层的一次性判断只会在"应用刚启动、还停在 /login"那一刻求值一次，之后
// route.path 变成 "/" 也不会重新触发——之前这里就是一次性 if，实测登录后
// 顶栏 KB/模型选择器与用户名/角色徽章一直空着，得手动刷新页面才会出现。
// watch 能在每次 route.path 变化时重新判断，immediate:true 顶上"应用启动
// 时已经带着有效 Cookie 直接落地到 /"这种首次求值场景。
watch(() => route.path, (path) => {
  if (path === "/login") return;
  getSession().then((session) => {
    me.value = session;
    // 首登邮箱引导：没绑邮箱就提醒补（忘记密码重置的唯一通道）。
    // "稍后再说"记 sessionStorage，本次浏览器会话内不再弹。
    if (session && session.email === null
        && !sessionStorage.getItem("kbase_email_prompt_dismissed")) {
      emailPromptOpen.value = true;
    }
  });
  void ensureTopbarLoaded();
}, { immediate: true });

const changePwOpen = ref(false);
const emailPromptOpen = ref(false);

function handleEmailSaved(email: string) {
  // 原地改而不是换新对象：getSession 缓存的就是这个引用，换新对象的话
  // 下次路由变化 watch 重读缓存仍是 email:null，会再弹一次
  if (me.value) me.value.email = email;
}

async function handleLogout() {
  try {
    await logout();
  } finally {
    await router.push("/login");
  }
}

// M6-2：主库之外可联查的候选库（排除主库自身）；勾/取消维护 extraKbIds。
function toggleExtraKb(id: string) {
  const next = new Set(extraKbIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  extraKbIds.value = [...next];
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
    <header class="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] px-4">
      <div class="flex items-center gap-4">
        <div class="text-lg font-semibold tracking-tight">KBase</div>
        <Select v-model="kbId">
          <SelectTrigger class="w-44"><SelectValue placeholder="选择知识库" /></SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem v-for="kb in kbs" :key="kb.id" :value="kb.id">{{ kb.name }}</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
        <!-- M6-2 多库联合问答：勾选主库之外要一起检索的库，只作用于新会话 -->
        <Popover v-if="kbs.length > 1">
          <PopoverTrigger as-child>
            <button
              type="button"
              class="rounded-[var(--radius-ctl)] border px-2.5 py-1.5 text-sm transition-colors"
              :class="extraKbIds.length
                ? 'border-[var(--accent)] text-[var(--accent-text)]'
                : 'border-[var(--border)] text-[var(--text-2)] hover:bg-[var(--surface-2)]'"
            >
              {{ extraKbIds.length ? `联合 ${extraKbIds.length + 1} 库` : "+ 联查" }}
            </button>
          </PopoverTrigger>
          <PopoverContent class="w-56 p-2">
            <p class="px-1 pb-2 text-xs text-[var(--text-3)]">
              新会话将同时检索勾选的库（跨库联合问答）
            </p>
            <label
              v-for="kb in kbs.filter((k) => k.id !== kbId)"
              :key="kb.id"
              class="flex cursor-pointer items-center gap-2 rounded-[var(--radius-ctl)] px-1 py-1.5 text-sm hover:bg-[var(--surface-2)]"
            >
              <input
                type="checkbox"
                class="accent-[var(--accent)]"
                :checked="extraKbIds.includes(kb.id)"
                @change="toggleExtraKb(kb.id)"
              />
              <span class="truncate">{{ kb.name }}</span>
            </label>
          </PopoverContent>
        </Popover>
        <Select v-model="provider">
          <SelectTrigger class="w-44"><SelectValue placeholder="选择模型" /></SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem v-for="p in providers" :key="p" :value="p">{{ p }}</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </div>
      <div class="flex items-center gap-3">
        <button
          v-if="me && canManageContent(me.role)"
          type="button"
          class="rounded-[var(--radius-ctl)] border border-[var(--border)] px-3 py-1.5 text-sm text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
          @click="enterWorkbench"
        >
          进入工作台
        </button>
        <button
          type="button"
          class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
          :title="theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'"
          @click="toggleTheme"
        >
          <component :is="theme === 'dark' ? Sun : Moon" class="size-4" />
        </button>
        <div v-if="me" class="flex items-center gap-2">
          <button
            type="button"
            class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
            title="修改密码"
            @click="changePwOpen = true"
          >
            <KeyRound class="size-4" />
          </button>
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

    <main class="min-h-0 flex-1 overflow-hidden">
      <router-view />
    </main>
  </div>
  <ChangePasswordDialog v-model:open="changePwOpen" />
  <EmailPromptDialog v-model:open="emailPromptOpen" @saved="handleEmailSaved" />
  <Toaster />
</template>
