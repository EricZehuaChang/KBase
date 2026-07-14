"""KB 级检索策略（M6-1.5）：三层合并解析、按库门控多路召回/重排、
请求级试跑覆盖与改写模式覆盖。"""
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.config import load_config
from kbase.retrieval_strategy import pick_min_score, resolve_strategy
from tests.test_api import CFG, MD, FakeLLM


def _cfg(tmp_path):
    p = tmp_path / "kbase.yaml"
    p.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                 encoding="utf-8")
    return load_config(p)


# ---------------- resolve_strategy / pick_min_score 纯函数 ----------------


def test_resolve_defaults_follow_global(tmp_path):
    cfg = _cfg(tmp_path)
    s = resolve_strategy(cfg, None)
    assert (s.use_keyword, s.use_rerank, s.rewrite_mode, s.candidates) == (
        cfg.retrieval.hybrid, cfg.retrieval.rerank.enabled,
        cfg.retrieval.rewrite.mode, cfg.retrieval.candidates)


def test_resolve_kb_overrides_and_request_wins(tmp_path):
    cfg = _cfg(tmp_path)
    kb = {"hybrid": False, "rewrite": "off", "candidates": 7}
    s = resolve_strategy(cfg, kb)
    assert s.use_keyword is False and s.rewrite_mode == "off" and s.candidates == 7
    # 请求覆盖 > KB 配置
    s2 = resolve_strategy(cfg, kb, overrides={"use_keyword": True, "candidates": 9})
    assert s2.use_keyword is True and s2.candidates == 9
    assert s2.rewrite_mode == "off"       # 未覆盖的键仍取 KB 层


def test_pick_min_score_follows_effective_rerank(tmp_path):
    cfg = _cfg(tmp_path)
    on = resolve_strategy(cfg, {"rerank": True})
    off = resolve_strategy(cfg, {"rerank": False})
    assert pick_min_score(cfg, on, rerank_available=True) == cfg.retrieval.min_score_rerank
    # 策略要重排但部署没装 reranker → 实际不重排，用余弦量纲
    assert pick_min_score(cfg, on, rerank_available=False) == cfg.retrieval.min_score_dense
    assert pick_min_score(cfg, off, rerank_available=True) == cfg.retrieval.min_score_dense


# ---------------- API 端到端 ----------------


class FakeReranker:
    def __init__(self):
        self.calls = 0

    def rerank(self, query, texts):
        self.calls += 1
        return [float(len(texts) - i) for i in range(len(texts))]


def _client(tmp_path, fake_embedder, reranker=False):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=reranker, auth="off")
    return TestClient(app)


def _kb_with_doc(c, retrieval: dict | None = None):
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    if retrieval is not None:
        c.put(f"/api/kb/{kb_id}/config", json={"retrieval": retrieval})
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    return kb_id


def test_kb_hybrid_off_skips_keyword_route(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = _kb_with_doc(c, retrieval={"hybrid": False})
    trace = c.post(f"/api/kb/{kb_id}/search",
                   json={"query": "住房补贴", "debug": True}).json()["trace"]
    assert "keyword" not in trace and "dense" in trace
    # 请求级强制打开（能力已安装：全局 hybrid=True 建了索引）→ 关键词路恢复
    trace2 = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "住房补贴", "debug": True,
                          "use_keyword": True}).json()["trace"]
    assert "keyword" in trace2


def test_kb_rerank_off_and_request_force_on(tmp_path, fake_embedder):
    """部署装了 reranker（注入 fake）：KB 策略关掉→不调用；请求级强制开→调用。"""
    fake_rr = FakeReranker()
    c = _client(tmp_path, fake_embedder, reranker=fake_rr)
    kb_id = _kb_with_doc(c, retrieval={"rerank": False})
    t1 = c.post(f"/api/kb/{kb_id}/search",
                json={"query": "住房补贴", "debug": True}).json()["trace"]
    assert t1["rerank_status"] == "off" and fake_rr.calls == 0
    t2 = c.post(f"/api/kb/{kb_id}/search",
                json={"query": "住房补贴", "debug": True,
                      "use_rerank": True}).json()["trace"]
    assert t2["rerank_status"] == "on" and fake_rr.calls == 1


def test_kb_config_retrieval_roundtrip_preserves_other_keys(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.put(f"/api/kb/{kb_id}/config",
          json={"chunk_size": 256, "retrieval": {"hybrid": False, "rewrite": "off"}})
    got = next(k for k in c.get("/api/kb").json() if k["id"] == kb_id)["config"]
    assert got["chunk_size"] == 256
    assert got["retrieval"] == {"hybrid": False, "rewrite": "off"}
    # 未知策略键被 extra=forbid 拒绝
    r = c.put(f"/api/kb/{kb_id}/config",
              json={"retrieval": {"hybird": True}})       # 故意拼错
    assert r.status_code == 422


def test_kb_rewrite_off_skips_rewriter_llm(tmp_path, fake_embedder):
    """rewrite=off 的库：会话查询完全不触碰改写 LLM（省调用），检索用原文。"""
    from kbase.rag.rewriter import QueryRewriter

    class SpyLLM:
        calls = 0

        async def complete(self, messages, **kw):
            SpyLLM.calls += 1
            return "改写后的问题"

    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False,
                     rewriter=QueryRewriter(llm=SpyLLM(), mode="always"),
                     auth="off")
    c = TestClient(app)
    kb_id = _kb_with_doc(c, retrieval={"rewrite": "off"})
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    # 两轮：第二轮才有历史（always 模式本应触发改写）
    for q in ["住房补贴怎么申领", "那需要什么材料"]:
        with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                      json={"question": q}) as r:
            for _ in r.iter_lines():
                pass
    assert SpyLLM.calls == 0      # off 策略下改写 LLM 从未被调用