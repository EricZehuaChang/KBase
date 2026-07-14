"""模型目录（M5-2 Provider UI）：/models 拉取、缓存 TTL、手动刷新与
访问驱动的周更后台刷新。"""
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from kbase import model_catalog
from kbase.api.main import create_app
from kbase.models import AppSetting
from tests.test_api import CFG, FakeLLM

# ---------------- fetch_models ----------------


def _transport(models=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers.get("authorization", "").startswith("Bearer ")
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={
            "data": [{"id": m} for m in (models or [])]})
    return httpx.MockTransport(handler)


def test_fetch_models_sorted_dedup():
    got = model_catalog.fetch_models(
        "https://api.x.com/v1/", "sk-k",
        transport=_transport(["b-model", "a-model", "b-model"]))
    assert got == ["a-model", "b-model"]


def test_fetch_models_auth_error_mentions_key():
    with pytest.raises(RuntimeError, match="API Key"):
        model_catalog.fetch_models("https://api.x.com/v1", "bad",
                                   transport=_transport(status=401))


def test_fetch_models_empty_list_is_error():
    with pytest.raises(RuntimeError, match="空列表"):
        model_catalog.fetch_models("https://api.x.com/v1", "k",
                                   transport=_transport([]))


# ---------------- 缓存与 API ----------------


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_refresh_with_form_credentials_and_get(tmp_path, fake_embedder, monkeypatch):
    """表单场景：provider 还没建，用 base_url+api_key 直接拉取并缓存。"""
    c = _client(tmp_path, fake_embedder)
    monkeypatch.setattr(model_catalog, "fetch_models",
                        lambda base_url, key, **kw: ["m1", "m2"])
    r = c.post("/api/settings/models/refresh",
               json={"base_url": "https://api.corp.local/v1", "api_key": "sk-x"})
    assert r.status_code == 200
    assert r.json()["models"] == ["m1", "m2"] and r.json()["stale"] is False
    got = c.get("/api/settings/models").json()["catalogs"]
    assert got[0]["base_url"] == "https://api.corp.local/v1"
    assert got[0]["models"] == ["m1", "m2"]


def test_refresh_requires_some_credential(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/settings/models/refresh",
               json={"base_url": "https://api.x.com/v1"})
    assert r.status_code == 422
    assert "API Key" in r.json()["detail"]


def test_refresh_via_provider_name_uses_stored_key(tmp_path, fake_embedder, monkeypatch):
    """已存 provider 场景：按 name 用它存的直配密钥拉取。"""
    c = _client(tmp_path, fake_embedder)
    c.post("/api/settings/providers", json={
        "name": "corp", "base_url": "https://api.corp.local/v1",
        "api_key": "sk-stored", "model": "m"})
    seen = {}

    def fake_fetch(base_url, key, **kw):
        seen.update(base_url=base_url, key=key)
        return ["corp-model-1"]

    monkeypatch.setattr(model_catalog, "fetch_models", fake_fetch)
    r = c.post("/api/settings/models/refresh", json={"provider_name": "corp"})
    assert r.status_code == 200
    assert seen == {"base_url": "https://api.corp.local/v1", "key": "sk-stored"}


def test_stale_catalog_auto_refreshes_in_background(tmp_path, fake_embedder, monkeypatch):
    """访问驱动周更：GET 时对 stale 且有 provider 凭据的目录后台刷新
    （TestClient 的 bg 任务同步执行，本次请求后缓存即更新）。"""
    c = _client(tmp_path, fake_embedder)
    c.post("/api/settings/providers", json={
        "name": "corp", "base_url": "https://api.corp.local/v1",
        "api_key": "sk-stored", "model": "m"})
    # 手工植入一份 8 天前的过期缓存
    from kbase.config import load_config, resolve_db_url
    from kbase.db import make_session_factory
    sf = make_session_factory(resolve_db_url(load_config(tmp_path / "kbase.yaml")))
    old = (datetime.utcnow() - timedelta(days=8)).isoformat()
    with sf() as s:
        s.add(AppSetting(key="model_catalog:https://api.corp.local/v1",
                         value=json.dumps({"models": ["old"], "fetched_at": old})))
        s.commit()
    monkeypatch.setattr(model_catalog, "fetch_models",
                        lambda base_url, key, **kw: ["fresh-model"])
    first = c.get("/api/settings/models").json()["catalogs"][0]
    assert first["stale"] is True and first["models"] == ["old"]   # 本次仍回旧缓存
    second = c.get("/api/settings/models").json()["catalogs"][0]
    assert second["models"] == ["fresh-model"] and second["stale"] is False