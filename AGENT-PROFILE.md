---
profile_version: 1
project_type: development
skill_profile: medium
task_profile_override: auto
frameworks:
  backend: "Python 3.11 + FastAPI + SQLAlchemy（SQLite lite 档 / PG standard 档）"
  frontend: "Vue 3 + Vite 双 SPA（portal/admin）+ Tailwind v4 + reka-ui"
  mobile: ""
  data: "Chroma（lite）/ Qdrant（standard）向量库 + bge-m3 本地 embedding"
risk_domains:
  - auth-rbac        # 账号/角色/超管层级/MCP scope——最低按大型处理
  - customer-data    # 方案卡含客户商务信息，隔离与脱敏敏感
skill_budget:
  primary: 1
  supporting: 2
updated: 2026-08-06
---

# Agent Profile

## 档位说明

- 默认档位：中型（前后端多模块、持续迭代、外部依赖：LLM/OCR/VLM 云 API、飞书、SMTP）
- 选择原因：单产品但双 SPA + 后端 + MCP server + 连接器多模块；有生产演示实例在跑，失败成本中等。
- 临时升降档规则：遵循 [[skills/methods/选择-开发项目Skill档位]]；涉及 auth/RBAC/MCP scope/客户数据隔离的任务最低按大型处理（补 code-reviewer 级验证）。

## 项目专项路由

- 后端：Python（pytest 全量在 worktree 根 `"D:/Claude Code/RAG/.venv/Scripts/python" -m pytest`，金丝雀法确认测的是 worktree 代码）
- 前端：Vue3（`web-app` 内 `npx vitest run` + `npm run build`；**web/ 构建产物随 git 提交**，改前端必须重建再 commit）
- 数据与基础设施：向量库插件双实现（Chroma/Qdrant）改动必须两档都过测试；演示机 1.95.86.187 部署走"预备份→git archive→只重建 kbase-app-1"流程，严禁动他人容器
- Skill 路由说明：AGENTS.md §6.2 指定的 Jeffallan/claude-skills 技能集在当前 harness 不可用时，降级为
  karpathy-guidelines（编码纪律）+ 仓库既有约定（测试先行、中文注释含职责与取舍、错误走 AppError/i18n key）。

## 项目专项约束

- 提交署名 EricZehuaChang noreply，无 Co-Authored-By 尾注；bcrypt 锁 <4.1。
- i18n：新增用户可见文案必须三语（zh/en/ms）齐 key；含 `@`/`|` 等 vue-i18n 特殊字符须转义（渲染冒烟测试拦）。
- 推送分支 feature/m5-1，并同步 push 到 main（`git push origin feature/m5-1:main`）。
- 开工前读 `D:\brain\projects\project06-kbase\context.md` 开工须知；收工回写 context.md 与 COMPOUND-LOG。
