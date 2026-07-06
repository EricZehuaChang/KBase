#!/bin/sh
# 容器启动入口：standard profile 下先等 db/qdrant/tei 等依赖服务端口就绪
# 再启动 uvicorn，避免 app 在依赖容器还没 accept 连接时就跑迁移/健康检查
# 而失败退出（compose 的 depends_on.condition: service_healthy 已经保证了
# "健康检查通过"，但这里再加一层端口探测作为双保险，兼容用户自己起的、没
# healthcheck 的外部依赖）。lite profile 不设 KBASE_WAIT_FOR，直接跳过。
set -e

if [ -n "$KBASE_WAIT_FOR" ]; then
    echo "[entrypoint] KBASE_WAIT_FOR=$KBASE_WAIT_FOR，等待依赖端口就绪..."
    OLDIFS="$IFS"
    IFS=','
    for hostport in $KBASE_WAIT_FOR; do
        IFS="$OLDIFS"
        host="${hostport%:*}"
        port="${hostport#*:}"
        echo "[entrypoint] 等待 $host:$port ..."
        until python -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(('$host', $port))
except OSError:
    sys.exit(1)
finally:
    s.close()
"; do
            sleep 2
        done
        echo "[entrypoint] $host:$port 已就绪"
        IFS=','
    done
    IFS="$OLDIFS"
fi

echo "[entrypoint] 启动 uvicorn ..."
exec uvicorn --factory kbase.api.main:create_app --host 0.0.0.0 --port 8100
