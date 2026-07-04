# KBase M2 Plan A（后端检索增强）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 spec 工作流一：混合检索（FTS5+jieba+RRF）、重排插槽、上下文增强、OCR 路由与 pending_ocr、会话/全文/Provider 管理接口、检索调试与评测档位对比。

**Architecture:** 在 M1 插件化内核上增量演进：新增 KeywordIndex 内核组件（FTS5）、Reranker/Enricher/OCRBackend 三个插槽实现、Retriever 改造为分级管道（每级可开关+trace），启动时轻量迁移管理 schema 演进。API 层新增会话、设置、全文、调试端点；既有端点全部保持兼容（旧前端与评测脚本在 Plan B 完成前继续工作）。

**Tech Stack:** 既有栈 + jieba（中文分词）、pypdf（文本层探测）、sentence-transformers CrossEncoder（bge-reranker-v2-m3）。

**Spec:** `docs/superpowers/specs/2026-07-05-kbase-m2-design.md`
**基线：** main @ 8e360fa，40 passed / 2 deselected。执行分支 `feature/m2-backend`。

**约定（沿 M1）：** 工作目录 `D:\Claude Code\RAG`；`.venv\Scripts\python`；PowerShell 5.1 无 `&&`；`.env` 加载须 `Get-Content -Encoding utf8`；external 测试默认跳过；每任务一提交。

---

## 文件结构（Plan A 全量）

```
kbase/
├── migrations.py                # 启动时幂等迁移（新列/新表/FTS5）
├── models.py                    # [改] +Conversation/Message/ProviderRow/AppSetting；Document +ocr_confidence
├── db.py                        # [改] make_session_factory 调 run_migrations
├── config.py                    # [改] +RetrievalConfig/OCRConfig/EnrichConfig
├── index/
│   ├── __init__.py
│   └── keyword.py               # KeywordIndex：FTS5+jieba，index/search/delete_doc
├── plugins/
│   ├── base.py                  # [改] +Reranker/Enricher/OCRBackend Protocol；VectorStore 移除 keyword_search
│   ├── rerankers/bge_local.py   # CrossEncoder bge-reranker-v2-m3
│   ├── enrichers/contextual.py  # 上下文增强（LLM 定位说明）
│   └── ocr/monkey_http.py       # MonkeyOCR HTTP 适配器
├── ingest/pipeline.py           # [改] OCR 路由、FTS 入索引、enrich、kb config 读取
├── rag/retriever.py             # [改] 分级管道：双路→RRF→重排→父块；debug trace
├── rag/generator.py             # [改] min_score 参数化 + history 支持
├── conversations.py             # 会话领域逻辑（建/列/消息/多轮组装）
├── reindex.py                   # python -m kbase.reindex --kb <id>
└── api/main.py                  # [改] 新端点：会话/设置/全文/调试/重试/删除
eval/run_eval.py                 # [改] --tiers 档位对比模式
tests/                           # 每任务对应新测试文件
```

---

### Task A1: 迁移基建与新表

**Files:**
- Create: `kbase/migrations.py`
- Modify: `kbase/models.py`, `kbase/db.py`, `pyproject.toml`
- Test: `tests/test_migrations.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_migrations.py
from sqlalchemy import inspect, text

from kbase.db import make_session_factory


def test_migrations_add_columns_and_tables(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        insp = inspect(s.get_bind())
        chunk_cols = {c["name"] for c in insp.get_columns("chunks")}
        assert "enrich_context" in chunk_cols
        kb_cols = {c["name"] for c in insp.get_columns("knowledge_bases")}
        assert "config" in kb_cols
        doc_cols = {c["name"] for c in insp.get_columns("documents")}
        assert "ocr_confidence" in doc_cols
        tables = set(insp.get_table_names())
        assert {"conversations", "messages", "providers", "app_settings"} <= tables
        assert s.execute(text(
            "SELECT name FROM sqlite_master WHERE name='chunks_fts'"
        )).scalar() == "chunks_fts"


def test_migrations_idempotent(tmp_path):
    url = f"sqlite:///{tmp_path}/kb.sqlite"
    make_session_factory(url)
    factory = make_session_factory(url)     # 第二次不应报错
    with factory() as s:
        s.execute(text(
            "INSERT INTO chunks_fts(chunk_id, kb_id, doc_id, text) "
            "VALUES ('c1','kb1','d1','测试 内容')"))
        s.commit()
        got = s.execute(text(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH '测试'"
        )).scalar()
        assert got == "c1"


def test_existing_m1_db_upgrades(tmp_path):
    """模拟 M1 旧库（无新列）→ 迁移后可用。"""
    import sqlite3
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE knowledge_bases (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200), created_at DATETIME);
        CREATE TABLE documents (id VARCHAR(36) PRIMARY KEY, kb_id VARCHAR(36), filename VARCHAR(500),
            content_hash VARCHAR(64), status VARCHAR(20), error TEXT, created_at DATETIME);
        CREATE TABLE chunks (id VARCHAR(36) PRIMARY KEY, doc_id VARCHAR(36), kb_id VARCHAR(36),
            parent_id VARCHAR(36), prev_id VARCHAR(36), next_id VARCHAR(36),
            heading_path TEXT, text TEXT, is_leaf BOOLEAN);
    """)
    conn.close()
    factory = make_session_factory(f"sqlite:///{db}")
    with factory() as s:
        insp = inspect(s.get_bind())
        assert "enrich_context" in {c["name"] for c in insp.get_columns("chunks")}
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_migrations.py -v`
Expected: FAIL（ModuleNotFoundError 或断言失败）

- [ ] **Step 3: 实现**

models.py 追加（既有类不动，Document 加一列）：

```python
class Document(Base):
    # ...既有列不变，追加：
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class KnowledgeBase(Base):
    # ...既有列不变，追加：
    config: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON: 分块/增强配置


class Chunk(Base):
    # ...既有列不变，追加：
    enrich_context: Mapped[str | None] = mapped_column(Text, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conv_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))            # user | assistant
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProviderRow(Base):
    __tablename__ = "providers"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_env: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    max_concurrency: Mapped[int] = mapped_column(default=4)
    params: Mapped[str | None] = mapped_column(Text, nullable=True)      # JSON


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
```

（import 处补 `Float`。）

`kbase/migrations.py`：

```python
"""启动时幂等迁移：SQLite 轻量 schema 演进，不引入 Alembic。
create_all 只建缺失的表；本模块补既有表的缺列与 FTS5 虚拟表。"""
from sqlalchemy import inspect, text

_COLUMN_MIGRATIONS = [
    ("chunks", "enrich_context", "TEXT"),
    ("knowledge_bases", "config", "TEXT"),
    ("documents", "ocr_confidence", "REAL"),
]

_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
    "chunk_id UNINDEXED, kb_id UNINDEXED, doc_id UNINDEXED, text, "
    "tokenize='unicode61')"
)


def run_migrations(engine) -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl_type in _COLUMN_MIGRATIONS:
            if table in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns(table)}
                if column not in cols:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        conn.execute(text(_FTS_DDL))
```

db.py 的 `make_session_factory` 在 `create_all` 之后调用 `run_migrations(engine)`。

pyproject dependencies 追加：`"jieba>=0.42"`, `"pypdf>=4.0"`。执行 `.venv\Scripts\pip install -e ".[dev]"`。

注意：FTS 表的分词文本由 KeywordIndex 写入时用 jieba 预分词、空格连接，`unicode61` 只负责按空格切——迁移层不关心分词。

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `.venv\Scripts\python -m pytest tests/test_migrations.py -v` → 3 passed
Run: `.venv\Scripts\python -m pytest` → 43 passed, 2 deselected（40+3，无回归）

- [ ] **Step 5: Commit**

```bash
git add kbase/migrations.py kbase/models.py kbase/db.py pyproject.toml tests/test_migrations.py
git commit -m "feat: 启动迁移基建（新列/会话/providers/FTS5 表）"
```

---

### Task A2: KeywordIndex（FTS5 + jieba）

**Files:**
- Create: `kbase/index/__init__.py`（空）、`kbase/index/keyword.py`
- Modify: `kbase/ingest/pipeline.py`（叶子块入 FTS 索引）
- Test: `tests/test_keyword_index.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_keyword_index.py
from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex


def _mk(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    idx = KeywordIndex(factory)
    idx.index("kb1", [
        ("c1", "d1", "兵团本级机关事业单位工作人员差旅费管理办法 新兵办发〔2014〕76号"),
        ("c2", "d1", "住房补贴的申领条件为连续工作满两年"),
        ("c3", "d2", "公务卡结算范围包括办公用品采购"),
    ])
    return idx


def test_exact_term_hit(tmp_path):
    idx = _mk(tmp_path)
    hits = idx.search("kb1", "新兵办发〔2014〕76号", top_k=3)
    assert hits and hits[0].chunk_id == "c1"


def test_chinese_word_hit(tmp_path):
    idx = _mk(tmp_path)
    hits = idx.search("kb1", "公务卡的结算范围", top_k=3)
    assert hits and hits[0].chunk_id == "c3"


def test_kb_isolation(tmp_path):
    idx = _mk(tmp_path)
    assert idx.search("kb2", "住房补贴", top_k=3) == []


def test_delete_doc(tmp_path):
    idx = _mk(tmp_path)
    idx.delete_doc("d1")
    assert idx.search("kb1", "住房补贴", top_k=3) == []
    assert idx.search("kb1", "公务卡", top_k=3)      # d2 不受影响


def test_no_match_returns_empty(tmp_path):
    idx = _mk(tmp_path)
    assert idx.search("kb1", "量子力学", top_k=3) == []
```

- [ ] **Step 2: 运行确认失败**（ModuleNotFoundError）

- [ ] **Step 3: 实现 keyword.py**

```python
# kbase/index/keyword.py
"""关键词索引：SQLite FTS5 + jieba。写入与查询用同一分词器，
FTS 侧 tokenize=unicode61 只按空格切预分词结果。"""
import jieba
from sqlalchemy import text

from kbase.plugins.base import Hit


def _tokenize(s: str) -> str:
    return " ".join(t.strip() for t in jieba.cut_for_search(s) if t.strip())


def _fts_query(s: str) -> str:
    # 每个词加引号防 FTS 语法字符，OR 连接保召回（融合层负责排序）
    tokens = [t for t in jieba.cut_for_search(s) if t.strip()]
    return " OR ".join(f'"{t}"' for t in tokens) if tokens else '""'


class KeywordIndex:
    def __init__(self, session_factory):
        self._sf = session_factory

    def index(self, kb_id: str, rows: list[tuple[str, str, str]]) -> None:
        """rows: [(chunk_id, doc_id, raw_text)]"""
        if not rows:
            return
        with self._sf() as s:
            for chunk_id, doc_id, raw in rows:
                s.execute(text(
                    "INSERT INTO chunks_fts(chunk_id, kb_id, doc_id, text) "
                    "VALUES (:c, :k, :d, :t)"),
                    {"c": chunk_id, "k": kb_id, "d": doc_id, "t": _tokenize(raw)})
            s.commit()

    def search(self, kb_id: str, query: str, top_k: int = 20) -> list[Hit]:
        with self._sf() as s:
            rows = s.execute(text(
                "SELECT chunk_id, bm25(chunks_fts) AS r FROM chunks_fts "
                "WHERE chunks_fts MATCH :q AND kb_id = :k "
                "ORDER BY r LIMIT :n"),
                {"q": _fts_query(query), "k": kb_id, "n": top_k}).fetchall()
        # bm25 越小越相关，取负转为越大越好
        return [Hit(chunk_id=r[0], score=-float(r[1]), meta={"route": "keyword"})
                for r in rows]

    def delete_doc(self, doc_id: str) -> None:
        with self._sf() as s:
            s.execute(text("DELETE FROM chunks_fts WHERE doc_id = :d"),
                      {"d": doc_id})
            s.commit()
```

- [ ] **Step 4: 接线 IngestPipeline**

`IngestPipeline.__init__` 增加可选参数 `keyword_index: KeywordIndex | None = None`（默认 None 保持既有测试兼容）。`_process` 在 SQLite 持久化之后追加：

```python
        if self._keyword_index and leaves:
            self._keyword_index.index(
                kb_id, [(c.id, doc_id, f"{c.heading_path}\n{c.text}") for c in leaves])
```

tests/test_ingest.py 追加一个用例：构造带 KeywordIndex 的 pipeline，摄取后 `idx.search` 能命中文档内容。

- [ ] **Step 5: 运行 + 回归**：本文件 5 passed + 追加用例 passed；全量 49 passed, 2 deselected

- [ ] **Step 6: Commit**

```bash
git add kbase/index/ kbase/ingest/pipeline.py tests/test_keyword_index.py tests/test_ingest.py
git commit -m "feat: KeywordIndex（FTS5+jieba 中文关键词路）并接入摄取"
```

---

### Task A3: 检索管道分级改造（混合 + RRF + trace）

**Files:**
- Modify: `kbase/config.py`（+RetrievalConfig）、`kbase/rag/retriever.py`、`kbase/api/main.py`（组装处传入新依赖）
- Test: `tests/test_hybrid_retriever.py`（新）；`tests/test_retriever.py` 既有用例保持通过

- [ ] **Step 1: 失败测试**

```python
# tests/test_hybrid_retriever.py
from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.rag.retriever import Retriever, rrf_fuse

MD = """# 差旅办法
## 第一章 交通
乘坐火车出行按新兵办发〔2014〕76号文件执行。
## 第二章 住宿
住宿标准按级别确定。
"""


def test_rrf_fuse_math():
    a = [("x", 0.9), ("y", 0.8)]          # (chunk_id, score) 有序列表
    b = [("y", 5.0), ("z", 4.0)]
    fused = rrf_fuse([a, b], k=60)
    scores = dict(fused)
    # y 双路命中：1/(60+2) + 1/(60+1)
    assert abs(scores["y"] - (1 / 62 + 1 / 61)) < 1e-9
    assert fused[0][0] == "y"             # 双路命中排最前
    assert set(scores) == {"x", "y", "z"}


def _setup(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    kw = KeywordIndex(factory)
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=30, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "files",
                              keyword_index=kw)
    f = tmp_path / "差旅办法.md"
    f.write_text(MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "差旅办法.md")
    return factory, fake_embedder, store, kw


def test_hybrid_recalls_keyword_only_hit(tmp_path, fake_embedder):
    """FakeEmbedder 对不同文本给随机向量——文件号查询在稠密路必然命中差，
    但关键词路能精确命中；混合检索必须把它捞回来。"""
    factory, emb, store, kw = _setup(tmp_path, fake_embedder)
    r = Retriever(factory, emb, store, keyword_index=kw)
    blocks = r.retrieve("kb1", "新兵办发〔2014〕76号", top_k=3)
    assert any("76号" in b.text for b in blocks)


def test_pure_dense_mode_unchanged(tmp_path, fake_embedder):
    factory, emb, store, kw = _setup(tmp_path, fake_embedder)
    r = Retriever(factory, emb, store)         # 不传 keyword_index = 纯向量档
    q = "差旅办法.md > 差旅办法 > 第二章 住宿\n住宿标准按级别确定。"
    blocks = r.retrieve("kb1", q, top_k=3)
    assert blocks and "住宿标准" in blocks[0].text


def test_debug_trace(tmp_path, fake_embedder):
    factory, emb, store, kw = _setup(tmp_path, fake_embedder)
    r = Retriever(factory, emb, store, keyword_index=kw)
    result = r.retrieve("kb1", "火车 出行", top_k=3, debug=True)
    assert result.trace is not None
    assert set(result.trace) >= {"dense", "keyword", "fused"}
    assert result.blocks is not None
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

config.py 追加：

```python
class RerankConfig(BaseModel):
    enabled: bool = True
    model: str = "BAAI/bge-reranker-v2-m3"


class RetrievalConfig(BaseModel):
    hybrid: bool = True
    candidates: int = 20          # 每路召回数与融合候选数
    rrf_k: int = 60
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    min_score_dense: float = 0.3
    min_score_rerank: float = 0.35


class AppConfig(BaseModel):
    # ...追加：
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
```

retriever.py 重构（保持既有 ContextBlock 与父块组装逻辑，新增管道）：

```python
# kbase/rag/retriever.py 结构（完整实现）
from dataclasses import dataclass, field


def rrf_fuse(ranked_lists: list[list[tuple[str, float]]], k: int = 60
             ) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion。输入各路 (chunk_id, score) 有序列表，
    输出按融合分降序的 (chunk_id, fused_score)。"""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (cid, _s) in enumerate(ranked, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


@dataclass
class RetrievalResult:
    blocks: list          # list[ContextBlock]
    trace: dict | None = None


class Retriever:
    def __init__(self, session_factory, embedder, store,
                 keyword_index=None, reranker=None,
                 candidates: int = 20, rrf_k: int = 60):
        self._sf = session_factory
        self._embedder = embedder
        self._store = store
        self._kw = keyword_index
        self._reranker = reranker
        self._candidates = candidates
        self._rrf_k = rrf_k

    @property
    def rerank_active(self) -> bool:
        return self._reranker is not None

    def retrieve(self, kb_id, query, top_k=5, debug=False):
        """debug=False 返回 list[ContextBlock]（向后兼容）；
        debug=True 返回 RetrievalResult(blocks, trace)。"""
        trace: dict = {}
        vec = self._embedder.embed([query])[0]
        dense_hits = self._store.search(kb_id, vec, top_k=self._candidates)
        dense = [(h.chunk_id, h.score) for h in dense_hits]
        cosine = {h.chunk_id: h.score for h in dense_hits}
        trace["dense"] = dense

        if self._kw is not None:
            kw_hits = self._kw.search(kb_id, query, top_k=self._candidates)
            keyword = [(h.chunk_id, h.score) for h in kw_hits]
            trace["keyword"] = keyword
            fused = rrf_fuse([dense, keyword], k=self._rrf_k)[: self._candidates]
        else:
            fused = dense[: self._candidates]
        trace["fused"] = fused

        candidate_ids = [cid for cid, _ in fused]
        # 关键词路独有候选补算余弦（用 Chroma 存量向量，保证阈值语义统一）
        missing = [cid for cid in candidate_ids if cid not in cosine]
        if missing:
            cosine.update(self._cosine_from_store(kb_id, missing, vec))

        if self._reranker is not None:
            texts = self._leaf_texts(candidate_ids)
            scores = self._reranker.rerank(query, [texts[cid] for cid in candidate_ids])
            reranked = sorted(zip(candidate_ids, scores),
                              key=lambda kv: kv[1], reverse=True)
            trace["reranked"] = reranked
            ordered = [(cid, s) for cid, s in reranked[:top_k]]
        else:
            ordered = [(cid, cosine.get(cid, 0.0)) for cid in candidate_ids[:top_k]]

        blocks = self._assemble(kb_id, ordered)
        if debug:
            return RetrievalResult(blocks=blocks, trace=trace)
        return blocks
```

其中：`_cosine_from_store` 用 `ChromaStore.get_vectors(collection, ids)`（本任务给 ChromaStore 加这个只读方法：`collection.get(ids=ids, include=["embeddings"])`，归一化向量点积即余弦）；`_leaf_texts` 批量取 SQLite 叶子块文本（heading_path+"\n"+text，与向量化文本一致）；`_assemble` 即 M1 的父块组装+去重逻辑（按 ordered 顺序，score 用 ordered 中的分数）。

api/main.py 组装处：构造 `KeywordIndex(sf)`（cfg.retrieval.hybrid 为真时），传入 Retriever；query 路由对 `retrieve` 的调用保持返回 blocks（debug=False 分支签名不变）。Reranker 本任务先不接（A4 接）。

- [ ] **Step 4: 运行 + 回归**：新文件 4 passed；`tests/test_retriever.py` 既有 2 条不改动且通过；全量 53 passed, 2 deselected

- [ ] **Step 5: Commit**

```bash
git add kbase/config.py kbase/rag/retriever.py kbase/plugins/vectorstores/chroma_store.py kbase/api/main.py tests/test_hybrid_retriever.py
git commit -m "feat: 混合检索管道（双路召回+RRF 融合+debug trace+余弦补算）"
```

---

### Task A4: Reranker 插槽与降级

**Files:**
- Modify: `kbase/plugins/base.py`（+Reranker Protocol；VectorStore 移除 keyword_search）、`kbase/rag/generator.py`（min_score 参数化）、`kbase/api/main.py`（重排接线+降级+healthz）
- Create: `kbase/plugins/rerankers/__init__.py`（空）、`kbase/plugins/rerankers/bge_local.py`
- Test: `tests/test_reranker.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_reranker.py
import pytest


def test_module_importable_without_model():
    import kbase.plugins.rerankers.bge_local  # noqa: F401


def test_generator_min_score_param():
    """MIN_SCORE 从模块常量改为构造参数，拒答阈值随模式切换。"""
    from kbase.rag.generator import Generator
    from kbase.rag.retriever import ContextBlock

    class FakeLLM:
        async def stream(self, messages, **params):
            yield "答"

    b = ContextBlock(doc_id="d", doc_name="n", heading_path="h",
                     text="t", snippet="s", score=0.32)
    gen_low = Generator(FakeLLM(), min_score=0.3)
    gen_high = Generator(FakeLLM(), min_score=0.35)
    assert gen_low.usable_blocks([b]) == [b]
    assert gen_high.usable_blocks([b]) == []


@pytest.mark.external
def test_bge_reranker_orders_by_relevance():
    from kbase.plugins.rerankers.bge_local import BgeLocalReranker
    r = BgeLocalReranker()
    scores = r.rerank("住房补贴的申领条件",
                      ["申领住房补贴需连续工作满两年", "食堂周五供应红烧肉"])
    assert scores[0] > scores[1]
```

- [ ] **Step 2: 运行确认失败**（非 external 两条失败）

- [ ] **Step 3: 实现**

base.py 追加（并删除 `VectorStore.keyword_search` 方法声明——已被 KeywordIndex 取代，无实现者）：

```python
@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, texts: list[str]) -> list[float]: ...
```

`kbase/plugins/rerankers/bge_local.py`：

```python
from kbase.plugins.registry import registry


@registry.register("reranker", "bge-local")
class BgeLocalReranker:
    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3", device: str | None = None):
        from sentence_transformers import CrossEncoder   # 延迟 import
        self._model = CrossEncoder(model, device=device)

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        return [float(s) for s in
                self._model.predict([(query, t) for t in texts])]
```

generator.py：`MIN_SCORE` 常量删除，`Generator.__init__(self, llm, min_score: float = 0.3)`，`usable_blocks` 用 `self._min_score`。既有 tests/test_generator.py 引用 `MIN_SCORE` 处改为构造参数写法（同任务内修改测试属预期——契约变更）。

api/main.py 组装：

```python
    reranker = None
    rerank_degraded = False
    if cfg.retrieval.rerank.enabled:
        try:
            import kbase.plugins.rerankers.bge_local  # noqa: F401
            reranker = registry.create("reranker", "bge-local",
                                       model=cfg.retrieval.rerank.model)
        except Exception as e:  # noqa: BLE001 —— 模型加载失败降级不重排
            rerank_degraded = True
            logging.getLogger(__name__).warning("重排模型加载失败，已降级: %s", e)
```

Retriever 传入 `reranker=reranker`；Generator 构造改为 `Generator(llm, min_score=cfg.retrieval.min_score_rerank if retriever.rerank_active else cfg.retrieval.min_score_dense)`；healthz 返回追加 `"reranker": "on" | "degraded" | "off"`。测试注入：`create_app(..., reranker=...)` 新增可选参数（None=按配置，测试可传 fake 或显式关闭用 sentinel `False`）。tests/test_api.py 的 _client 传 `reranker=False`（显式关闭，保持既有 SSE 用例的分数语义不变）。

- [ ] **Step 4: 运行 + 回归**：新 2 passed（external 1 deselected）；test_generator 改造后全过；全量 ~56 passed, 3 deselected
- [ ] **Step 5: external 验证**（下载 reranker 模型 ~1.1GB，HF 直连）：`pytest tests/test_reranker.py -m external -v` → 1 passed
- [ ] **Step 6: Commit**

```bash
git add kbase/plugins/base.py kbase/plugins/rerankers/ kbase/rag/generator.py kbase/api/main.py tests/test_reranker.py tests/test_generator.py tests/test_api.py
git commit -m "feat: Reranker 插槽（bge-reranker+加载失败降级+分模式拒答阈值）"
```

---

### Task A5: 上下文增强 Enricher 与 reindex 命令

**Files:**
- Modify: `kbase/plugins/base.py`（+Enricher Protocol）、`kbase/ingest/pipeline.py`（kb config 读取+enrich 环节+enrich_context 持久化）、`kbase/config.py`（+EnrichConfig）
- Create: `kbase/plugins/enrichers/__init__.py`（空）、`kbase/plugins/enrichers/contextual.py`、`kbase/reindex.py`
- Test: `tests/test_enricher.py`、`tests/test_reindex.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_enricher.py
import json

from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import Chunk, KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.enrichers.contextual import ContextualEnricher
from kbase.plugins.vectorstores.chroma_store import ChromaStore

MD = "# 补贴办法\n## 第一章\n满两年可申领。\n"


class FakeLLM:
    async def complete(self, messages, **params):
        return "该片段属于补贴办法申领条件部分"


def test_enricher_fills_context():
    from kbase.plugins.base import ChunkData
    leaves = [ChunkData(id="c1", text="满两年可申领。", heading_path="h", parent_id="p")]
    enricher = ContextualEnricher(llm=FakeLLM())
    out = enricher.enrich("补贴办法.md", MD, leaves)
    assert out[0].meta["enrich_context"] == "该片段属于补贴办法申领条件部分"


def test_pipeline_enrich_persists_and_embeds(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库",
                            config=json.dumps({"enrich": {"enabled": True}})))
        s.commit()
    calls = []

    class SpyEmbedder:
        dimension = 8
        def embed(self, texts):
            calls.extend(texts)
            return fake_embedder.embed(texts)

    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=200, chunk_overlap=0),
                              SpyEmbedder(), ChromaStore(persist_dir=str(tmp_path / "c")),
                              tmp_path / "f", enricher=ContextualEnricher(llm=FakeLLM()))
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    doc_id = pipeline.ingest_file("kb1", f, "a.md")
    with factory() as s:
        leaf = s.query(Chunk).filter_by(doc_id=doc_id, is_leaf=True).first()
        assert leaf.enrich_context == "该片段属于补贴办法申领条件部分"
    assert any(t.startswith("该片段属于") for t in calls)   # 增强文本参与向量化


def test_enrich_failure_falls_back(tmp_path, fake_embedder):
    class BrokenLLM:
        async def complete(self, messages, **params):
            raise RuntimeError("api down")

    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库",
                            config=json.dumps({"enrich": {"enabled": True}})))
        s.commit()
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=200, chunk_overlap=0),
                              fake_embedder, ChromaStore(persist_dir=str(tmp_path / "c")),
                              tmp_path / "f", enricher=ContextualEnricher(llm=BrokenLLM()))
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    doc_id = pipeline.ingest_file("kb1", f, "a.md")
    with factory() as s:
        from kbase.models import Document
        assert s.get(Document, doc_id).status == "ready"    # 失败回退不阻塞
```

```python
# tests/test_reindex.py
from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.reindex import reindex_kb

MD = "# 办法\n## 一章\n新兵办发〔2014〕76号相关内容。\n"


def test_reindex_backfills_fts(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "c"))
    # 摄取时不带 keyword_index（模拟 M1 存量库）
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=200, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "f")
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "a.md")

    kw = KeywordIndex(factory)
    assert kw.search("kb1", "76号", top_k=3) == []          # 存量库无 FTS
    n = reindex_kb(factory, kw, fake_embedder, store, kb_id="kb1")
    assert n > 0
    assert kw.search("kb1", "76号", top_k=3)                 # 回填后命中
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

base.py 追加：

```python
@runtime_checkable
class Enricher(Protocol):
    def enrich(self, doc_name: str, markdown: str,
               leaves: list[ChunkData]) -> list[ChunkData]: ...
```

`contextual.py`：

```python
# kbase/plugins/enrichers/contextual.py
"""上下文增强：LLM 为每个叶子块生成一句全文定位说明，存入 meta["enrich_context"]。
单块失败静默跳过（该块回退为无增强），不向外抛异常。"""
import asyncio

from kbase.plugins.base import ChunkData
from kbase.plugins.registry import registry

_PROMPT = (
    "以下是文档《{doc_name}》的全文（可能截断）：\n{doc_head}\n\n"
    "请用一句话（30字内）说明下面这个片段在全文中的位置与主题，直接输出该句，"
    "不要任何前缀：\n{chunk}"
)


@registry.register("enricher", "contextual")
class ContextualEnricher:
    def __init__(self, llm, max_doc_chars: int = 6000, concurrency: int = 4):
        self._llm = llm
        self._max_doc = max_doc_chars
        self._concurrency = concurrency

    def enrich(self, doc_name, markdown, leaves):
        return asyncio.run(self._enrich_async(doc_name, markdown, leaves))

    async def _enrich_async(self, doc_name, markdown, leaves):
        sem = asyncio.Semaphore(self._concurrency)
        doc_head = markdown[: self._max_doc]

        async def one(leaf: ChunkData):
            async with sem:
                try:
                    ctx = await self._llm.complete([{
                        "role": "user",
                        "content": _PROMPT.format(doc_name=doc_name,
                                                  doc_head=doc_head,
                                                  chunk=leaf.text)}])
                    if ctx.strip():
                        leaf.meta["enrich_context"] = ctx.strip()
                except Exception:  # noqa: BLE001 —— 单块回退
                    pass

        await asyncio.gather(*(one(leaf) for leaf in leaves))
        return leaves
```

pipeline.py 改造：
- `__init__` 增加 `enricher=None`
- `_process` 读取 kb config（`KnowledgeBase.config` JSON），若 `enrich.enabled` 且 self._enricher 存在则调用 `leaves = self._enricher.enrich(name, markdown, leaves)`
- 向量化文本改为 `f"{c.meta.get('enrich_context', '')}\n{c.heading_path}\n{c.text}".lstrip()`
- Chunk 持久化时写 `enrich_context=c.meta.get("enrich_context")`
- kb config 的 `chunk_size/chunk_overlap` 若存在则用 kb 级 StructureChunker 覆盖默认（在 _process 内按需新建 chunker）

config.py 追加 `EnrichConfig(provider: str = "qwen-turbo-provider-name"...)`——实际字段：`class EnrichConfig(BaseModel): provider: str | None = None`（None=用 active provider）挂在 AppConfig（`enrich: EnrichConfig`），api 组装处按它创建 enricher 的 LLM。

`kbase/reindex.py`：

```python
"""重建索引：基于 SQLite 存量 chunk（不重新解析原始文件）回填 FTS 与向量。
用法：python -m kbase.reindex --kb <id> [--config config/kbase.yaml]"""
import argparse

from kbase.models import Chunk


def reindex_kb(session_factory, keyword_index, embedder, store, kb_id: str) -> int:
    with session_factory() as s:
        leaves = s.query(Chunk).filter_by(kb_id=kb_id, is_leaf=True).all()
    if not leaves:
        return 0
    keyword_index.delete_kb(kb_id)
    keyword_index.index(kb_id, [(c.id, c.doc_id, f"{c.heading_path}\n{c.text}")
                                for c in leaves])
    texts = [f"{(c.enrich_context or '')}\n{c.heading_path}\n{c.text}".lstrip()
             for c in leaves]
    vectors = embedder.embed(texts)
    store.upsert(kb_id, ids=[c.id for c in leaves], vectors=vectors,
                 metas=[{"doc_id": c.doc_id, "parent_id": c.parent_id}
                        for c in leaves])
    return len(leaves)
```

（KeywordIndex 补一个 `delete_kb(kb_id)` 方法：`DELETE FROM chunks_fts WHERE kb_id=:k`。）`__main__` 入口按 config 组装真实组件（import bge_local 等）后调 `reindex_kb` 并打印结果。

- [ ] **Step 4: 运行 + 回归**：新 4 passed；全量 ~60 passed, 3 deselected
- [ ] **Step 5: Commit**

```bash
git add kbase/plugins/base.py kbase/plugins/enrichers/ kbase/ingest/pipeline.py kbase/config.py kbase/reindex.py kbase/index/keyword.py tests/test_enricher.py tests/test_reindex.py
git commit -m "feat: 上下文增强 Enricher（kb 级开关+失败回退）与 reindex 命令"
```

---

### Task A6: OCR 路由与 pending_ocr

**Files:**
- Modify: `kbase/plugins/base.py`（+OCRBackend/OCRResult/OCRUnavailable）、`kbase/ingest/pipeline.py`（探测+路由+pending_ocr）、`kbase/config.py`（+OCRConfig）、`kbase/api/main.py`（重试端点）
- Create: `kbase/plugins/ocr/__init__.py`（空）、`kbase/plugins/ocr/monkey_http.py`
- Test: `tests/test_ocr_routing.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_ocr_routing.py
from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline, pdf_has_text_layer
from kbase.models import Document, KnowledgeBase
from kbase.plugins.base import OCRResult, OCRUnavailable
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore


def _scanned_pdf(path):
    """生成无文本层 PDF（纯图片页）。"""
    from PIL import Image
    img = Image.new("RGB", (600, 800), "white")
    img.save(path, "PDF")


def _text_pdf(path):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=600, height=800)
    with open(path, "wb") as f:
        w.write(f)
    # 空白页无文本 → 用 fpdf 不引入新依赖，改用 markitdown 可读的真实文本 PDF 不易构造；
    # 文本层探测的正例直接用 .md 文件路径旁路（探测只对 .pdf 生效），此函数不需要。


class FakeOCR:
    def __init__(self, result=None, fail=False):
        self._result = result or OCRResult(markdown="# 扫描件\n识别出的内容。", confidence=0.9)
        self._fail = fail

    def to_markdown(self, path):
        if self._fail:
            raise OCRUnavailable("service down")
        return self._result


def _pipeline(tmp_path, fake_embedder, ocr):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    return factory, IngestPipeline(
        factory, StructureChunker(chunk_size=200, chunk_overlap=0),
        fake_embedder, ChromaStore(persist_dir=str(tmp_path / "c")),
        tmp_path / "f", ocr_backend=ocr)


def test_pdf_text_layer_detection(tmp_path):
    scanned = tmp_path / "s.pdf"
    _scanned_pdf(scanned)
    assert pdf_has_text_layer(scanned) is False


def test_scanned_pdf_routes_to_ocr(tmp_path, fake_embedder):
    factory, pipeline = _pipeline(tmp_path, fake_embedder, FakeOCR())
    f = tmp_path / "scan.pdf"
    _scanned_pdf(f)
    doc_id = pipeline.ingest_file("kb1", f, "scan.pdf")
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "ready"
        assert doc.ocr_confidence == 0.9


def test_image_routes_to_ocr(tmp_path, fake_embedder):
    from PIL import Image
    factory, pipeline = _pipeline(tmp_path, fake_embedder, FakeOCR())
    f = tmp_path / "pic.png"
    Image.new("RGB", (100, 100), "white").save(f)
    doc_id = pipeline.ingest_file("kb1", f, "pic.png")
    with factory() as s:
        assert s.get(Document, doc_id).status == "ready"


def test_ocr_unavailable_sets_pending(tmp_path, fake_embedder):
    factory, pipeline = _pipeline(tmp_path, fake_embedder, FakeOCR(fail=True))
    f = tmp_path / "scan.pdf"
    _scanned_pdf(f)
    doc_id = pipeline.ingest_file("kb1", f, "scan.pdf")
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "pending_ocr"        # 非 failed


def test_no_ocr_backend_scanned_fails_with_message(tmp_path, fake_embedder):
    factory, pipeline = _pipeline(tmp_path, fake_embedder, None)
    f = tmp_path / "scan.pdf"
    _scanned_pdf(f)
    doc_id = pipeline.ingest_file("kb1", f, "scan.pdf")
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "failed"
        assert "OCR" in doc.error
```

- [ ] **Step 2: 运行确认失败**（Pillow 已随 chromadb/其他依赖存在则直接用；若缺失，`pip install pillow` 并加入 dev extra）

- [ ] **Step 3: 实现**

base.py 追加：

```python
@dataclass
class OCRResult:
    markdown: str
    confidence: float = 1.0


class OCRUnavailable(RuntimeError):
    """OCR 服务不可达/超时——文档应转 pending_ocr 而非 failed。"""


@runtime_checkable
class OCRBackend(Protocol):
    def to_markdown(self, path) -> OCRResult: ...
```

pipeline.py：

```python
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def pdf_has_text_layer(path, sample_pages: int = 3, min_chars_per_page: int = 50) -> bool:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages = reader.pages[:sample_pages]
    if not pages:
        return False
    chars = sum(len((p.extract_text() or "").strip()) for p in pages)
    return chars / len(pages) >= min_chars_per_page
```

`_process` 解析段改为路由：

```python
        suffix = Path(path).suffix.lower()
        needs_ocr = suffix in _IMAGE_EXTS or (
            suffix == ".pdf" and not pdf_has_text_layer(path))
        if needs_ocr:
            if self._ocr is None:
                raise ValueError("扫描件/图片需要 OCR，当前未配置 OCR 后端")
            result = self._ocr.to_markdown(path)      # OCRUnavailable 向上抛
            markdown = result.markdown
            ocr_confidence = result.confidence
        else:
            from markitdown import MarkItDown
            markdown = MarkItDown(enable_plugins=False).convert(str(path)).text_content
            ocr_confidence = None
```

`ingest_file` 的异常处理拆两层：`except OCRUnavailable: self._set_status(doc_id, "pending_ocr")`；其余仍 failed。`_set_status` 支持写 `ocr_confidence`。**重试**：`ingest_file` 需支持对既有 doc 重跑——新增 `retry_document(doc_id)`：读 Document 行，找到 uploads 原始文件（Document 新增 `source_path` 列？不加列——上传文件名含 uuid 无法反查。决策：Document 增加 `source_path TEXT` 列，A1 迁移清单补一行 `("documents", "source_path", "TEXT")`，pipeline 创建 doc 时写入，重试据此重跑 `_process`）。

`monkey_http.py`：

```python
# kbase/plugins/ocr/monkey_http.py
"""MonkeyOCR HTTP 适配器。先阅读 D:\Claude Code\MonkeyOCR 的 api/ 与 运行手册.md
确认真实请求格式（multipart 字段名/返回 JSON 结构），按实际调整 _parse_response。"""
import httpx

from kbase.plugins.base import OCRResult, OCRUnavailable
from kbase.plugins.registry import registry


@registry.register("ocr", "monkey-http")
class MonkeyOCRBackend:
    def __init__(self, endpoint: str = "http://localhost:7861", timeout: float = 300.0):
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout

    def to_markdown(self, path) -> OCRResult:
        try:
            with open(path, "rb") as f:
                resp = httpx.post(f"{self._endpoint}/parse",
                                  files={"file": f}, timeout=self._timeout)
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise OCRUnavailable(f"OCR 服务不可达: {e}") from e
        except httpx.HTTPStatusError as e:
            raise OCRUnavailable(f"OCR 服务错误: {e.response.status_code}") from e
        data = resp.json()
        return OCRResult(markdown=data.get("markdown", ""),
                         confidence=float(data.get("confidence", 1.0)))
```

config.py 追加 `class OCRConfig(BaseModel): enabled: bool = False; backend: str = "monkey-http"; endpoint: str = "http://localhost:7861"`，AppConfig 挂 `ocr: OCRConfig`；api 组装处按配置创建并传入 pipeline。API 端点：`POST /api/documents/{doc_id}/retry`（单个）、`POST /api/kb/{kb_id}/retry-ocr`（批量 pending_ocr，BackgroundTasks 逐个 retry_document）。tests/test_api.py 补一条：上传假 OCR 场景下 pending_ocr 文档经 retry 转 ready（用可切换失败状态的 FakeOCR 注入 create_app 新参数 `ocr_backend=`）。

- [ ] **Step 4: 运行 + 回归**：新 5 passed + api 重试 1 passed；全量 ~66 passed, 3 deselected
- [ ] **Step 5: MonkeyOCR 真实服务（尽力而为，失败不阻塞任务）**

按 `D:\Claude Code\MonkeyOCR\运行手册.md` 在该目录建独立 venv、装 CPU 依赖（用 model_configs_cpu.yaml）、下载权重、起 api 服务；用一张程序生成的中文文字图片实测 `to_markdown`。**若环境安装失败（Windows CPU 依赖冲突等），记录失败原因到报告，保持 FakeOCR 测试绿即视为任务完成**——真实 OCR 服务列入已知限制，等 GPU 服务器。

- [ ] **Step 6: Commit**

```bash
git add kbase/plugins/base.py kbase/plugins/ocr/ kbase/ingest/pipeline.py kbase/config.py kbase/migrations.py kbase/models.py kbase/api/main.py tests/test_ocr_routing.py tests/test_api.py
git commit -m "feat: OCR 路由（文本层探测+MonkeyOCR 适配器+pending_ocr 状态与重试）"
```

---

### Task A7: 多轮会话

**Files:**
- Create: `kbase/conversations.py`
- Modify: `kbase/rag/generator.py`（history 支持）、`kbase/api/main.py`（会话端点）
- Test: `tests/test_conversations.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_conversations.py
import json

from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, MD, FakeLLM


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False)
    c = TestClient(app)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    return c, kb_id


def test_conversation_crud_and_title(tmp_path, fake_embedder):
    c, kb_id = _client(tmp_path, fake_embedder)
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    assert conv["title"] == "新会话"
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q}) as r:
        body = "".join(r.iter_text())
    assert "event: done" in body
    convs = c.get("/api/conversations", params={"kb_id": kb_id}).json()
    assert convs[0]["title"] == q[:20]                     # 标题=首问前20字
    msgs = c.get(f"/api/conversations/{conv['id']}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"]                              # 助手消息已落库
    assert json.loads(msgs[1]["citations"])                # 引用已落库


def test_multi_turn_history_in_prompt(tmp_path, fake_embedder):
    c, kb_id = _client(tmp_path, fake_embedder)
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q}) as r:
        "".join(r.iter_text())
    # 第二轮：FakeLLM 记录 last_messages，历史应包含第一轮问答
    fake = c.app.state.test_llm                            # 见实现注记
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q + "第二问"}) as r:
        "".join(r.iter_text())
    roles = [m["role"] for m in fake.last_messages]
    assert roles.count("user") >= 2                        # 历史 user + 当前 user
    assert roles[0] == "system"


def test_query_unknown_conversation_404(tmp_path, fake_embedder):
    c, _ = _client(tmp_path, fake_embedder)
    r = c.post("/api/conversations/nope/query", json={"question": "x"})
    assert r.status_code == 404
```

实现注记：`tests/test_api.py` 的 `FakeLLM` 增加 `self.last_messages = None` 记录（stream 开头赋值），`create_app` 把注入的 active llm 挂到 `app.state.test_llm`（仅测试注入路径，生产为 None）——避免测试用私有变量。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

generator.py：`answer_stream(self, question, blocks, history: list[dict] | None = None)`——`_build_messages` 在 system 之后、当前 user 之前插入 history（已是 `{"role","content"}` 列表）。

`kbase/conversations.py`：

```python
"""会话领域逻辑：CRUD 与多轮上下文组装。HTTP 编排在 api/main.py。"""
import json
import uuid
from datetime import datetime

from kbase.models import Conversation, Message

HISTORY_ROUNDS = 3


def create_conversation(sf, kb_id: str) -> dict:
    conv = Conversation(id=str(uuid.uuid4()), kb_id=kb_id)
    with sf() as s:
        s.add(conv)
        s.commit()
    return {"id": conv.id, "kb_id": conv.kb_id, "title": conv.title}


def list_conversations(sf, kb_id: str | None = None) -> list[dict]:
    with sf() as s:
        q = s.query(Conversation).order_by(Conversation.updated_at.desc())
        if kb_id:
            q = q.filter_by(kb_id=kb_id)
        return [{"id": c.id, "kb_id": c.kb_id, "title": c.title,
                 "updated_at": c.updated_at.isoformat()} for c in q.all()]


def list_messages(sf, conv_id: str) -> list[dict]:
    with sf() as s:
        msgs = (s.query(Message).filter_by(conv_id=conv_id)
                .order_by(Message.created_at, Message.id).all())
        return [{"id": m.id, "role": m.role, "content": m.content,
                 "citations": m.citations, "provider": m.provider} for m in msgs]


def build_history(sf, conv_id: str, rounds: int = HISTORY_ROUNDS) -> list[dict]:
    with sf() as s:
        msgs = (s.query(Message).filter_by(conv_id=conv_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(rounds * 2).all())
    return [{"role": m.role, "content": m.content} for m in reversed(msgs)]


def append_round(sf, conv_id: str, question: str, answer: str,
                 citations: list[dict], provider: str) -> None:
    with sf() as s:
        conv = s.get(Conversation, conv_id)
        if conv is None:
            return
        if not s.query(Message).filter_by(conv_id=conv_id).first():
            conv.title = question[:20]
        s.add(Message(id=str(uuid.uuid4()), conv_id=conv_id, role="user",
                      content=question))
        s.add(Message(id=str(uuid.uuid4()), conv_id=conv_id, role="assistant",
                      content=answer, provider=provider,
                      citations=json.dumps(citations, ensure_ascii=False)))
        conv.updated_at = datetime.utcnow()
        s.commit()
```

api/main.py 新端点：`POST /api/conversations` {kb_id}；`GET /api/conversations?kb_id=`；`GET /api/conversations/{id}/messages`；`POST /api/conversations/{id}/query`（体同 QueryBody）——流程：404 校验会话存在 → 检索（当前问题，run_in_threadpool）→ usable/citations → SSE events()：流式收集完整答案文本，`finally` 中调 `append_round`（流中断也保存已生成部分；拒答同样落库）。事件序列与既有 query 端点一致（citations→token*→done）。

- [ ] **Step 4: 运行 + 回归**：新 3 passed；全量 ~69 passed, 3 deselected
- [ ] **Step 5: Commit**

```bash
git add kbase/conversations.py kbase/rag/generator.py kbase/api/main.py tests/test_conversations.py tests/test_api.py
git commit -m "feat: 多轮会话（历史组装+标题生成+消息落库含中断保存）"
```

---

### Task A8: Provider 管理入库、设置 API、文档全文与删除

**Files:**
- Create: `kbase/providers_store.py`
- Modify: `kbase/api/main.py`
- Test: `tests/test_settings_api.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_settings_api.py
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, MD, FakeLLM


def _client(tmp_path, fake_embedder, llms=None):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms=llms or {"fake": FakeLLM()}, reranker=False)
    return TestClient(app)


def test_yaml_seeded_into_db_once(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    rows = c.get("/api/settings/providers").json()
    names = {r["name"] for r in rows["providers"]}
    assert "fake" in names and "fake2" in names            # CFG 里的两个
    assert rows["active"] == "fake"


def test_provider_crud(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/settings/providers", json={
        "name": "new-p", "base_url": "http://x/v1",
        "api_key_env": "NEW_KEY", "model": "m2", "max_concurrency": 2,
        "params": {"extra_body": {"enable_thinking": False}}})
    assert r.status_code == 200
    got = c.get("/api/settings/providers").json()["providers"]
    new = next(p for p in got if p["name"] == "new-p")
    assert new["params"]["extra_body"]["enable_thinking"] is False
    c.put("/api/settings/providers/new-p", json={"model": "m3"})
    got = c.get("/api/settings/providers").json()["providers"]
    assert next(p for p in got if p["name"] == "new-p")["model"] == "m3"
    assert c.delete("/api/settings/providers/new-p").status_code == 200
    c.put("/api/settings/active-provider", json={"name": "fake2"})
    assert c.get("/api/settings/providers").json()["active"] == "fake2"


def test_provider_connectivity_test_endpoint(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/settings/providers/fake/test").json()
    assert r["ok"] is True and "latency_ms" in r


def test_document_fulltext_and_delete(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    full = c.get(f"/api/documents/{doc['id']}/content").json()
    assert "申领条件" in full["markdown"]
    assert c.delete(f"/api/kb/{kb_id}/documents/{doc['id']}").status_code == 200
    assert c.get(f"/api/kb/{kb_id}/documents").json() == []
    assert c.get(f"/api/documents/{doc['id']}/content").status_code == 404
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

`kbase/providers_store.py`：ProviderRow CRUD + `seed_from_config(sf, cfg)`（providers 表空时导入 YAML providers 与 active 到 app_settings["active_provider"]）+ `get_provider_dict(sf, name)`（含 params JSON 解码）+ `get_active(sf)`。api/main.py：启动时 seed；`get_llm` 改为从 DB 读 provider 定义（缓存按 name，PUT/DELETE 时使相应缓存失效）；设置端点组：GET/POST/PUT/DELETE `/api/settings/providers[/{name}]`、PUT `/api/settings/active-provider`、POST `/api/settings/providers/{name}/test`（构造该 provider 并 `complete` 一个 "回复：好"，测量延迟；异常返回 `{"ok": False, "error": ...}`，密钥缺失信息含环境变量名）。`/api/providers`（旧端点）改为读 DB，返回结构不变（Plan B 前旧前端仍用它）。

文档全文：`GET /api/documents/{doc_id}/content` 读 `data/files/{doc_id}/content.md`（不存在或 doc 不存在 → 404），返回 `{"doc_id", "filename", "markdown"}`。

删除：`DELETE /api/kb/{kb_id}/documents/{doc_id}` → `store.delete(kb_id, doc_id)` + `keyword_index.delete_doc(doc_id)` + 删 Chunk 行 + 删 Document 行 + 删 files 目录（shutil.rmtree, ignore_errors）。

- [ ] **Step 4: 运行 + 回归**：新 4 passed；全量 ~73 passed, 3 deselected
- [ ] **Step 5: Commit**

```bash
git add kbase/providers_store.py kbase/api/main.py tests/test_settings_api.py
git commit -m "feat: Provider 入库管理+连通性测试+文档全文/删除接口"
```

---

### Task A9: 检索调试端点、评测档位对比与真实验收

**Files:**
- Modify: `kbase/api/main.py`（search 端点）、`eval/run_eval.py`（--tiers）
- Test: `tests/test_search_debug.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_search_debug.py
from tests.test_settings_api import _client
from tests.test_api import MD


def _kb_with_doc(c):
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    return kb_id


def test_search_plain(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = _kb_with_doc(c)
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    r = c.post(f"/api/kb/{kb_id}/search", json={"query": q, "top_k": 3}).json()
    assert r["blocks"] and "连续工作满两年" in r["blocks"][0]["text"]
    assert "trace" not in r


def test_search_debug_trace(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = _kb_with_doc(c)
    r = c.post(f"/api/kb/{kb_id}/search",
               json={"query": "住房补贴", "top_k": 3, "debug": True}).json()
    assert set(r["trace"]) >= {"dense", "keyword", "fused"}
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

api/main.py：`POST /api/kb/{kb_id}/search` body `{query, top_k=5, debug=False}`——debug=False 返回 `{"blocks": [...]}`（ContextBlock 字典化）；debug=True 用 `retriever.retrieve(..., debug=True)` 返回 `{"blocks": [...], "trace": {...}}`（run_in_threadpool）。

eval/run_eval.py 增加 `--tiers` 开关：构造三个 Retriever 变体——dense（无 kw 无 rerank）、hybrid（kw 无 rerank）、hybrid_rerank（kw + reranker，reranker 加载失败则该档跳过并注明）——只做检索命中评测（不生成，快），输出对比表：

```markdown
| 档位 | 命中率 |
|---|---|
| 纯向量 | x/N |
| 混合 | y/N |
| 混合+重排 | z/N |
```

写入 `--out`。`--providers` 在 tiers 模式下不需要（互斥；tiers 时忽略并提示）。

- [ ] **Step 4: 运行 + 回归**：新 2 passed；全量 ~75 passed, 3 deselected

- [ ] **Step 5: 真实验收（rehearsal KB）**

1. 加载 .env；对彩排库跑 `python -m kbase.reindex --kb <兵团政策演示库id>`（回填 FTS；kb id 用一段 python 从 data/kbase.sqlite 查）
2. `python eval/run_eval.py --tiers --kb <id> --questions eval/questions.jsonl --out <scratchpad>/tiers.md`——注意 questions.jsonl 当前 3 题针对示例文档，彩排库无对应文档时改用 T15 彩排的 5 题（写入 scratchpad 的 rehearsal_questions.jsonl，题目按彩排报告里的 5 问重建）
3. 报告表格贴入任务报告；验收线：混合+重排 ≥ 混合 ≥ 纯向量（允许并列）
4. 用真实 provider 对彩排库走一次 `/api/kb/{id}/search?debug=true`（uvicorn 起服务或 TestClient 均可），确认 trace 各阶段非空

- [ ] **Step 6: Commit**

```bash
git add kbase/api/main.py eval/run_eval.py tests/test_search_debug.py
git commit -m "feat: 检索调试端点与评测档位对比模式"
```

---

## 任务依赖

```
A1 → A2 → A3 → A4 → A5 → A6 → A7 → A8 → A9（严格顺序，共享 pipeline/api 文件）
```

## Plan B 衔接

Plan A 验收后另写 `2026-07-XX-kbase-m2b-frontend.md`（Vue3+shadcn-vue 四页），接口契约以 Plan A 实际落地的端点为准。
