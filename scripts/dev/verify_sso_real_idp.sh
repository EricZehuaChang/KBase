#!/usr/bin/env bash
# KBase 企业 SSO（M6-8）**真 IdP 端到端验收** —— 在 kbase-test 上跑
#
# 解决什么问题：M6-8 的 SSO 从落地起所有测试都把 kbase/auth/oidc.py 的
# discover / exchange_code 打桩掉了，从没跟真 IdP 说过话。本脚本起一个真
# Keycloak，把真 KBase app（create_app(auth="on")）跑在真 HTTP 端口上，走完
# 整条授权码流：discovery → 登录页 → code → token → userinfo → 会话 Cookie
# → 已认证业务接口 200。全程不打桩。
#
# 与 provision_keycloak_sso.sh 的分工：
#   provision_keycloak_sso.sh  只负责"把 Keycloak 起起来、配好 realm/client/user"
#   verify_sso_real_idp.sh     负责"拿真 IdP 验证 KBase 的 SSO 实现"（本脚本）
# 本脚本会先调 provision 脚本（幂等），所以单独跑本脚本即可。
#
# 用法（服务器上，root）：
#   bash /opt/kbase-test/repo/scripts/dev/verify_sso_real_idp.sh
#   bash /opt/kbase-test/repo/scripts/dev/verify_sso_real_idp.sh --dump-fixture
#
# Mac 侧：先 scripts/dev/remote_test.sh sync 把工作区同步过去，再
#   ssh kbase-test 'bash /opt/kbase-test/repo/scripts/dev/verify_sso_real_idp.sh'
#
# 铁律：不碰服务器上任何既有服务（Gitea / java / nginx / penpot / PG5433 全不涉及）。
#   只用 127.0.0.1:8090（Keycloak）与 127.0.0.1:8092（KBase app），数据落在
#   /opt/kbase-test/keycloak-run/（独立 sqlite，不碰 kbase_test 库）。
set -euo pipefail

KC_SCRIPT_REL="scripts/dev/provision_keycloak_sso.sh"
REPO="${KBASE_TEST_REPO:-/opt/kbase-test/repo}"
VENV="${KBASE_TEST_VENV:-/opt/kbase-test/venv}"
RUN_DIR=/opt/kbase-test/keycloak-run
KC_DIR=/opt/kbase-test/keycloak
APP_PORT=8092                     # Keycloak 用 8090；两者都必须先确认空闲
KC_PORT=8090
ISSUER="http://127.0.0.1:${KC_PORT}/realms/kbase"
CLIENT_ID=kbase
APP_URL="http://127.0.0.1:${APP_PORT}"
LOG="${RUN_DIR}/uvicorn.log"
# IdP 侧与 KBase 本地 bootstrap 超管同名的用户：用来验证"同名账号提权"被拒
COLLISION_USER=admin

DUMP_FIXTURE=0
[ "${1:-}" = "--dump-fixture" ] && DUMP_FIXTURE=1

log() { echo "[$(date +%H:%M:%S)] $*"; }

# ---------------------------------------------------------------- 0) 前置检查
log "== 0) 前置检查 =="
[ "$(id -u)" = "0" ] || { echo "必须在服务器上以 root 跑（要 podman exec）" >&2; exit 2; }
command -v podman >/dev/null || { echo "服务器上没有 podman" >&2; exit 2; }
[ -x "${VENV}/bin/python" ] || { echo "缺 venv: ${VENV}" >&2; exit 2; }
[ -d "$REPO" ] || { echo "缺仓库副本: ${REPO}" >&2; exit 2; }
mkdir -p "$RUN_DIR"

# 端口占用检查：只清"我们自己上次留下的"探针 app（按 pidfile 精确杀），
# 端口被别人占就换端口，绝不碰别人的进程。
if ss -tln | grep -q ":${KC_PORT} "; then
  podman ps --format '{{.Names}}' | grep -qx kbase-keycloak \
    || { echo "端口 ${KC_PORT} 被非本项目进程占用，换端口再跑" >&2; exit 2; }
fi
PIDFILE="${RUN_DIR}/app.pid"
if [ -s "$PIDFILE" ]; then
  OLD_PID="$(cat "$PIDFILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    log "  清掉上次遗留的探针 app（pid=${OLD_PID}）"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi
ss -tln | grep -q ":${APP_PORT} " \
  && { echo "端口 ${APP_PORT} 被占用（非本项目残留），换 APP_PORT 再跑" >&2; exit 2; }

# ------------------------------------------------- 1) 确保 Keycloak 起好且配好
log "== 1) 确保 Keycloak 就绪（调 provision 脚本，幂等）=="
bash "${REPO}/${KC_SCRIPT_REL}" | sed 's/^/    /'

# ------------------------------------------- 2) 取密钥（从 Keycloak 现取，不写死）
log "== 2) 取 client secret 与测试用户口令 =="
KCADM=/opt/keycloak/bin/kcadm.sh
CLIENT_SECRET="$(podman exec kbase-keycloak $KCADM get clients -r kbase \
    -q clientId="${CLIENT_ID}" --fields secret --format csv --noquotes \
    | tail -1 | tr -d '\r')"
[ -n "$CLIENT_SECRET" ] || { echo "取不到 client secret" >&2; exit 3; }
USER_PW="$(cat "${KC_DIR}/.user-pw.txt")"
ADMIN_PW="$(cat "${KC_DIR}/.admin-pw.txt")"
TEST_USER="${KBASE_SSO_TEST_USER:-zhang.san}"
log "  client_id=${CLIENT_ID} 测试用户=${TEST_USER}（secret 不回显）"

# --------------------------------------------------------------- 3) 生成配置
log "== 3) 生成 KBase 配置（独立 sqlite，不碰 kbase_test 库）=="
CFG="${RUN_DIR}/kbase-sso.yaml"
# 每次验收从空库开始：上一次跑留下的证据（如已建好的 zhang.san）会让"首次
# 见到这个 SSO 身份"的分支不再触发，验收就不再验的是首次路径。sqlite 在
# RUN_DIR 下，删掉即可——不碰 kbase_test 库、不碰 PG。
rm -rf "${RUN_DIR}/data"
mkdir -p "${RUN_DIR}/data"
cat > "$CFG" <<YAML
# 由 scripts/dev/verify_sso_real_idp.sh 生成，仅供真机 SSO 验收使用
data_dir: ${RUN_DIR}/data
sso:
  enabled: true
  issuer: ${ISSUER}
  client_id: ${CLIENT_ID}
  client_secret_env: KBASE_OIDC_CLIENT_SECRET
  default_role: viewer
  # 默认 false：C2 就是验"IdP 里叫 admin 的人拿不到本地超管"
  allow_existing_users: false
llm:
  active: fake
  providers:
    - {name: fake, base_url: 'http://127.0.0.1:1', api_key_env: FAKE_KEY, model: m}
YAML
log "  已写 ${CFG}"

# ------------------------------------------------------------- 4) 起 KBase app
log "== 4) 起真 KBase app（create_app(auth=\"on\")）于 ${APP_URL} =="
export KBASE_SSO_VERIFY_CONFIG="$CFG"
export KBASE_OIDC_CLIENT_SECRET="$CLIENT_SECRET"
export KBASE_ADMIN_PASSWORD="$ADMIN_PW"
export KBASE_SSO_APP_PORT="$APP_PORT"
export KBASE_TEST_REPO="$REPO"
# 注：2026-09-15 曾需要挂 scripts/dev/qa_outcomes_shim 绕开"main 缺
# kbase/qa_outcomes.py"的断链（T12 半成品被 stash 收走）。该断链已修复
# （见提交 "fix: 修复主干两处断裂"），占位目录已删除，这里不再需要任何补丁。
# 启动脚本落成文件（而不是 heredoc 到后台进程）：报错能看见、pid 可精确回收
cat > "${RUN_DIR}/run_app.sh" <<'SH'
#!/usr/bin/env bash
# 由 verify_sso_real_idp.sh 生成：起探针用的 KBase app（真 create_app，假向量器）
set -euo pipefail
cd "$KBASE_TEST_REPO"
sys_path_dir="${KBASE_TEST_REPO}/scripts/dev"
exec "${KBASE_TEST_VENV:-/opt/kbase-test/venv}/bin/python" -c "
import sys, os, uvicorn
sys.path.insert(0, '${sys_path_dir}')
import sso_real_idp_probe
uvicorn.run(sso_real_idp_probe.create_probe_app(),
            host='127.0.0.1', port=int(os.environ['KBASE_SSO_APP_PORT']),
            log_level='info')
"
SH
chmod +x "${RUN_DIR}/run_app.sh"
: > "$LOG"
# setsid：脱离本会话的进程组，SSH 断开也不会把 app 带走（虽然本脚本会自己收尾）
setsid bash -c "echo \$\$ > '${PIDFILE}'; exec '${RUN_DIR}/run_app.sh'" >> "$LOG" 2>&1 &
sleep 1
for _ in $(seq 1 10); do [ -s "$PIDFILE" ] && break; sleep 1; done
APP_PID="$(cat "$PIDFILE" 2>/dev/null || echo "")"
log "  app pid=${APP_PID}，日志 ${LOG}"

cleanup() {
  log "== 收尾：停掉探针 app（Keycloak 容器保留，见 provision 脚本）=="
  kill "$APP_PID" 2>/dev/null || true
  sleep 1
  kill -9 "$APP_PID" 2>/dev/null || true
  rm -f "$PIDFILE"
}
trap cleanup EXIT

for i in $(seq 1 60); do
  curl -fsS --max-time 3 "${APP_URL}/api/auth/sso/status" >/dev/null 2>&1 && break
  kill -0 "$APP_PID" 2>/dev/null || { echo "app 启动失败，日志：" >&2; tail -40 "$LOG" >&2; exit 4; }
  sleep 1
done
curl -fsS --max-time 3 "${APP_URL}/api/auth/sso/status" >/dev/null 2>&1 \
  || { echo "app 60s 内未就绪，日志：" >&2; tail -40 "$LOG" >&2; exit 4; }
log "  app 就绪：$(curl -fsS "${APP_URL}/api/auth/sso/status")"

# ------------------------------------------------------------------ 5) 跑探针
log "== 5) 跑真机授权码流探针（全程无打桩）=="
# 探针输出同时落盘：这份 transcript（含每一步的 HTTP 状态、authorize 参数、
# 会话 Cookie、解码后的 JWT claims）就是"真用户登进来了"的交付证据。
TRANSCRIPT="${RUN_DIR}/verify-$(date +%Y%m%d-%H%M%S).log"
log "  证据留存：${TRANSCRIPT}"
set +e
FIXTURE_ARG=""
[ "$DUMP_FIXTURE" = "1" ] && FIXTURE_ARG="--dump-fixture ${REPO}/tests/fixtures/keycloak_real_idp.json"
"${VENV}/bin/python" "${REPO}/scripts/dev/sso_real_idp_probe.py" \
  --app-url "$APP_URL" \
  --issuer "$ISSUER" \
  --client-id "$CLIENT_ID" \
  --client-secret "$CLIENT_SECRET" \
  --username "$TEST_USER" \
  --password "$USER_PW" \
  --collision-username "$COLLISION_USER" \
  $FIXTURE_ARG 2>&1 | tee "$TRANSCRIPT"
RC=${PIPESTATUS[0]}
set -e

log "== 探针退出码 ${RC}；app 日志尾部（关注 SSO 相关报错）=="
grep -iE "sso|oidc|error|traceback" "$LOG" | tail -20 || true

if [ "$RC" -ne 0 ]; then
  echo "❌ 真机 SSO 验收未全绿，见上面逐条 [FAIL]" >&2
  exit "$RC"
fi
echo "✅ 真机 SSO 验收全绿：真 Keycloak 授权码流 + 会话 Cookie + 已认证接口 200"
echo "   Keycloak 仍在跑：podman ps | grep kbase-keycloak（重启用 provision 脚本）"
echo "   夹具（若带 --dump-fixture）：${REPO}/tests/fixtures/keycloak_real_idp.json"
