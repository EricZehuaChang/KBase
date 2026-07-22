<script setup lang="ts">
// 设置页编排（分栏版，生产 UX 反馈驱动）：左侧子导航分五组，右侧只渲染
// 选中组——此前全部区块堆一页纵向滚动，找配置要滚很久。选中组同步到
// URL ?tab=（深链接/刷新保位）。各功能卡片组件不动，本文件只管布局与
// Provider 区块自身的编排（测试状态按 provider 名集中持有 testStates）。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Boxes, Cpu, Gauge, Link2, Plus, Settings, Users } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import PageHeader from "@/components/PageHeader.vue";
import ProviderCard from "@/components/ProviderCard.vue";
import ProviderFormDialog from "@/components/ProviderFormDialog.vue";
import UserManagementCard from "@/components/UserManagementCard.vue";
import ApiKeyCard from "@/components/ApiKeyCard.vue";
import EmbedderKeysCard from "@/components/EmbedderKeysCard.vue";
import FeishuCard from "@/components/FeishuCard.vue";
import FeishuBotCard from "@/components/FeishuBotCard.vue";
import SmtpCard from "@/components/SmtpCard.vue";
import LicenseCard from "@/components/LicenseCard.vue";
import OpsDashboardCard from "@/components/OpsDashboardCard.vue";
import {
  settingsListProviders, deleteProvider, setActiveProvider, testProvider, healthz,
  currentRole,
  type Provider, type HealthzResponse,
} from "@/lib/api";
import { healthDot, type ProviderTestState } from "@/lib/settings-utils";
import { canAdminister } from "@/lib/auth-utils";
import { theme, setTheme } from "@/lib/theme";

const { t } = useI18n();

// ---- 分栏导航：五组，选中态走 URL ?tab=（刷新/分享保位） ----
const route = useRoute();
const router = useRouter();

// label/desc 存 i18n key，渲染时 t()。
const SECTIONS = [
  { id: "providers", label: "settings.sec.providers_label", icon: Cpu, desc: "settings.sec.providers_desc" },
  { id: "embedders", label: "settings.sec.embedders_label", icon: Boxes, desc: "settings.sec.embedders_desc" },
  { id: "access", label: "settings.sec.access_label", icon: Users, desc: "settings.sec.access_desc" },
  { id: "connectors", label: "settings.sec.connectors_label", icon: Link2, desc: "settings.sec.connectors_desc" },
  { id: "ops", label: "settings.sec.ops_label", icon: Gauge, desc: "settings.sec.ops_desc" },
  { id: "system", label: "settings.sec.system_label", icon: Settings, desc: "settings.sec.system_desc" },
] as const;
type SectionId = (typeof SECTIONS)[number]["id"];

const validIds = new Set(SECTIONS.map((s) => s.id));
const tab = ref<SectionId>(
  typeof route.query.tab === "string" && validIds.has(route.query.tab as SectionId)
    ? (route.query.tab as SectionId) : "providers");
watch(tab, (v) => {
  router.replace({ query: { ...route.query, tab: v } });
});
const currentSection = computed(() => SECTIONS.find((s) => s.id === tab.value)!);

const providers = ref<Provider[]>([]);
const active = ref<string | null>(null);
const loading = ref(true);

async function loadProviders() {
  loading.value = true;
  try {
    const r = await settingsListProviders();
    providers.value = r.providers;
    active.value = r.active;
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
}

const testStates = reactive<Record<string, ProviderTestState>>({});

async function handleTest(name: string) {
  testStates[name] = { status: "testing" };
  try {
    const r = await testProvider(name);
    testStates[name] = r.ok
      ? { status: "ok", latencyMs: r.latency_ms }
      : { status: "fail", error: r.error ?? t("settings.unknown_error") };
  } catch (err) {
    testStates[name] = { status: "fail", error: err instanceof Error ? err.message : String(err) };
  }
}

async function handleSetActive(name: string) {
  try {
    await setActiveProvider(name);
    toast.success(t("settings.set_active", { name }));
    await loadProviders();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

const deleteTarget = ref<Provider | null>(null);

async function confirmDelete() {
  if (!deleteTarget.value) return;
  const name = deleteTarget.value.name;
  try {
    await deleteProvider(name);
    toast.success(t("settings.deleted", { name }));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    deleteTarget.value = null;
    await loadProviders();
  }
}

const dialogOpen = ref(false);
const editTarget = ref<Provider | null>(null);

function openCreateDialog() {
  editTarget.value = null;
  dialogOpen.value = true;
}

function openEditDialog(p: Provider) {
  editTarget.value = p;
  dialogOpen.value = true;
}

function handleSaved(mode: "create" | "edit", name: string) {
  // 编辑成功后作废旧测试徽章——它对应修改前的配置，展示会误导
  if (mode === "edit") delete testStates[name];
  loadProviders();
}

const health = ref<HealthzResponse | null>(null);
const healthError = ref<string | null>(null);

async function loadHealth() {
  try {
    health.value = await healthz();
    healthError.value = null;
  } catch (err) {
    healthError.value = err instanceof Error ? err.message : String(err);
  }
}

onMounted(async () => {
  await Promise.all([loadProviders(), loadHealth()]);
});
</script>

<template>
  <div class="p-6">
    <PageHeader :title="t('admin.nav_settings')" :subtitle="t('settings.subtitle')">
      <template #actions>
        <Button v-if="tab === 'providers'" size="sm" @click="openCreateDialog">
          <Plus class="size-3.5" />
          {{ t("settings.add_provider") }}
        </Button>
      </template>
    </PageHeader>

    <div class="flex gap-6">
      <!-- 左侧分栏导航（Vben/Soybean 设置页范式）：每组一行图标+名称，
      选中态与管理端侧栏同语言（accent-weak 底+左指示条） -->
      <aside class="w-48 shrink-0">
        <nav class="sticky top-4 flex flex-col gap-0.5">
          <button
            v-for="s in SECTIONS"
            :key="s.id"
            type="button"
            class="relative flex items-center gap-2.5 rounded-[var(--radius-ctl)] px-3 py-2 text-left text-sm transition-colors"
            :class="tab === s.id
              ? 'bg-[var(--accent-weak)] font-medium text-[var(--accent-text)]'
              : 'text-[var(--text-2)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]'"
            @click="tab = s.id"
          >
            <span
              v-if="tab === s.id"
              class="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-[var(--accent)]"
            />
            <component :is="s.icon" class="size-4" />
            {{ t(s.label) }}
          </button>
        </nav>
      </aside>

      <!-- 右侧内容区：只渲染选中组 -->
      <div class="min-w-0 flex-1">
        <p class="mb-4 text-sm text-[var(--text-3)]">{{ t(currentSection.desc) }}</p>

        <!-- 模型服务 -->
        <section v-if="tab === 'providers'">
          <p v-if="loading" class="text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
          <p v-else-if="!providers.length" class="text-sm text-[var(--text-3)]">
            {{ t("settings.no_providers") }}
          </p>
          <div v-else class="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-4">
            <ProviderCard
              v-for="p in providers"
              :key="p.name"
              :provider="p"
              :is-active="p.name === active"
              :test-state="testStates[p.name]"
              @set-active="handleSetActive(p.name)"
              @edit="openEditDialog(p)"
              @delete="deleteTarget = p"
              @test="handleTest(p.name)"
            />
          </div>
        </section>

        <!-- 向量模型 -->
        <section v-else-if="tab === 'embedders'">
          <EmbedderKeysCard />
        </section>

        <!-- 用户与权限（admin；后端 require_admin 强制，v-if 仅 UX 防呆） -->
        <section v-else-if="tab === 'access'" class="flex flex-col gap-4">
          <template v-if="canAdminister(currentRole ?? '')">
            <UserManagementCard />
            <ApiKeyCard />
          </template>
        </section>

        <!-- 连接器（admin） -->
        <section v-else-if="tab === 'connectors'" class="flex flex-col gap-4">
          <template v-if="canAdminister(currentRole ?? '')">
            <FeishuCard />
            <FeishuBotCard />
          </template>
        </section>

        <!-- 运营看板 -->
        <section v-else-if="tab === 'ops'">
          <OpsDashboardCard v-if="canAdminister(currentRole ?? '')" />
        </section>

        <!-- 系统：状态 + 许可证 + 外观 -->
        <section v-else-if="tab === 'system'" class="flex flex-col gap-6">
          <div>
            <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">{{ t("settings.system_status") }}</h2>
            <div v-if="healthError" class="rounded-[var(--radius-card)] bg-[var(--err-weak)] p-4 text-sm text-[var(--err)]">
              ⚠️ {{ t("settings.health_failed") }}：{{ healthError }}
            </div>
            <div
              v-else-if="health"
              class="flex flex-wrap gap-6 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4"
            >
              <div v-for="key in (['status', 'embedder', 'vectorstore', 'reranker'] as const)" :key="key" class="flex items-center gap-2">
                <span class="size-2.5 rounded-full" :class="healthDot(health[key]).class" />
                <span class="text-sm text-[var(--text-2)]">
                  {{ key }}：<span class="text-[var(--text)]">{{ healthDot(health[key]).label }}</span>
                </span>
              </div>
            </div>
          </div>

          <SmtpCard v-if="canAdminister(currentRole ?? '')" />

          <LicenseCard v-if="canAdminister(currentRole ?? '')" />

          <div>
            <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">{{ t("settings.appearance") }}</h2>
            <div class="inline-flex rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] p-0.5">
              <button
                type="button"
                class="rounded-[calc(var(--radius-ctl)-2px)] px-3 py-1.5 text-sm transition-colors"
                :class="theme === 'light' ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]' : 'text-[var(--text-2)]'"
                @click="setTheme('light')"
              >
                {{ t("settings.theme_light") }}
              </button>
              <button
                type="button"
                class="rounded-[calc(var(--radius-ctl)-2px)] px-3 py-1.5 text-sm transition-colors"
                :class="theme === 'dark' ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]' : 'text-[var(--text-2)]'"
                @click="setTheme('dark')"
              >
                {{ t("settings.theme_dark") }}
              </button>
            </div>
          </div>
        </section>
      </div>
    </div>
  </div>

  <ProviderFormDialog v-model:open="dialogOpen" :provider="editTarget" @saved="handleSaved" />

  <!-- 删除确认 Dialog -->
  <Dialog :open="!!deleteTarget" @update:open="(v) => { if (!v) deleteTarget = null; }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("settings.delete_provider") }}</DialogTitle>
        <DialogDescription>
          {{ t("settings.delete_provider_confirm", { name: deleteTarget?.name }) }}
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="deleteTarget = null">{{ t("common.cancel") }}</Button>
        <Button variant="destructive" @click="confirmDelete">{{ t("common.confirm_delete") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
