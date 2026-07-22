// src/lib/api.ts —— typed API 客户端统一出口（barrel）。
//
// 900 行单文件按域拆进 lib/api/（健康度重构，导入路径不变——全站仍
// `import { ... } from "@/lib/api"`）：
//   core.ts     HTTP 基座（req/jsonInit/401拦截）+ 登录会话/角色 + SSO
//   kb.ts       知识库/文档/Chunk/授权/向量模型/URL导入/演示数据
//   chat.ts     检索调试/会话 CRUD/SSE 问答/消息反馈
//   evals.ts    评测回归（B）
//   settings.ts Provider/模型目录/向量密钥/用户/APIKey/许可证/运营看板
//   jobs.ts     大纲与长任务生成
//
// 新端点加到对应域文件；跨域共用的只有 core 的 req/jsonInit。
// 声明式代码不单测（由使用它的组件测试间接覆盖）。
export * from "./api/core";
export * from "./api/kb";
export * from "./api/chat";
export * from "./api/evals";
export * from "./api/settings";
export * from "./api/jobs";
export * from "./api/share";
export * from "./api/i18n";
