from pathlib import Path
from kbase.config import load_config, resolve_db_url


def test_load_config(tmp_path: Path):
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        """
data_dir: ./data
embedder:
  name: bge-local
  model: BAAI/bge-m3
vectorstore:
  name: chroma
chunker:
  name: structure
  chunk_size: 512
  chunk_overlap: 64
llm:
  active: qwen-72b
  providers:
    - name: qwen-72b
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key_env: DASHSCOPE_API_KEY
      model: qwen2.5-72b-instruct
      max_concurrency: 4
    - name: qwen-32b
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key_env: DASHSCOPE_API_KEY
      model: qwen2.5-32b-instruct
      max_concurrency: 4
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.embedder.name == "bge-local"
    assert cfg.chunker.chunk_size == 512
    assert cfg.llm.active == "qwen-72b"
    assert cfg.llm.providers[1].model == "qwen2.5-32b-instruct"
    assert cfg.get_provider("qwen-32b").base_url.startswith("https://dashscope")
    assert cfg.ingest.workers == 2      # D5：默认并行度


def test_provider_params_block(tmp_path: Path):
    """有 params 块时正常解析；没有时默认为 {}。"""
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        """
data_dir: ./data
llm:
  active: with-params
  providers:
    - name: with-params
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key_env: DASHSCOPE_API_KEY
      model: qwen3-32b
      max_concurrency: 4
      params:
        extra_body:
          enable_thinking: false
    - name: without-params
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key_env: DASHSCOPE_API_KEY
      model: qwen-plus
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.get_provider("with-params").params == {
        "extra_body": {"enable_thinking": False}}
    assert cfg.get_provider("without-params").params == {}


def test_get_provider_unknown_raises(tmp_path: Path):
    import pytest
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        "data_dir: ./data\nllm:\n  active: a\n  providers:\n    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    with pytest.raises(KeyError):
        cfg.get_provider("nope")


def test_active_not_in_providers_raises(tmp_path: Path):
    import pytest
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        "data_dir: ./data\nllm:\n  active: nope\n  providers:\n    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="nope"):
        load_config(cfg_file)


def test_embedder_and_rerank_endpoint_default_to_none(tmp_path: Path):
    """M4-2 H1：新增字段默认不填，向后兼容既有 lite 配置。"""
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        "data_dir: ./data\nllm:\n  active: a\n  providers:\n    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.embedder.endpoint is None
    assert cfg.retrieval.rerank.name == "bge-local"
    assert cfg.retrieval.rerank.endpoint is None


def test_embedder_and_rerank_tei_endpoint_parses(tmp_path: Path):
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        """
data_dir: ./data
embedder:
  name: tei
  endpoint: http://tei-embed:80
retrieval:
  rerank:
    name: tei
    endpoint: http://tei-rerank:80
llm:
  active: a
  providers:
    - {name: a, base_url: 'http://x', api_key_env: K, model: m}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.embedder.name == "tei"
    assert cfg.embedder.endpoint == "http://tei-embed:80"
    assert cfg.retrieval.rerank.name == "tei"
    assert cfg.retrieval.rerank.endpoint == "http://tei-rerank:80"


def test_vectorstore_endpoint_and_api_key_default_to_none(tmp_path: Path):
    """M4-2 H2：新增字段默认不填，向后兼容既有 lite（chroma）配置。"""
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        "data_dir: ./data\nllm:\n  active: a\n  providers:\n    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.vectorstore.name == "chroma"
    assert cfg.vectorstore.endpoint is None
    assert cfg.vectorstore.api_key is None


def test_vectorstore_qdrant_endpoint_parses(tmp_path: Path):
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        """
data_dir: ./data
vectorstore:
  name: qdrant
  endpoint: http://qdrant:6333
  api_key: secret
llm:
  active: a
  providers:
    - {name: a, base_url: 'http://x', api_key_env: K, model: m}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.vectorstore.name == "qdrant"
    assert cfg.vectorstore.endpoint == "http://qdrant:6333"
    assert cfg.vectorstore.api_key == "secret"


def test_db_url_defaults_to_sqlite_placeholder(tmp_path: Path):
    """M4-2 H3：新增字段默认不填，向后兼容既有 lite 配置——默认值携带
    {data_dir} 占位符，由 create_app 负责替换成实际路径。"""
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        "data_dir: ./data\nllm:\n  active: a\n  providers:\n    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.db.url == "sqlite:///{data_dir}/kbase.sqlite"


def test_db_postgresql_url_parses_verbatim(tmp_path: Path):
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        """
data_dir: ./data
db:
  url: postgresql+psycopg://user:pass@pg-host:5432/kbase
llm:
  active: a
  providers:
    - {name: a, base_url: 'http://x', api_key_env: K, model: m}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.db.url == "postgresql+psycopg://user:pass@pg-host:5432/kbase"


def test_resolve_db_url_substitutes_sqlite_placeholder(tmp_path: Path):
    """review fix：sqlite 默认带 {data_dir} 占位符时才替换。"""
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        "data_dir: ./data\nllm:\n  active: a\n  providers:\n    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert resolve_db_url(cfg) == "sqlite:///data/kbase.sqlite"


def test_resolve_db_url_passes_through_literal_brace_in_pg_url(tmp_path: Path):
    """review fix：db.url.format() 的脆弱性——PG 密码/URL 中若含字面 "{"，
    无条件 .format(data_dir=...) 会因缺少匹配字段名而抛 KeyError 崩溃；
    只有确认存在 "{data_dir}" 占位符时才应替换，否则必须原样透传不崩溃。"""
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        """
data_dir: ./data
db:
  url: "postgresql+psycopg://user:pa{ss@pg-host:5432/kbase"
llm:
  active: a
  providers:
    - {name: a, base_url: 'http://x', api_key_env: K, model: m}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    # 不崩溃，且字面 "{" 原样保留（不是 {data_dir} 占位符，不做任何替换）
    assert resolve_db_url(cfg) == "postgresql+psycopg://user:pa{ss@pg-host:5432/kbase"


def test_server_threadpool_size_defaults_to_anyio_default(tmp_path: Path):
    """M4-2 H7：不配置 server 时，threadpool_size 默认 40——与 AnyIO 库自身
    默认线程池容量一致，保证"不配置=零行为变化"。"""
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        "data_dir: ./data\nllm:\n  active: a\n  providers:\n    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.server.threadpool_size == 40


def test_server_threadpool_size_configurable(tmp_path: Path):
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        """
data_dir: ./data
server:
  threadpool_size: 120
llm:
  active: a
  providers:
    - {name: a, base_url: 'http://x', api_key_env: K, model: m}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.server.threadpool_size == 120


# ------------------------------------------- T05/G03：PG 密码走环境变量

def _pg_cfg(tmp_path: Path, *, password_env: str | None, url: str) -> Path:
    f = tmp_path / "kbase.yaml"
    env_line = f"  password_env: {password_env}\n" if password_env else ""
    f.write_text(
        f"data_dir: ./data\ndb:\n  url: \"{url}\"\n" + env_line
        + "llm:\n  active: a\n  providers:\n"
          "    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8")
    return f


def test_resolve_db_url_renders_password_env(tmp_path: Path, monkeypatch):
    """设了 password_env：密码从环境变量渲染进 URL，配置文件里不出现密码值。"""
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")
    cfg = load_config(_pg_cfg(
        tmp_path, password_env="POSTGRES_PASSWORD",
        url="postgresql+psycopg://kbase@postgres:5432/kbase"))
    assert cfg.db.password_env == "POSTGRES_PASSWORD"
    url = resolve_db_url(cfg)
    assert url == "postgresql+psycopg://kbase:s3cret@postgres:5432/kbase"
    # 用 SQLAlchemy 反解确认 host/库名/用户名都没被密码搅乱
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    assert (parsed.username, parsed.host, parsed.port, parsed.database) == (
        "kbase", "postgres", 5432, "kbase")


def test_resolve_db_url_escapes_special_chars_in_password(tmp_path: Path,
                                                          monkeypatch):
    """密码含 @ : / % { 等字符时仍拼出可解析的 URL（字符串拼接会拼坏）。"""
    from sqlalchemy.engine import make_url
    for password in ["p@ss:word/1", "50%off", "pa{ss}", "a b#c?d", "p&s=s"]:
        monkeypatch.setenv("POSTGRES_PASSWORD", password)
        cfg = load_config(_pg_cfg(
            tmp_path, password_env="POSTGRES_PASSWORD",
            url="postgresql+psycopg://kbase@postgres:5432/kbase"))
        parsed = make_url(resolve_db_url(cfg))
        assert parsed.password == password, (password, resolve_db_url(cfg))
        assert parsed.host == "postgres" and parsed.database == "kbase"


def test_resolve_db_url_missing_password_env_raises(tmp_path: Path,
                                                    monkeypatch):
    """变量缺失必须明确报错，不静默退回（静默失败会拿错密码连库）。"""
    import pytest

    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    cfg = load_config(_pg_cfg(
        tmp_path, password_env="POSTGRES_PASSWORD",
        url="postgresql+psycopg://kbase@postgres:5432/kbase"))
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        resolve_db_url(cfg)


def test_resolve_db_url_without_password_env_unchanged(tmp_path: Path,
                                                       monkeypatch):
    """没设 password_env：{data_dir} 语义与其它 URL 透传都与改造前一致。"""
    monkeypatch.setenv("POSTGRES_PASSWORD", "should-be-ignored")
    cfg = load_config(_pg_cfg(
        tmp_path, password_env=None,
        url="postgresql+psycopg://kbase:PASSWORD@postgres:5432/kbase"))
    assert cfg.db.password_env is None
    assert resolve_db_url(cfg) == \
        "postgresql+psycopg://kbase:PASSWORD@postgres:5432/kbase"

    # sqlite 默认路径不受 password_env 特性影响
    f = tmp_path / "lite.yaml"
    f.write_text(
        "data_dir: ./data\nllm:\n  active: a\n  providers:\n"
        "    - {name: a, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8")
    assert resolve_db_url(load_config(f)) == "sqlite:///data/kbase.sqlite"


def test_standard_profile_config_loads_with_password_env():
    """仓库自带的 config/kbase.standard.yaml：url 不含密码串，password_env
    指向 compose 透传的变量名（防止有人把密码又写回配置文件）。"""
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parent.parent
    raw = (root / "config" / "kbase.standard.yaml").read_text(encoding="utf-8")
    assert "PASSWORD@" not in raw and "password_env: POSTGRES_PASSWORD" in raw
    cfg = load_config(root / "config" / "kbase.standard.yaml")
    assert cfg.db.password_env == "POSTGRES_PASSWORD"
    assert ":" not in cfg.db.url.split("//", 1)[1].split("@", 1)[0]  # 无 user:pass

    compose = (root / "docker-compose.standard.yml").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-}" in compose
