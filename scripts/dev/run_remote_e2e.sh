#!/usr/bin/env bash
# 把页面级 E2E 冒烟（Playwright + Chromium，web-app/e2e/）丢到远端服务器跑。
#
# 为什么不在本机跑：本机只有 8G 内存且 swap 打满，起一个 Chromium 就会把机器
# 压死、还会拖垮同一时间在跑的 pytest。服务器 182.61.137.91（32G / 8 核）才跑得动。
# 本机能做的离线校验是 `npx playwright test --list`（发现 + 解析用例，不起浏览器）。
#
# 用法（本机执行）：
#   scripts/dev/run_remote_e2e.sh              # 同步 → 起两个服务 → 连跑两次 → 收日志 → 关进程
#   scripts/dev/run_remote_e2e.sh once         # 只跑一次
#   scripts/dev/run_remote_e2e.sh setup        # 只装依赖与 Chromium（npm ci + playwright install）
#   scripts/dev/run_remote_e2e.sh sync         # 只同步工作区
#   scripts/dev/run_remote_e2e.sh logs         # 看最近一次 E2E 日志尾部
#   USE_DEV_APP=1 scripts/dev/run_remote_e2e.sh   # 后端换 scripts.dev_app（真实云 LLM，用同步过去的 .env 密钥）
#
# 默认后端是 web-app/e2e/dev_server.py（dev 配置 + 假向量 + auth=off + 确定性桩
# LLM）——与 CI 的 e2e job 完全同口径：不依赖任何外部 API 与密钥。要看真实模型的
# 表现再加 USE_DEV_APP=1（那时引用角标取决于模型是否在正文里标 [1]）。
#
# 服务器约定（同 provision_remote_test_env.sh / remote_test.sh）：
#   - /opt/kbase-test/{repo,venv,logs}；仓库副本由 remote_test.sh sync 维护（排除 node_modules/data/.env 之外的敏感物按原样）。
#   - 只监听 127.0.0.1 的 8100（后端）与 5173（Vite，代理 /api→8100），不对外暴露。
#   - **不碰服务器上任何在跑的服务**（Gitea 容器 / java / nginx 全不涉及）；
#     自己起的两个进程按 PID 关闭（trap 兜住异常退出），不留常驻进程。
set -euo pipefail

HOST=kbase-test
REPO_DIR=/opt/kbase-test/repo
VENV=/opt/kbase-test/venv
LOGDIR=/opt/kbase-test/logs
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15)
API_PORT=8100
WEB_PORT=5173
STAMP="$(date +%Y%m%d-%H%M%S)"

# 默认桩后端（CI 口径）；USE_DEV_APP=1 换成真实 LLM 的 dev_app
FACTORY="dev_server:create_dev_app"
FACTORY_CWD="${REPO_DIR}/web-app/e2e"
if [ "${USE_DEV_APP:-}" = "1" ]; then
  FACTORY="scripts.dev_app:create_dev_app"
  FACTORY_CWD="${REPO_DIR}"
fi

rsh() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

sync_repo() {
  echo "== 1) 同步工作区 → ${HOST}:${REPO_DIR} =="
  # 复用既有同步脚本（排除规则只有一处，不重复维护）
  "${SCRIPT_DIR}/remote_test.sh" sync
  # 本机跑过的产物别带到服务器：test-results/created-kbs.json 是上一轮的知识库
  # 清单，同步过去会被服务器上的 teardown 当成自己的记录去删（噪音）。
  rsh "rm -rf ${REPO_DIR}/web-app/test-results ${REPO_DIR}/web-app/playwright-report"
}

setup_env() {
  echo "== 2) 服务器依赖（node/npm + node_modules + Chromium）=="
  rsh 'node --version && npm --version' || {
    echo "!! 服务器上没有 Node：dnf module enable nodejs:22 && dnf install -y nodejs npm" >&2
    exit 1
  }
  if ! rsh "test -x ${REPO_DIR}/web-app/node_modules/.bin/playwright"; then
    echo "-- npm ci（首次约 1 分钟）--"
    rsh "cd ${REPO_DIR}/web-app && npm ci --no-audit --no-fund"
  fi
  # 幂等：已装过就秒过；--with-deps 会补系统库（Rocky 9 上缺 nss/atk 之类会起不来）
  rsh "cd ${REPO_DIR}/web-app && npx playwright install --with-deps chromium" | tail -3
}

start_services() {
  echo "== 3) 在服务器上起后端(8100) + Vite(5173) =="
  rsh "mkdir -p ${LOGDIR}"
  # 先清掉可能残留的同端口进程（只清本脚本自己那两种命令行，不用宽泛的 pkill vite）
  rsh "pkill -f 'uvicorn --factory ${FACTORY%%:*}' 2>/dev/null; pkill -f 'vite --host 127.0.0.1 --port ${WEB_PORT}' 2>/dev/null; true"
  rsh "cd ${FACTORY_CWD} && nohup ${VENV}/bin/python -m uvicorn --factory ${FACTORY} \
        --host 127.0.0.1 --port ${API_PORT} > ${LOGDIR}/${STAMP}-api.log 2>&1 & echo \$! > ${LOGDIR}/e2e-api.pid; sleep 1; cat ${LOGDIR}/e2e-api.pid"
  rsh "cd ${REPO_DIR}/web-app && nohup npx vite --host 127.0.0.1 --port ${WEB_PORT} --strictPort \
        > ${LOGDIR}/${STAMP}-web.log 2>&1 & echo \$! > ${LOGDIR}/e2e-web.pid; sleep 1; cat ${LOGDIR}/e2e-web.pid"

  echo "-- 等服务就绪（后端 jieba 初始化 + Vite 首次依赖预打包，最多 90s）--"
  local i ok_api=0 ok_web=0
  for i in $(seq 1 45); do
    [ "$ok_api" = 0 ] && rsh "curl -fsS -m 3 http://127.0.0.1:${API_PORT}/healthz >/dev/null" 2>/dev/null && ok_api=1
    [ "$ok_web" = 0 ] && rsh "curl -fsS -m 3 -o /dev/null http://127.0.0.1:${WEB_PORT}/" 2>/dev/null && ok_web=1
    [ "$ok_api" = 1 ] && [ "$ok_web" = 1 ] && break
    sleep 2
  done
  if [ "$ok_api" != 1 ] || [ "$ok_web" != 1 ]; then
    echo "!! 服务没起来（api=${ok_api} web=${ok_web}），日志尾部：" >&2
    rsh "tail -30 ${LOGDIR}/${STAMP}-api.log ${LOGDIR}/${STAMP}-web.log" >&2 || true
    return 1
  fi
  echo "后端 8100 / Vite 5173 就绪"
}

stop_services() {
  echo "== 5) 关掉本次起的两个进程 =="
  rsh "for f in ${LOGDIR}/e2e-api.pid ${LOGDIR}/e2e-web.pid; do \
         [ -f \"\$f\" ] && pid=\$(cat \"\$f\") && kill \"\$pid\" 2>/dev/null && echo \"killed \$(basename \$f) pid=\$pid\"; \
         rm -f \"\$f\"; done; \
       pkill -f 'uvicorn --factory ${FACTORY%%:*}' 2>/dev/null; \
       pkill -f 'vite --host 127.0.0.1 --port ${WEB_PORT}' 2>/dev/null; true" || true
  # 确认没留常驻
  rsh "ss -ltnp 2>/dev/null | grep -E ':(${API_PORT}|${WEB_PORT})\b' || echo '端口已释放'" || true
}

run_suite() {
  local times="${1:-2}" i rc=0
  echo "== 4) 跑 Playwright（${times} 次，reporter=list）=="
  for i in $(seq 1 "$times"); do
    echo "---- 第 ${i} 次 ----"
    set +e
    rsh "cd ${REPO_DIR}/web-app && npx playwright test --reporter=list 2>&1 | tee ${LOGDIR}/${STAMP}-e2e-run${i}.log"
    local code=$?
    set -e
    rsh "grep -E '^[[:space:]]*[0-9]+ (passed|failed|flaky|skipped)' ${LOGDIR}/${STAMP}-e2e-run${i}.log | tail -2" || true
    echo "---- 第 ${i} 次 exit=${code} ----"
    [ "$code" -ne 0 ] && rc=1
  done
  return "$rc"
}

trap 'stop_services' EXIT

case "${1:-run}" in
  sync)  sync_repo ;;
  setup) setup_env ;;
  logs)
    rsh "ls -t ${LOGDIR}/*-e2e-run*.log 2>/dev/null | head -3; echo ---; \
         for f in \$(ls -t ${LOGDIR}/*-e2e-run*.log 2>/dev/null | head -2); do echo \"### \$f\"; tail -20 \"\$f\"; done"
    ;;
  ssh) ssh "${SSH_OPTS[@]}" "$HOST" ;;
  once|run)
    TIMES=2; [ "${1:-}" = "once" ] && TIMES=1
    sync_repo
    setup_env
    start_services
    run_suite "$TIMES"
    ;;
  *)
    echo "用法：$0 {run|once|setup|sync|logs|ssh}" >&2
    exit 2
    ;;
esac
