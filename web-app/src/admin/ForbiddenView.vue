<script setup lang="ts">
// 【管理端】"无权限访问"终态页——viewer 携带有效会话访问 /admin 时的落地页
// （src/admin/guard.ts decideAdminLanding 判定，路由名 admin-forbidden）。
// 特意不做自动倒计时/自动跳转：一是避免用户还没看清提示文案就被强制带走，
// 二是本路由本身不再被 beforeEach 拦截判定（见 router.ts 注释），如果这里
// 自动重定向回某个受守卫保护的路径，反而可能绕回循环跳转——保持"停在这里，
// 给一个显式链接"是最简单也最不会出错的做法。
</script>

<template>
  <div class="flex h-screen w-full flex-col items-center justify-center gap-4 bg-[var(--bg)] px-6 text-center text-[var(--text)]">
    <h1 class="text-lg font-semibold">无权限访问管理端</h1>
    <p class="max-w-sm text-sm text-[var(--text-2)]">
      当前账号没有管理知识库的权限。如需管理权限请联系管理员，或返回使用端继续问答。
    </p>
    <a
      href="/"
      class="rounded-[var(--radius-ctl)] border border-[var(--border)] px-4 py-2 text-sm text-[var(--text)] transition-colors hover:bg-[var(--surface-2)]"
    >
      返回使用端
    </a>
  </div>
</template>
