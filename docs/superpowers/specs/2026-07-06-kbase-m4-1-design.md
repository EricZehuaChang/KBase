# KBase M4-1 设计文档 — 商业化加固（认证/权限/审计/API Key/许可证）

- 日期：2026-07-06
- 状态：授权自主执行（M4 总授权）
- 前置：M3 全部合并（161 backend / 55 frontend）。当前系统零认证——本篇是私有化交付的硬门槛。

## 1. 范围

原始 spec v3 承诺的商业化加固：本地用户体系 + 角色权限 + 审计日志 + API Key 管理 + 轻量许可证。单租户 B2B 定位，不做 SSO/LDAP（M5 若客户要）。

## 2. 认证

- **users 表**：id/username(unique)/password_hash/role(admin|editor|viewer)/disabled/created_at。密码 bcrypt（passlib[bcrypt]）。
- **会话**：JWT HS256，密钥优先 env `KBASE_SECRET_KEY`，否则首启生成并存 app_settings（重启稳定）。httpOnly Cookie `kbase_session`（SameSite=Lax，7 天）。CSRF：中间件校验非 GET 请求的 Origin/Referer 与 Host 同源（无 Origin 的非浏览器客户端放行——它们走 Bearer）。
- **端点**：POST /api/auth/login、POST /api/auth/logout、GET /api/auth/me（含 role）。
- **首启引导**：users 空表时自动建 admin——密码取 env `KBASE_ADMIN_PASSWORD`，否则随机 16 位打印到日志（README 醒目说明）。
- **豁免**：/healthz、/api/auth/login、静态资源与 SPA 回退。
- **双通道**：Cookie（浏览器）或 `Authorization: Bearer kbase_ak_*`（API Key，集成方/MCP）。

## 3. 角色权限（路由级映射）

| 能力 | admin | editor | viewer |
|---|---|---|---|
| 问答/会话/检索/生成任务查看 | ✓ | ✓ | ✓ |
| 建库/上传/删除文档/删库/发起生成/KB 配置 | ✓ | ✓ | ✗ |
| 设置页（Provider/API Key/用户管理）/审计查询 | ✓ | ✗ | ✗ |

403 带中文 detail。前端按 role 门控隐藏入口（防呆，不替代后端校验）。

## 4. API Key 与 MCP

- **api_keys 表**：id/name/prefix(前8字符明文,列表展示用)/key_hash(sha256)/role/created_at/revoked。完整 key 仅创建时返回一次（`kbase_ak_` + 32 随机字符）。
- 设置页 CRUD（admin）：创建（弹窗一次性展示完整 key）/吊销/列表。
- **MCP**：kbase_mcp 读 env `KBASE_API_KEY`，所有反调请求带 Bearer；未配置时工具返回清晰错误指引。

## 5. 审计

- **audit_logs 表**：id/ts/actor(用户名或 key name)/action/resource/detail(JSON,截断)/ip。
- 中间件记录：全部 mutating 请求（action=HTTP方法+路径模板）、登录成/败、问答（action=query，detail=问题前100字）。
- GET /api/audit?limit&offset（admin），设置页只读列表。

## 6. 许可证（轻量）

license.json（org/expires/signature，Ed25519，公钥内置代码）。无文件=试用模式横幅；签名无效=错误横幅；过期=警告横幅。**不锁功能**（v1 商务友好）。GET /api/license 返回状态，settings 展示。配套 scripts/gen_license.py（私钥本地签发，私钥不入库）。

## 7. 测试口径

`create_app(auth="on"|"off")`：默认 on；既有功能测试传 off（被测物是功能不是鉴权）；新增鉴权测试套件专测 on 路径（登录/401/403 角色矩阵/Key 通道/吊销/审计落行/Origin 防护/bootstrap）。生产路径永远 on。前端：路由守卫、401 拦截跳登录、角色门控纯函数。

## 8. 非目标

SSO/LDAP/OAuth；密码策略与强制改密；细到文档级的 ACL；审计导出；license 功能锁。
