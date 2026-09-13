#!/usr/bin/env bash
# 把 KBase 测试跑到远端服务器（本机 8G 内存跑不动，见 AGENTS.md 验证约定）
#
# 服务器：182.61.137.91（Rocky 9.6，32G 内存 / 8 核），SSH 别名 kbase-test
#   - PostgreSQL 16 独立实例：127.0.0.1:5433，库/角色 kbase_test
#   - /opt/kbase-test/{repo,venv,logs}，venv 用 uv 管理的 Python 3.11.16
#     （系统 python3.11 自带 sqlite 3.34 < chromadb 要求的 3.35，必须用 managed）
#   - **不碰服务器上任何在跑的服务**（Gitea 容器、java 服务、nginx 全不涉及）
#
# 用法：
#   scripts/dev/remote_test.sh sync              # 只同步当前工作区到服务器
#   scripts/dev/remote_test.sh run all           # 全量 + PG 集成（= CI 两个 job）
#   scripts/dev/remote_test.sh run pg            # 只跑 PG 集成（本机跑不了的那部分）
#   scripts/dev/remote_test.sh run security      # 权限矩阵三件套
#   scripts/dev/remote_test.sh tunnel            # 开 SSH 隧道，本机可连远端 PG
#   scripts/dev/remote_test.sh ssh               # 进服务器
#   scripts/dev/remote_test.sh logs              # 看最近一次日志
set -euo pipefail

HOST=kbase-test
REPO_DIR=/opt/kbase-test/repo
LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15)

sync_repo() {
  echo "== 同步工作区 → ${HOST}:${REPO_DIR} =="
  rsync -a --delete \
    --exclude '.venv' --exclude 'web-app/node_modules' --exclude 'data' \
    --exclude '材料.rar' --exclude '*.pptx' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.git' --exclude '.ruff_cache' \
    -e "ssh ${SSH_OPTS[*]}" "$LOCAL_REPO/" "${HOST}:${REPO_DIR}/"
  scp -q "${SSH_OPTS[@]}" "$LOCAL_REPO/scripts/dev/run_remote_tests.sh" \
      "${HOST}:/opt/kbase-test/run_remote_tests.sh"
  echo "同步完成（产物 web/ 一并同步，前端漂移检查才准）"
}

case "${1:-run}" in
  sync)
    sync_repo
    ;;
  run)
    MODE="${2:-all}"
    sync_repo
    echo "== 在服务器上跑 ${MODE}（前台，输出实时回传）=="
    # 注意：不能给 ssh 加 nohup+&（ssh 会等管道关闭而挂住）；这里就前台跑，
    # 需要后台时用 tmux：ssh kbase-test 'tmux new -d "bash /opt/kbase-test/run_remote_tests.sh all"'
    ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p /opt/kbase-test/logs && bash /opt/kbase-test/run_remote_tests.sh ${MODE}"
    ;;
  tunnel)
    # 远端 PG 只监听 127.0.0.1，本机要用就走隧道（本机 5433 → 服务器 5433）
    if lsof -nP -iTCP:5433 -sTCP:LISTEN >/dev/null 2>&1; then
      echo "本机 5433 已在监听（隧道可能已开）"
    else
      ssh -f -N -o ExitOnForwardFailure=yes "${SSH_OPTS[@]}" -L 5433:127.0.0.1:5433 "$HOST"
      echo "隧道已开：本机 127.0.0.1:5433 → ${HOST} 的 PG"
    fi
    echo "连接串：postgresql+psycopg://kbase_test:<服务器 /opt/kbase-test/.pgpass.txt>@127.0.0.1:5433/kbase_test"
    ;;
  ssh)
    ssh "${SSH_OPTS[@]}" "$HOST"
    ;;
  logs)
    ssh "${SSH_OPTS[@]}" "$HOST" 'ls -t /opt/kbase-test/logs/*.log | head -3; echo ---; tail -20 "$(ls -t /opt/kbase-test/logs/*.log | head -1)"'
    ;;
  *)
    echo "用法：$0 {sync|run [all|backend|pg|security]|tunnel|ssh|logs}" >&2
    exit 2
    ;;
esac
