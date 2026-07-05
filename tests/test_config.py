from pathlib import Path
from kbase.config import load_config


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
