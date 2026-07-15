"""M4-2 H4：Docker 部署产物的结构校验。

本地开发机大概率没有跑起来的 Docker daemon（`docker compose config` 需要
daemon 才能跑——实测本机只有 CLI 没有 daemon），所以这里不依赖 docker
命令，直接用 yaml.safe_load 解析两个 compose 文件，断言关键 service/volume/
healthcheck 存在；Dockerfile 用文本断言检查关键阶段都在。真实构建验证留给
H5（部署到有 Docker daemon 的 GCP VM 时）。
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))


def test_dockerfile_has_key_stages():
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.11-slim" in text
    assert "libgomp1" in text          # torch/bge 运行期依赖
    # local-embed 必须在镜像里：lite 档默认进程内 bge-m3，缺 extra 启动即炸
    assert 'pip install --no-cache-dir ".[mcp,local-embed]"' in text
    assert "COPY kbase/ kbase/" in text
    assert "COPY kbase_mcp/ kbase_mcp/" in text
    assert "COPY web/ web/" in text     # 前端构建产物，镜像内不装 Node
    assert "COPY config/ config/" in text
    assert "ENTRYPOINT" in text
    assert "EXPOSE 8100" in text


def test_entrypoint_waits_for_deps_then_execs_uvicorn():
    text = (REPO_ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    assert "KBASE_WAIT_FOR" in text
    assert "exec uvicorn --factory kbase.api.main:create_app" in text
    assert "--host 0.0.0.0" in text
    assert "--port 8100" in text


def test_dockerignore_excludes_dev_only_paths():
    text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for entry in ("data/", ".venv/", "材料/", "*.sqlite", ".env",
                  "tests/", "docs/", "eval/", "loadtest/"):
        assert entry in text, f"缺少排除项: {entry}"


def test_compose_lite_has_app_service_and_volumes():
    doc = _load_yaml("docker-compose.lite.yml")
    services = doc["services"]
    assert "app" in services
    app = services["app"]
    assert app["build"]["context"] == "."
    assert app["build"]["dockerfile"] == "Dockerfile"
    assert "8100:8100" in app["ports"]
    assert "healthcheck" in app
    assert "curl" in " ".join(app["healthcheck"]["test"])

    volume_targets = {v.split(":")[1] if ":" in v else v for v in app["volumes"]}
    assert any(v.endswith("/app/data") for v in app["volumes"])
    assert any("huggingface" in v for v in app["volumes"])
    assert "hf-cache" in doc["volumes"]

    # 密钥类环境变量必须存在（值走 host env / .env，不硬编码在文件里）
    for key in ("KBASE_SECRET_KEY", "KBASE_ADMIN_PASSWORD", "DASHSCOPE_API_KEY"):
        assert key in app["environment"]


def test_compose_standard_has_all_required_services():
    doc = _load_yaml("docker-compose.standard.yml")
    services = doc["services"]
    for name in ("app", "postgres", "qdrant", "tei-embed", "tei-rerank"):
        assert name in services, f"缺少 service: {name}"


def test_compose_standard_postgres_healthcheck_and_volume():
    doc = _load_yaml("docker-compose.standard.yml")
    pg = doc["services"]["postgres"]
    assert pg["image"].startswith("postgres:16")
    assert "pg_isready" in " ".join(pg["healthcheck"]["test"])
    assert any("var/lib/postgresql/data" in v for v in pg["volumes"])
    env = pg["environment"]
    assert env["POSTGRES_USER"] and env["POSTGRES_DB"]
    assert "POSTGRES_PASSWORD" in env


def test_compose_standard_qdrant_healthcheck_and_volume():
    doc = _load_yaml("docker-compose.standard.yml")
    qdrant = doc["services"]["qdrant"]
    assert qdrant["image"].startswith("qdrant/qdrant")
    assert "healthcheck" in qdrant
    assert any("qdrant/storage" in v for v in qdrant["volumes"])


def test_compose_standard_tei_services_use_correct_models():
    doc = _load_yaml("docker-compose.standard.yml")
    tei_embed = doc["services"]["tei-embed"]
    tei_rerank = doc["services"]["tei-rerank"]

    assert "text-embeddings-inference" in tei_embed["image"]
    assert "text-embeddings-inference" in tei_rerank["image"]
    assert "BAAI/bge-m3" in tei_embed["command"]
    assert "BAAI/bge-reranker-v2-m3" in tei_rerank["command"]
    assert "healthcheck" in tei_embed
    assert "healthcheck" in tei_rerank


def test_compose_standard_app_depends_on_all_healthy():
    doc = _load_yaml("docker-compose.standard.yml")
    app = doc["services"]["app"]
    depends_on = app["depends_on"]
    for name in ("postgres", "qdrant", "tei-embed", "tei-rerank"):
        assert depends_on[name]["condition"] == "service_healthy"


def test_compose_standard_app_mounts_standard_config():
    doc = _load_yaml("docker-compose.standard.yml")
    app = doc["services"]["app"]
    assert any("kbase.standard.yaml" in v and "/app/config/kbase.yaml" in v
                for v in app["volumes"])


def test_config_standard_yaml_points_at_compose_service_names():
    doc = _load_yaml("config/kbase.standard.yaml")
    assert doc["embedder"]["name"] == "tei"
    assert doc["embedder"]["endpoint"] == "http://tei-embed:80"
    assert doc["vectorstore"]["name"] == "qdrant"
    assert doc["vectorstore"]["endpoint"] == "http://qdrant:6333"
    assert doc["retrieval"]["rerank"]["name"] == "tei"
    assert doc["retrieval"]["rerank"]["endpoint"] == "http://tei-rerank:80"
    assert doc["db"]["url"].startswith("postgresql+psycopg://")
    assert "@postgres:5432/kbase" in doc["db"]["url"]
    assert doc["ocr"]["enabled"] is True


def test_config_standard_yaml_loads_via_app_config():
    """确保样例配置不仅 YAML 语法合法，还满足 AppConfig 的 pydantic 校验
    （复用生产同一条 load_config 路径，而不是自己拼断言）。"""
    import os

    from kbase.config import load_config

    os.environ.setdefault("DASHSCOPE_API_KEY", "test-key-for-validation")
    cfg = load_config(REPO_ROOT / "config" / "kbase.standard.yaml")
    assert cfg.embedder.name == "tei"
    assert cfg.vectorstore.name == "qdrant"
    assert cfg.db.url.startswith("postgresql+psycopg://")
