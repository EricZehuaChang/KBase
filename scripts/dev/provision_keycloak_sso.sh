#!/usr/bin/env bash
# 在 kbase-test 上起一个**真 Keycloak**（OIDC IdP），给 KBase 企业 SSO（M6-8）
# 做真机联调用。跑完用 scripts/dev/verify_sso_real_idp.sh 走完整授权码流。
#
# 为什么需要它：M6-8 的 SSO 测试全程把 kbase/auth/oidc.py 的 discover /
# exchange_code 打桩掉了，从没跟真 IdP 说过话——claim 形状、issuer 取值、
# redirect_uri 精确匹配、client_secret 传递方式这些真机行为一个都没验过。
#
# 铁律（与 scripts/dev/provision_remote_test_env.sh 同规矩）：
#   **不碰服务器上任何正在跑的服务**。本脚本只新增自己的资源——
#     - 容器 kbase-keycloak（独占命名，不 --rm 别人的容器）；
#     - 宿主端口只绑 127.0.0.1:8090（**绝不 0.0.0.0**，公网不可达）；
#     - 自己的目录 /opt/kbase-test/keycloak/；
#     - 不写 firewalld、不动 nginx、不动已有的 PG5433 与 Gitea。
#
# 镜像来源：服务器到 Docker Hub 不通、quay.io 的 CDN（cdn01.quay.io）也常
# TLS 超时（本会话实测 pull 重试 3 次后失败）。daocloud 镜像站可达，服务器上
# 别的镜像也是从它拉的，所以默认走它；要换源改 IMAGE 即可。
#
# 幂等：重复执行只补齐缺失的部分（容器在跑就复用、realm/client/user 存在就跳过、
# 口令文件存在就不重生成）。**可以在任意时刻重跑**——这是"事后怎么再起来"的答案。
#
# 用法（服务器上，root）：
#   bash /opt/kbase-test/repo/scripts/dev/provision_keycloak_sso.sh
#   bash /opt/kbase-test/repo/scripts/dev/provision_keycloak_sso.sh status   # 只看状态
#   bash /opt/kbase-test/repo/scripts/dev/provision_keycloak_sso.sh down     # 停容器（保留数据）
#
# 想彻底删掉（含数据卷）：podman rm -f kbase-keycloak && rm -rf /opt/kbase-test/keycloak
set -euo pipefail

# ---------------------------------------------------------------- 可调参数
IMAGE="${KC_IMAGE:-docker.m.daocloud.io/keycloak/keycloak:26.0}"
CONTAINER=kbase-keycloak
KC_DIR=/opt/kbase-test/keycloak
KC_DATA="${KC_DIR}/data"
HOST_PORT="${KC_HOST_PORT:-8090}"        # 只绑 127.0.0.1
REALM="${KC_REALM:-kbase}"
CLIENT_ID="${KC_CLIENT_ID:-kbase}"
APP_PORT="${KC_APP_PORT:-8092}"          # KBase app 的端口，决定 redirect_uri
# ⚠️ 必须与 KBase 实际访问地址**逐字节**一致：KBase 用 request.base_url 推
#    redirect_uri，Keycloak 默认对 redirect_uri 做精确匹配（不做通配），
#    端口/主机名/protocol 任一处不同就是 invalid_redirect_uri。
REDIRECT_URI="http://127.0.0.1:${APP_PORT}/api/auth/sso/callback"
WEB_ORIGIN="http://127.0.0.1:${APP_PORT}"
TEST_USERS=("zhang.san:zhang.san@corp.example:San:Zhang"
            "li.si:lisi@corp.example:Si:Li"
            "no.email::No:Email"          # 第三个故意不给邮箱：验 claim 回退路径
            # 第四个故意与 KBase 本地 bootstrap 超管同名：验"同名账号提权"被拒
            # （改造前这个用户登录一次就拿到 KBase 的 superadmin，真机实测）
            "admin:admin@corp.example:Boss:Admin")

KCADM=/opt/keycloak/bin/kcadm.sh
ADMIN_USER=admin
ADMIN_PW_FILE="${KC_DIR}/.admin-pw.txt"
USER_PW_FILE="${KC_DIR}/.user-pw.txt"
SECRET_FILE="${KC_DIR}/.client-secret.txt"

log() { echo "[$(date +%H:%M:%S)] $*"; }
kc() { podman exec "$CONTAINER" "$KCADM" "$@"; }

# ------------------------------------------------------------------ 只读状态
if [ "${1:-}" = "status" ]; then
  podman ps -a --filter "name=${CONTAINER}" \
    --format '容器 {{.Names}} / {{.Status}} / {{.Ports}}' || true
  ss -tlnp | grep ":${HOST_PORT} " || echo "端口 ${HOST_PORT} 无监听"
  curl -fsS --max-time 5 "http://127.0.0.1:${HOST_PORT}/realms/${REALM}" \
    | head -c 120 && echo || echo "realm ${REALM} 不可达"
  exit 0
fi

if [ "${1:-}" = "down" ]; then
  log "停容器 ${CONTAINER}（数据保留在 ${KC_DATA}；重跑本脚本即恢复）"
  podman stop "$CONTAINER" >/dev/null 2>&1 || true
  exit 0
fi

# ---------------------------------------------------------------- 0) 前置检查
log "== 0) 前置检查 =="
[ "$(id -u)" = "0" ] || { echo "必须在服务器上以 root 跑（需要 podman）" >&2; exit 2; }
command -v podman >/dev/null || { echo "服务器上没有 podman" >&2; exit 2; }
command -v curl >/dev/null || { echo "缺 curl" >&2; exit 2; }

# 端口冲突：只在"不是我们自己的容器"时报错退出，别人的服务一律不碰
if ss -tln | grep -q ":${HOST_PORT} "; then
  if ! podman ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "端口 ${HOST_PORT} 被非本项目进程占用：$(ss -tlnp | grep ":${HOST_PORT} ")" >&2
    echo "换端口：KC_HOST_PORT=8091 KC_APP_PORT=8093 bash $0" >&2
    exit 2
  fi
  log "  端口 ${HOST_PORT} 已由 ${CONTAINER} 自己占用，继续复用"
fi

mkdir -p "$KC_DATA"
chmod 700 "$KC_DIR"
# 容器内默认 uid 1000 运行 Keycloak（start-dev 用非 root），绑挂目录要可写。
# 用 :Z 让 podman 打 SELinux 标签（Rocky 9 开着 SELinux，不打标签会 Permission denied）。
chmod 777 "$KC_DATA"

# -------------------------------------------------------------- 1) 拉镜像
log "== 1) 拉镜像 ${IMAGE}（若已存在则跳过）=="
if podman image exists "$IMAGE"; then
  log "  镜像已在本地"
else
  # 服务器到 Docker Hub 不通；quay.io 的 CDN 常 TLS 超时。失败时明确告知，
  # 不要让它默默挂很久。
  podman pull "$IMAGE" || {
    echo "拉镜像失败。服务器可达的镜像源实测：" >&2
    echo "  docker.m.daocloud.io   ← 本脚本默认（可用）" >&2
    echo "  quay.io                ← 可达但 CDN 常 TLS 超时" >&2
    echo "  registry-1.docker.io   ← 不可达" >&2
    exit 3
  }
fi

# -------------------------------------------------------------- 2) 起容器
log "== 2) 起容器 ${CONTAINER}（只绑 127.0.0.1:${HOST_PORT}）=="
umask 077
[ -s "$ADMIN_PW_FILE" ] || openssl rand -hex 16 > "$ADMIN_PW_FILE"
[ -s "$USER_PW_FILE" ]  || openssl rand -hex 12 > "$USER_PW_FILE"
ADMIN_PW="$(cat "$ADMIN_PW_FILE")"

if podman container exists "$CONTAINER" 2>/dev/null; then
  log "  容器已存在，按需启动"
  podman start "$CONTAINER" >/dev/null 2>&1 || true
else
  # start-dev：开发模式（HTTP 明文 + 内置 H2 文件库），够真机联调用。
  # --hostname-strict=false 让 issuer 跟随实际访问的 host（127.0.0.1:8090），
  # 否则 issuer 会变成容器内主机名，KBase 侧的 discovery 与跳转对不上。
  # 生产不该用 start-dev，见 docs/manual/运维手册.md。
  podman run -d --name "$CONTAINER" \
    --restart=no \
    -p "127.0.0.1:${HOST_PORT}:8080" \
    -e KC_BOOTSTRAP_ADMIN_USERNAME="$ADMIN_USER" \
    -e KC_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PW" \
    -e KC_HTTP_ENABLED=true \
    -v "${KC_DATA}:/opt/keycloak/data:Z" \
    "$IMAGE" \
    start-dev --http-port=8080 --hostname-strict=false
fi
# 注意：不在 podman run 上写 --restart=always。服务器重启后它是"自己起来"，
# 但对本机来说是"多了一个常驻服务"，与本项目的临时性不符——要恢复就重跑本脚本。

log "== 3) 等就绪（轮询 /realms/master）=="
for i in $(seq 1 60); do
  curl -fsS --max-time 3 "http://127.0.0.1:${HOST_PORT}/realms/master" >/dev/null 2>&1 \
    && { log "  Keycloak 就绪（${i}s）"; break; }
  sleep 2
done
curl -fsS --max-time 3 "http://127.0.0.1:${HOST_PORT}/realms/master" >/dev/null 2>&1 \
  || { echo "Keycloak 120s 内未就绪，看日志：podman logs ${CONTAINER}" >&2; exit 4; }

# 绑定核验：既看 podman 的端口映射，也看宿主真实监听地址
log "  端口映射：$(podman port "$CONTAINER" | tr '\n' ' ')"
if ss -tln | grep ":${HOST_PORT} " | grep -qv "127.0.0.1:${HOST_PORT}"; then
  echo "⚠️ 端口 ${HOST_PORT} 不只绑在 127.0.0.1，立即排查（绝不能公网暴露）" >&2
  exit 5
fi
log "  ✅ 只监听 127.0.0.1:${HOST_PORT}（公网不可达）"

# ------------------------------------------------------ 4) realm / client / user
log "== 4) 配 realm=${REALM} / client=${CLIENT_ID} / 测试用户 =="
kc config credentials --server http://127.0.0.1:8080 --realm master \
   --user "$ADMIN_USER" --password "$ADMIN_PW" >/dev/null

if kc get "realms/${REALM}" >/dev/null 2>&1; then
  log "  realm ${REALM} 已存在"
else
  kc create realms -s "realm=${REALM}" -s enabled=true >/dev/null
  log "  已建 realm ${REALM}"
fi

CLIENT_UUID="$(kc get clients -r "$REALM" -q "clientId=${CLIENT_ID}" \
                 --fields id --format csv --noquotes | tail -1 | tr -d '\r')"
# 强制 PKCE（S256）。Keycloak 默认是"不强制"，但 Azure AD / Okta / Authing 等
# 多数企业 IdP 的生产配置都会要求 code_challenge，KBase 改造前遇到这种 IdP 会
# 直接失败（authorize 被回 error=invalid_request）。测试环境按**严格档**配，
# 这样每次跑验收都在持续证明 KBase 真的带了 PKCE。
PKCE_ATTR='attributes."pkce.code.challenge.method"=S256'
if [ -n "$CLIENT_UUID" ]; then
  # redirectUris 幂等收敛：每次都 update，避免上次用别的端口建好后再也改不回来
  kc update "clients/${CLIENT_UUID}" -r "$REALM" \
     -s "redirectUris=[\"${REDIRECT_URI}\"]" \
     -s "webOrigins=[\"${WEB_ORIGIN}\"]" \
     -s "$PKCE_ATTR" >/dev/null
  log "  client ${CLIENT_ID} 已存在，redirectUris 收敛为 ${REDIRECT_URI}（强制 PKCE）"
else
  # confidential client（publicClient=false）：KBase 用 client_secret 换 token。
  # standardFlowEnabled=true 才是授权码流；directAccessGrantsEnabled=false
  # 表示不许用密码模式绕过浏览器（真机验收要的就是浏览器那条路）。
  kc create clients -r "$REALM" \
     -s "clientId=${CLIENT_ID}" -s name=KBase -s enabled=true \
     -s protocol=openid-connect -s publicClient=false \
     -s standardFlowEnabled=true -s implicitFlowEnabled=false \
     -s directAccessGrantsEnabled=false -s serviceAccountsEnabled=false \
     -s "redirectUris=[\"${REDIRECT_URI}\"]" \
     -s "webOrigins=[\"${WEB_ORIGIN}\"]" \
     -s "$PKCE_ATTR" >/dev/null
  CLIENT_UUID="$(kc get clients -r "$REALM" -q "clientId=${CLIENT_ID}" \
                   --fields id --format csv --noquotes | tail -1 | tr -d '\r')"
  log "  已建 client ${CLIENT_ID}"
fi

USER_PW="$(cat "$USER_PW_FILE")"
for spec in "${TEST_USERS[@]}"; do
  IFS=':' read -r uname uemail ufirst ulast <<< "$spec"
  if kc get users -r "$REALM" -q "username=${uname}" --fields id \
       --format csv --noquotes 2>/dev/null | grep -q .; then
    log "  用户 ${uname} 已存在"
  else
    args=(-s "username=${uname}" -s enabled=true -s "firstName=${ufirst}"
          -s "lastName=${ulast}")
    [ -n "$uemail" ] && args+=(-s "email=${uemail}" -s emailVerified=true)
    kc create users -r "$REALM" "${args[@]}" >/dev/null
    log "  已建用户 ${uname}${uemail:+ <${uemail}>}"
  fi
  # 每次都重设口令：幂等（重跑后口令仍是文件里那个，不会漂）
  kc set-password -r "$REALM" --username "$uname" --new-password "$USER_PW" >/dev/null
done

# 组 + 域角色：用来验证"KBase 是否读取 IdP 侧角色"。当前实现**不读**（只按
# default_role 建号），这里造出来是为了让这个缺口可被复现、可被写进文档。
kc get groups -r "$REALM" -q name=kbase-editors --fields id --format csv --noquotes \
  2>/dev/null | grep -q . \
  || { kc create groups -r "$REALM" -s name=kbase-editors >/dev/null; log "  已建组 kbase-editors"; }
kc get roles -r "$REALM" -q name=kbase_editor --fields name --format csv --noquotes \
  2>/dev/null | grep -q . \
  || { kc create roles -r "$REALM" -s name=kbase_editor >/dev/null; log "  已建域角色 kbase_editor"; }
GID="$(kc get groups -r "$REALM" -q name=kbase-editors --fields id \
        --format csv --noquotes | tail -1 | tr -d '\r')"
UID_LISI="$(kc get users -r "$REALM" -q username=li.si --fields id \
             --format csv --noquotes | tail -1 | tr -d '\r')"
kc update "users/${UID_LISI}/groups/${GID}" -r "$REALM" -n >/dev/null 2>&1 || true
kc add-roles -r "$REALM" --uusername li.si --rolename kbase_editor >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 5) 取密钥
SECRET="$(kc get clients -r "$REALM" -q "clientId=${CLIENT_ID}" \
            --fields secret --format csv --noquotes | tail -1 | tr -d '\r')"
printf '%s' "$SECRET" > "$SECRET_FILE"
chmod 600 "$SECRET_FILE" "$ADMIN_PW_FILE" "$USER_PW_FILE"

# ------------------------------------------------------------------ 汇总
cat <<EOF

== 完成 ==
Realm            : ${REALM}
Issuer           : http://127.0.0.1:${HOST_PORT}/realms/${REALM}
Discovery        : http://127.0.0.1:${HOST_PORT}/realms/${REALM}/.well-known/openid-configuration
Client ID        : ${CLIENT_ID}
Client secret    : 见 ${SECRET_FILE}（不回显；KBase 侧经环境变量 KBASE_OIDC_CLIENT_SECRET 注入）
Redirect URI     : ${REDIRECT_URI}   ← 必须与 KBase 实际访问地址逐字节一致
测试用户/口令    : zhang.san / li.si / no.email，口令见 ${USER_PW_FILE}
管理台           : http://127.0.0.1:${HOST_PORT}/admin（admin / 见 ${ADMIN_PW_FILE}）
                   —— 只在服务器本机可达；Mac 上看要开隧道：
                   ssh -N -L 8090:127.0.0.1:8090 kbase-test

下一步（真机验收整条授权码流）：
  bash /opt/kbase-test/repo/scripts/dev/verify_sso_real_idp.sh

重建/恢复：直接重跑本脚本（幂等）。
停掉     ：bash $0 down            （数据保留在 ${KC_DATA}）
彻底删除 ：podman rm -f ${CONTAINER} && rm -rf ${KC_DIR}
EOF
