# KBase 生产镜像：单镜像同时支撑 lite（docker-compose.lite.yml）与
# standard（docker-compose.standard.yml）两种 profile —— 具体走哪条插槽
# 完全由挂载的 config/*.yaml + 环境变量决定，镜像本身不区分。
#
# 国内网络：若拉取 python:3.11-slim 缓慢，可将下行替换为国内镜像源，如
#   FROM docker.m.daocloud.io/library/python:3.11-slim
# 或改用阿里云 ACR 的镜像加速仓库；二选一，勿同时改多处。
FROM python:3.11-slim

# 运行期系统依赖：
# - libgomp1：torch（bge-local/reranker CPU 推理，lite profile 走进程内模型）
#   与部分 BLAS 后端运行时需要 OpenMP 动态库，slim 基础镜像默认不带。
# - curl：compose healthcheck 探测 /healthz 用，slim 镜像默认不带。
# standard profile 的 TEI/Qdrant/Postgres 都是独立容器，本镜像不需要为它们
# 装依赖；OCR（MonkeyOCR）是外部服务，本镜像也不内置任何 CV/OCR 库。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 分层缓存：先只拷贝依赖清单并安装，代码变更不会使这一层失效，重建镜像时
# 不用重新下载/编译依赖（torch 等体积较大，命中缓存对国内网络尤其重要）。
COPY pyproject.toml ./
# hatchling 需要能看到包目录才能完成 editable/normal 构建的元数据探测；
# 建一个空壳先满足 `pip install .[mcp]` 的依赖解析与安装，真正的代码在下一层
# COPY 覆盖进来（内容不同不影响已装的第三方依赖，仅重装本包自身，很快）。
RUN mkdir -p kbase kbase_mcp \
    && touch kbase/__init__.py kbase_mcp/__init__.py
RUN pip install --no-cache-dir ".[mcp]"

# 代码层：变更频繁，放在依赖安装之后，改代码不触发依赖重装。
# web/ 是前端构建产物（web-app/ 编译后提交进仓库的静态文件），镜像内不装
# Node/npm，直接拷贝现成的 dist 内容由 FastAPI 的 StaticFiles 托管。
COPY kbase/ kbase/
COPY kbase_mcp/ kbase_mcp/
COPY web/ web/
COPY config/ config/
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

EXPOSE 8100

ENTRYPOINT ["/app/entrypoint.sh"]
