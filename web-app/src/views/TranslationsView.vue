<script setup lang="ts">
// 【管理端】多语言译文管理页（P1-7）：全部 i18n key × 三语，可编辑保存到 DB
// 覆盖层（PUT /api/i18n，空值=删除覆盖回落基线）。基线来自随包 locales/*.json，
// DB 覆盖来自 GET /api/i18n，合并展示。保存后并入运行时 i18n 实例（当前会话
// 即时生效），其他会话下次加载生效（方案 A）。
// P2-4 增强：搜索（key 或任一语言译文）/ 按域筛选 / 缺译高亮 / 已改标记 /
// 一键还原（清空即删除覆盖回落基线）。
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Search } from "@lucide/vue";
import { Input } from "@/components/ui/input";
import PageHeader from "@/components/PageHeader.vue";
import { LANGUAGES } from "@/i18n/languages";
import { i18n } from "@/i18n";
import { getAllI18nOverrides, putI18nOverride } from "@/lib/api";
import enBase from "@/i18n/locales/en.json";
import msBase from "@/i18n/locales/ms.json";
import zhBase from "@/i18n/locales/zh.json";

const { t } = useI18n();

// ---- 基线扁平化（key→value），zh 为源，key 集合以 zh 为准 ----
function flatten(obj: Record<string, unknown>, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(out, flatten(v as Record<string, unknown>, key));
    else out[key] = String(v);
  }
  return out;
}
const BASE: Record<string, Record<string, string>> = {
  zh: flatten(zhBase), en: flatten(enBase), ms: flatten(msBase),
};
const ALL_KEYS = Object.keys(BASE.zh).sort();
const DOMAINS = [...new Set(ALL_KEYS.map((k) => k.split(".")[0]))].sort();

// 单 key 还原嵌套（保存后并入运行时实例，供当前会话即时生效）
function unflatten(key: string, value: string): Record<string, unknown> {
  const parts = key.split(".");
  const out: Record<string, unknown> = {};
  let cur = out;
  for (let i = 0; i < parts.length - 1; i++) cur = (cur[parts[i]] = {}) as Record<string, unknown>;
  cur[parts[parts.length - 1]] = value;
  return out;
}

// ---- DB 覆盖 {lang:{key:value}} ----
const overrides = reactive<Record<string, Record<string, string>>>({ zh: {}, en: {}, ms: {} });
const loading = ref(true);

async function load() {
  loading.value = true;
  try {
    const data = await getAllI18nOverrides();
    for (const l of LANGUAGES) overrides[l.code] = data[l.code] ?? {};
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
}
onMounted(load);

// 生效值 = DB 覆盖 ?? 基线；已改 = 有非空覆盖；缺译 = 生效为空
function effective(lang: string, key: string): string {
  const ov = overrides[lang]?.[key];
  return ov != null && ov !== "" ? ov : (BASE[lang]?.[key] ?? "");
}
function isOverridden(lang: string, key: string): boolean {
  const ov = overrides[lang]?.[key];
  return ov != null && ov !== "";
}
function isMissing(lang: string, key: string): boolean {
  return effective(lang, key) === "";
}

// 编辑草稿：key="lang::key"，未编辑时取生效值
const drafts = reactive<Record<string, string>>({});
function draftKey(lang: string, key: string) { return `${lang}::${key}`; }
function inputVal(lang: string, key: string): string {
  const dk = draftKey(lang, key);
  return dk in drafts ? drafts[dk] : effective(lang, key);
}
function onInput(lang: string, key: string, val: string) {
  drafts[draftKey(lang, key)] = val;
}

const saving = ref<Set<string>>(new Set());

async function save(lang: string, key: string) {
  const dk = draftKey(lang, key);
  if (!(dk in drafts)) return;                       // 没动过
  const val = drafts[dk];
  if (val === effective(lang, key)) { delete drafts[dk]; return; }  // 与现值相同，跳过
  // 与基线相同 → 送空串删除覆盖（保持覆盖表只存真正的增量）
  const toSend = val === (BASE[lang]?.[key] ?? "") ? "" : val;
  saving.value = new Set(saving.value).add(dk);
  try {
    const r = await putI18nOverride(lang, key, toSend);
    if (r.result === "deleted") delete overrides[lang][key];
    else overrides[lang] = { ...overrides[lang], [key]: toSend };
    // 当前会话即时生效：并入运行时 i18n（DB 优先）
    i18n.global.mergeLocaleMessage(lang, unflatten(key, effective(lang, key)) as never);
    delete drafts[dk];
    toast.success(t("translations.saved"));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    const next = new Set(saving.value); next.delete(dk); saving.value = next;
  }
}

// ---- 筛选 ----
const search = ref("");
const domain = ref("all");
const onlyMissing = ref(false);

const filteredKeys = computed(() => {
  const q = search.value.trim().toLowerCase();
  return ALL_KEYS.filter((key) => {
    if (domain.value !== "all" && key.split(".")[0] !== domain.value) return false;
    if (onlyMissing.value && !LANGUAGES.some((l) => l.code !== "zh" && isMissing(l.code, key))) return false;
    if (!q) return true;
    if (key.toLowerCase().includes(q)) return true;
    return LANGUAGES.some((l) => effective(l.code, key).toLowerCase().includes(q));
  });
});
</script>

<template>
  <div class="p-6">
    <PageHeader :title="t('translations.title')" :subtitle="t('translations.subtitle')" />

    <!-- 工具条：搜索 / 域筛选 / 只看缺译 -->
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <div class="relative">
        <Search class="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--text-3)]" />
        <Input v-model="search" class="w-64 pl-8" :placeholder="t('translations.search_ph')" />
      </div>
      <select
        v-model="domain"
        class="h-9 rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-2 text-sm text-[var(--text)] outline-none"
      >
        <option value="all">{{ t('translations.all_domains') }}</option>
        <option v-for="d in DOMAINS" :key="d" :value="d">{{ d }}</option>
      </select>
      <label class="flex items-center gap-1.5 text-sm text-[var(--text-2)]">
        <input v-model="onlyMissing" type="checkbox" class="accent-[var(--accent)]" />
        {{ t('translations.only_missing') }}
      </label>
      <span class="ml-auto text-xs text-[var(--text-3)]">
        {{ t('translations.count', { shown: filteredKeys.length, total: ALL_KEYS.length }) }}
      </span>
    </div>

    <p v-if="loading" class="text-sm text-[var(--text-3)]">{{ t('common.loading') }}</p>
    <div v-else class="overflow-x-auto rounded-[var(--radius-card)] border border-[var(--border)]">
      <table class="w-full min-w-[720px] text-sm">
        <thead class="sticky top-0 bg-[var(--surface-2)] text-left text-xs text-[var(--text-3)]">
          <tr>
            <th class="w-64 px-3 py-2 font-medium">Key</th>
            <th v-for="l in LANGUAGES" :key="l.code" class="px-3 py-2 font-medium">{{ l.name }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="key in filteredKeys"
            :key="key"
            class="border-t border-[var(--border)] align-top"
          >
            <td class="px-3 py-1.5">
              <code class="break-all font-mono text-xs text-[var(--text-2)]">{{ key }}</code>
            </td>
            <td v-for="l in LANGUAGES" :key="l.code" class="px-2 py-1.5">
              <div class="relative">
                <textarea
                  :value="inputVal(l.code, key)"
                  rows="1"
                  class="w-full resize-y rounded-[var(--radius-ctl)] border bg-[var(--surface)] px-2 py-1 text-sm text-[var(--text)] outline-none focus:border-[var(--accent)]"
                  :class="isMissing(l.code, key) ? 'border-[var(--warn)] bg-[var(--warn-weak)]'
                    : isOverridden(l.code, key) ? 'border-[var(--accent)]' : 'border-[var(--border)]'"
                  :disabled="saving.has(`${l.code}::${key}`)"
                  @input="onInput(l.code, key, ($event.target as HTMLTextAreaElement).value)"
                  @blur="save(l.code, key)"
                  @keydown.enter.exact.prevent="save(l.code, key)"
                />
                <span
                  v-if="isOverridden(l.code, key)"
                  class="absolute -top-1.5 right-1 rounded bg-[var(--accent-weak)] px-1 text-[9px] text-[var(--accent-text)]"
                >{{ t('translations.edited') }}</span>
                <span
                  v-else-if="isMissing(l.code, key)"
                  class="absolute -top-1.5 right-1 rounded bg-[var(--warn-weak)] px-1 text-[9px] text-[var(--warn)]"
                >{{ t('translations.missing') }}</span>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="mt-2 text-xs text-[var(--text-3)]">{{ t('translations.hint') }}</p>
  </div>
</template>
