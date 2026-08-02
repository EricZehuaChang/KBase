# 厂商官方 Logo（模型服务卡片用）

来源：[lobe-icons](https://github.com/lobehub/lobe-icons)（`@lobehub/icons-static-svg` v1.94.0，MIT License）。
随前端打包内置，私有化部署离线可用；商标归各厂商所有，仅用于标识对应模型服务。

- `*-color.svg`：品牌色彩版（智谱/通义/DeepSeek/Kimi/硅基流动）
- `openai.svg`：单色版（OpenAI 官方即单色，`fill="currentColor"` 跟随主题文字色）

新增厂商：从 lobe-icons 拷对应 SVG 进本目录，并在 `lib/settings-utils.ts` 的
VENDORS 表加一行（域名正则 + icon 文件名），`components/VendorLogo.vue` 注册 import。
