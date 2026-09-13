#!/usr/bin/env bash
# KBase 本机测试卸载到远端服务器（2026-09-14）
#
# 背景：本机 8G 内存 + swap 打满，跑 KBase 全量 pytest 要 2.5 分钟且吃内存；
# 服务器 182.61.137.91 有 32G 内存 / 8 核 / 16G 空闲磁盘，用来跑"重活"。
#
# 铁律：**不碰服务器上任何正在运行的服务**。本脚本只做三件事——
#   1) 装 PostgreSQL 16 并把数据目录放在 /var/lib/pgsql/16/data（独立实例）；
#   2) 建独立的测试库/角色（kbase_test / kbase_test）；
#   3) 在 /opt/kbase-test 下建 venv 与仓库副本，供跑测试用。
# 不动 docker（Gitea 与 java 服务都在容器/systemd 里）、不动 nginx、不动 80/9877。
#
# 幂等：重复执行只做缺失的部分。日志：/var/log/kbase-test-provision.log
set -euo pipefail

PG_VERSION=16
PGDATA=/var/lib/pgsql/${PG_VERSION}/data
TEST_DB=kbase_test
TEST_USER=kbase_test
TEST_PW_FILE=/opt/kbase-test/.pgpass.txt
WORKDIR=/opt/kbase-test
PY_VER=3.11

log() { echo "[$(date +%H:%M:%S)] $*"; }

log "== 1) 安装 PostgreSQL ${PG_VERSION}（若已装则跳过）=="
if ! rpm -q postgresql${PG_VERSION}-server >/dev/null 2>&1; then
  dnf install -y "https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm" >/dev/null
  dnf -qy module disable postgresql >/dev/null 2>&1 || true
  dnf install -y postgresql${PG_VERSION}-server postgresql${PG_VERSION} >/dev/null
  log "已安装 postgresql${PG_VERSION}-server"
else
  log "postgresql${PG_VERSION}-server 已存在"
fi

log "== 2) 初始化数据目录（若未初始化）=="
if [ ! -s "${PGDATA}/PG_VERSION" ]; then
  /usr/pgsql-${PG_VERSION}/bin/postgresql-${PG_VERSION}-setup initdb >/dev/null
  log "initdb 完成：${PGDATA}"
else
  log "数据目录已就绪：$(cat "${PGDATA}/PG_VERSION")"
fi

log "== 3) 只监听 127.0.0.1（不对外暴露；Mac 侧走 SSH 隧道访问）=="
CONF="${PGDATA}/postgresql.conf"
if ! grep -q "^listen_addresses = '127.0.0.1'" "$CONF"; then
  sed -i "s/^#\?listen_addresses.*/listen_addresses = '127.0.0.1'/" "$CONF"
  log "listen_addresses 已设为 127.0.0.1"
fi
if ! grep -q "^port = 5433" "$CONF"; then
  # 用 5433：与服务器上可能存在的其他 PG（容器内）区分开，避免误连
  sed -i "s/^#\?port = .*/port = 5433/" "$CONF"
  log "port 已设为 5433"
fi

log "== 4) 启动/重启独立实例（systemd unit postgresql-${PG_VERSION}）=="
systemctl enable --now postgresql-${PG_VERSION} >/dev/null 2>&1 || true
systemctl restart postgresql-${PG_VERSION}
sleep 2
systemctl is-active postgresql-${PG_VERSION} && log "实例运行中"

log "== 5) 建测试角色与测试库（若不存在）=="
mkdir -p "$WORKDIR"          # 密码文件要落在这里，先建目录（步骤 6 还会再确保一次）
if [ ! -s "$TEST_PW_FILE" ]; then
  # 随机密码只落服务器本地文件（不进 git、不回显）
  umask 077
  openssl rand -hex 16 > "$TEST_PW_FILE"
fi
TEST_PW="$(cat "$TEST_PW_FILE")"
sudo -u postgres /usr/pgsql-${PG_VERSION}/bin/psql -p 5433 -tAc \
  "SELECT 1 FROM pg_roles WHERE rolname='${TEST_USER}'" | grep -q 1 \
  || sudo -u postgres /usr/pgsql-${PG_VERSION}/bin/psql -p 5433 -c \
       "CREATE ROLE ${TEST_USER} LOGIN PASSWORD '${TEST_PW}'" >/dev/null
sudo -u postgres /usr/pgsql-${PG_VERSION}/bin/psql -p 5433 -c \
  "ALTER ROLE ${TEST_USER} PASSWORD '${TEST_PW}'" >/dev/null
sudo -u postgres /usr/pgsql-${PG_VERSION}/bin/psql -p 5433 -tAc \
  "SELECT 1 FROM pg_database WHERE datname='${TEST_DB}'" | grep -q 1 \
  || sudo -u postgres /usr/pgsql-${PG_VERSION}/bin/psql -p 5433 -c \
       "CREATE DATABASE ${TEST_DB} OWNER ${TEST_USER}" >/dev/null
log "角色 ${TEST_USER} / 库 ${TEST_DB} 就绪（端口 5433，仅本机监听）"

log "== 6) Python ${PY_VER} 与仓库工作目录 =="
if ! command -v python${PY_VER} >/dev/null 2>&1; then
  dnf install -y python${PY_VER} python${PY_VER}-devel gcc gcc-c++ >/dev/null 2>&1 \
    || { dnf install -y python3.11 python3.11-devel gcc gcc-c++ >/dev/null; }
fi
python${PY_VER} --version
mkdir -p "$WORKDIR"

log "== 7) Java 21（opendataloader PDF 真路径；CI 用的就是 21）=="
if ! command -v java >/dev/null 2>&1; then
  dnf install -y java-21-openjdk-headless >/dev/null
  log "java 已安装"
fi
java -version 2>&1 | head -1

log "== 8) 防火墙：不开放 PG 端口（哪怕服务器防火墙是关的也不开）=="
if systemctl is-active firewalld >/dev/null 2>&1; then
  firewall-cmd --list-ports 2>/dev/null | tr ' ' '\n' | grep -qx "5433/tcp" \
    && firewall-cmd --permanent --remove-port=5433/tcp >/dev/null && firewall-cmd --reload >/dev/null \
    && log "已确保 5433 不在放行列表"
else
  log "firewalld 未运行（沿用现状，不做改动）"
fi

log "== 完成 =="
echo "PG 连接串（服务器本机）：postgresql+psycopg://${TEST_USER}:<见 ${TEST_PW_FILE}>@127.0.0.1:5433/${TEST_DB}"
echo "Mac 侧走隧道：ssh -N -L 5433:127.0.0.1:5433 kbase-test"
