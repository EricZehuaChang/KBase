"""向量模型密钥页面配置：脱敏清单、DB 覆盖优先于环境变量、改/清后缓存
失效重建、未知选项 404。"""
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM

CFG_EMB = CFG + """
embedders:
  - id: cloud-embed
    plugin: openai-embed
    base_url: https://embed.example/v1
    api_key_env: TEST_EMBED_KEY
    model: text-embedding-test
"""


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG_EMB.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_embedder_key_crud_and_masking(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)

    # 初始：清单出现 openai-embed 选项，无页面密钥
    items = c.get("/api/settings/embedder-keys").json()["items"]
    assert items == [{"id": "cloud-embed", "plugin": "openai-embed",
                      "model": "text-embedding-test",
                      "api_key_env": "TEST_EMBED_KEY",
                      "has_db_key": False, "key_hint": None}]

    # 配 key → 脱敏状态（尾4位），原文不出站
    r = c.put("/api/settings/embedder-keys/cloud-embed",
              json={"api_key": "sk-secret-abcd1234"})
    assert r.status_code == 200
    item = c.get("/api/settings/embedder-keys").json()["items"][0]
    assert item["has_db_key"] is True and item["key_hint"] == "…1234"
    assert "sk-secret" not in str(item)

    # 清除 → 回落 env；再删 404
    assert c.delete("/api/settings/embedder-keys/cloud-embed").json()["ok"] is True
    assert c.get("/api/settings/embedder-keys").json()["items"][0]["has_db_key"] is False
    assert c.delete("/api/settings/embedder-keys/cloud-embed").status_code == 404

    # 未知选项/无密钥概念的选项 404
    assert c.put("/api/settings/embedder-keys/nope",
                 json={"api_key": "x"}).status_code == 404


def test_pool_db_key_overrides_env(tmp_path, monkeypatch):
    """池级验证：无环境变量时 DB 密钥可让 openai-embed 构建成功（此前必
    ValueError）；invalidate 后按新解析结果重建。"""
    from kbase.config import load_config
    from kbase.plugins.embedders.factory import EmbedderPool

    monkeypatch.delenv("TEST_EMBED_KEY", raising=False)
    cfg_file = tmp_path / "kbase.yaml"
    cfg_file.write_text(
        CFG_EMB.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
        encoding="utf-8")
    cfg = load_config(cfg_file)

    db_key: dict = {}
    pool = EmbedderPool(cfg, default_embedder=object(),
                        key_resolver=lambda oid: db_key.get(oid))

    # 无 DB 密钥且 env 未设 → 构建必须失败（密钥缺失不能静默吞掉）
    with pytest.raises(RuntimeError, match="TEST_EMBED_KEY"):
        pool.get("cloud-embed")

    # 配上 DB 密钥 → 构建成功
    db_key["cloud-embed"] = "sk-from-db"
    emb = pool.get("cloud-embed")
    assert emb is not None
    # 缓存生效：同 id 复用同实例；invalidate 后重建为新实例
    assert pool.get("cloud-embed") is emb
    pool.invalidate("cloud-embed")
    assert pool.get("cloud-embed") is not emb
