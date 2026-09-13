#!/usr/bin/env bash
# 在服务器 182.61.137.91 上跑 KBase 的全量门禁（= ci-core.yml 的 backend +
# backend-pg 两个 job），把本机的内存压力卸载出去。
#
# 用法（服务器上）：
#   bash /opt/kbase-test/run_remote_tests.sh            # 全量 + PG
#   bash /opt/kbase-test/run_remote_tests.sh backend    # 只跑全量
#   bash /opt/kbase-test/run_remote_tests.sh pg         # 只跑 PG 集成
#   bash /opt/kbase-test/run_remote_tests.sh security   # 只跑权限矩阵三件套
#
# 说明：
# - venv 在 /opt/kbase-test/venv（Python 3.11，与 CI 同版本）；
#   依赖与 CI 完全一致：-e ".[dev,mcp]" "chromadb<1"。
# - PG：本机 127.0.0.1:5433/kbase_test（见 provision_remote_test_env.sh）。
#   密码从 /opt/kbase-test/.pgpass.txt 读，不进命令行、不进日志。
# - 不碰服务器上任何在跑的服务（Gitea / java / nginx 都不涉及）。
set -euo pipefail

REPO=/opt/kbase-test/repo
VENV=/opt/kbase-test/venv
LOGDIR=/opt/kbase-test/logs
MODE="${1:-all}"
mkdir -p "$LOGDIR"

PG_PW="$(cat /opt/kbase-test/.pgpass.txt)"
export KBASE_TEST_PG_URL="postgresql+psycopg://kbase_test:${PG_PW}@127.0.0.1:5433/kbase_test"

cd "$REPO"
PY="$VENV/bin/python"
STAMP="$(date +%Y%m%d-%H%M%S)"
rc=0

run() {
  local name="$1"; shift
  echo "=== ${name}  $(date +%H:%M:%S) ==="
  set +e
  "$@" 2>&1 | tee "${LOGDIR}/${STAMP}-${name}.log" | tail -25
  local code=${PIPESTATUS[0]}
  set -e
  local total
  total="$(grep -oE '[0-9]+ passed[^=]*' "${LOGDIR}/${STAMP}-${name}.log" | tail -1)"
  echo "--- ${name}: exit=${code} ${total}"
  [ "$code" -ne 0 ] && rc=1
  return 0
}

case "$MODE" in
  backend|all)
    run backend "$PY" -m pytest -q --junitxml="${LOGDIR}/${STAMP}-backend.xml"
    ;;
esac

case "$MODE" in
  pg|all)
    # -m pg 覆盖 addopts 里的排除；只有这套测试会真连 PostgreSQL
    run backend-pg "$PY" -m pytest -q -m pg
    ;;
esac

case "$MODE" in
  security)
    run security "$PY" -m pytest -q tests/test_apikey_scope.py tests/test_acl_matrix.py tests/test_kb_acl.py
    ;;
esac

echo "=== 日志：${LOGDIR}/${STAMP}-*.log ==="
exit "$rc"
