<script setup lang="ts">
// 厂商官方 logo（lobe-icons MIT 静态 SVG，assets/vendors/ 随包内置离线可用）。
// ?raw 内联 + v-html 而不是 <img>：OpenAI 官方 logo 即单色
// （fill="currentColor"），内联才吃得到 CSS 文字色，暗色主题不隐身；
// color 变体自带品牌色不受影响。v-html 的都是仓库内置资产，无注入面。
// 未知厂商（企业自有 OpenAI 兼容平台）回落"品牌色圆角块 + 缩写"徽章。
import { computed } from "vue";
import { vendorBadge } from "@/lib/settings-utils";
import zhipu from "@/assets/vendors/zhipu-color.svg?raw";
import qwen from "@/assets/vendors/qwen-color.svg?raw";
import deepseek from "@/assets/vendors/deepseek-color.svg?raw";
import openai from "@/assets/vendors/openai.svg?raw";
import kimi from "@/assets/vendors/kimi-color.svg?raw";
import siliconcloud from "@/assets/vendors/siliconcloud-color.svg?raw";

const RAW: Record<string, string> = { zhipu, qwen, deepseek, openai, kimi, siliconcloud };

const props = defineProps<{ baseUrl: string }>();
const badge = computed(() => vendorBadge(props.baseUrl));
const svg = computed(() =>
  badge.value.icon ? RAW[badge.value.icon] ?? null : null);
</script>

<template>
  <!-- SVG 自身 width/height=1em，外层字号即 logo 尺寸 -->
  <span
    v-if="svg"
    class="flex size-7 shrink-0 items-center justify-center text-[22px] text-[var(--text)]"
    :title="badge.label"
    v-html="svg"
  />
  <span
    v-else
    class="flex size-7 shrink-0 items-center justify-center rounded-lg text-[10px] font-bold text-white"
    :style="{ backgroundColor: badge.color }"
    :title="badge.label"
  >
    {{ badge.short }}
  </span>
</template>
