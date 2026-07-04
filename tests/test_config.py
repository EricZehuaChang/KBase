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
