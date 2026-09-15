<script setup lang="ts">
// 许可证状态卡片（设置页，仅 admin 可见）：状态徽章 + org/expires、T16 起
// 追加版本（edition）/席位（seats）/证书格式与宽限期剩余天数，并提供**离线
// 续期上传入口**。
// 为什么上传入口必须在这里：授权到期且 enforce=true 时业务端点一律 402，
// 现场（常常完全离线）唯一的自助续期方式就是管理员登录后上传新的
// license.json（后端 POST /api/license，先生成验签再原子替换）。AppShell
// 顶部横幅（用 licenseBannerInfo）是同一份数据在全局层面的提示，这里是
// 设置页内的详情视图 + 续期动作。
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { getLicense, uploadLicense, type LicenseInfo } from "@/lib/api";

const { t } = useI18n();

const STATUS_BADGE_CLASS: Record<string, string> = {
  trial: "bg-[var(--accent-weak)] text-[var(--accent-text)]",
  valid: "bg-[var(--ok-weak)] text-[var(--ok)]",
  expired: "bg-[var(--warn-weak)] text-[var(--warn)]",
  invalid: "bg-[var(--err-weak)] text-[var(--err)]",
};

const license = ref<LicenseInfo | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const uploading = ref(false);

async function refresh() {
  license.value = await getLicense();
}

onMounted(async () => {
  try {
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
});

function openPicker() {
  fileInput.value?.click();
}

async function handleFile(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  // 允许连续选择同一个文件重传（否则第二次 change 不触发）
  input.value = "";
  if (!file) return;
  uploading.value = true;
  try {
    const result = await uploadLicense(file);
    // 服务端返回落盘后的真实状态，直接用它刷新卡片（避免再打一次接口）
    license.value = result.license;
    toast.success(t("license.upload_done", {
      org: result.license.org ?? "",
      date: result.license.expires ?? "",
    }));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    uploading.value = false;
  }
}
</script>

<template>
  <section class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">{{ t("license.title") }}</h2>
    <div v-if="license" class="flex flex-wrap items-center gap-3 text-sm">
      <Badge :class="STATUS_BADGE_CLASS[license.status]">{{ t(`license.status.${license.status}`) }}</Badge>
      <span v-if="license.org" class="text-[var(--text-2)]">{{ t("license.org", { org: license.org }) }}</span>
      <span v-if="license.expires" class="text-[var(--text-2)]">{{ t("license.expires", { date: license.expires }) }}</span>
      <span v-if="license.edition" class="text-[var(--text-2)]">{{ t("license.edition", { edition: license.edition }) }}</span>
      <span v-if="license.seats" class="text-[var(--text-2)]">{{ t("license.seats", { seats: license.seats }) }}</span>
      <span v-if="license.format" class="text-[var(--text-3)]">{{ t("license.format", { format: license.format }) }}</span>
      <!-- 宽限期只对"已到期"有意义：过期后仍可用的剩余天数（0 = 已用尽，
           此时 enforce=true 的部署已经开始拦截业务端点） -->
      <span
        v-if="license.status === 'expired'"
        class="text-[var(--warn)]"
      >
        {{ (license.grace_days_left ?? 0) > 0
          ? t("license.grace_left", { days: license.grace_days_left })
          : t("license.grace_used") }}
      </span>
    </div>
    <p v-else class="text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>

    <div class="mt-4 border-t border-[var(--border)] pt-3">
      <p class="mb-2 text-xs text-[var(--text-3)]">{{ t("license.upload_hint") }}</p>
      <Button variant="outline" size="sm" :disabled="uploading" @click="openPicker">
        {{ uploading ? t("common.loading") : t("license.upload_button") }}
      </Button>
      <input
        ref="fileInput"
        type="file"
        accept=".json,application/json"
        class="hidden"
        aria-hidden="true"
        @change="handleFile"
      >
    </div>
  </section>
</template>
