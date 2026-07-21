"""i18n 覆盖表(方案 A):store CRUD + 公开读 + admin 写 + 空值删除回落基线
+ 公开端点绕过鉴权。前端基线 JSON 不在后端测试范围(那是打包资源)。"""
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


def _app(tmp_path, fake_embedder, auth="off"):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth=auth)


def _client(tmp_path, fake_embedder):
    return TestClient(_app(tmp_path, fake_embedder))


def test_overrides_empty_by_default(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    assert c.get("/api/i18n/en").json() == {}          # 空表=全用基线


def test_put_and_get_override(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    assert c.put("/api/i18n", json={"lang": "en", "key": "kb.create",
                                    "value": "Create KB"}).json()["result"] == "set"
    assert c.get("/api/i18n/en").json() == {"kb.create": "Create KB"}
    # 管理页全量视图
    allv = c.get("/api/i18n").json()
    assert allv["en"]["kb.create"] == "Create KB"


def test_upsert_overwrites(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    c.put("/api/i18n", json={"lang": "ms", "key": "common.save", "value": "A"})
    c.put("/api/i18n", json={"lang": "ms", "key": "common.save", "value": "Simpan"})
    assert c.get("/api/i18n/ms").json() == {"common.save": "Simpan"}


def test_empty_value_deletes_override(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    c.put("/api/i18n", json={"lang": "ms", "key": "common.save", "value": "Simpan!"})
    assert c.get("/api/i18n/ms").json() == {"common.save": "Simpan!"}
    r = c.put("/api/i18n", json={"lang": "ms", "key": "common.save", "value": ""})
    assert r.json()["result"] == "deleted"             # 空值=删除回落基线
    assert c.get("/api/i18n/ms").json() == {}


def test_langs_isolated(tmp_path, fake_embedder):
    """不同语言的同 key 覆盖互不干扰(复合主键 (lang,key))。"""
    c = _client(tmp_path, fake_embedder)
    c.put("/api/i18n", json={"lang": "en", "key": "x.y", "value": "EN"})
    c.put("/api/i18n", json={"lang": "ms", "key": "x.y", "value": "MS"})
    assert c.get("/api/i18n/en").json() == {"x.y": "EN"}
    assert c.get("/api/i18n/ms").json() == {"x.y": "MS"}


def test_public_read_bypasses_auth(tmp_path, fake_embedder):
    """关键设计:GET /api/i18n/{lang} 挂 app 级公开——生产鉴权下未登录
    (含免登录分享页)也可读;而 admin 端点未登录被拒。"""
    c = TestClient(_app(tmp_path, fake_embedder, auth="on"))
    assert c.get("/api/i18n/en").status_code == 200          # 公开读放行
    assert c.get("/api/i18n").status_code in (401, 403)      # admin 读被拒
    assert c.put("/api/i18n", json={"lang": "en", "key": "a.b",
                                    "value": "x"}).status_code in (401, 403)
