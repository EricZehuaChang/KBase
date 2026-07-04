# KBase Demo 里程碑（M1）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-07-08 技术测试前交付可演示的 lite 模式 RAG 系统：批量导入脱敏文档、流式问答带引用、运行时切换不同规模模型、评测集对比报告。

**Architecture:** FastAPI 异步单体 + 插槽化插件层（Embedder/VectorStore/LLMProvider/Chunker 抽象接口 + 注册表 + YAML 配置选择实现）。lite 模式：SQLite 存元数据与全部 chunk（含父子关系），Chroma 嵌入式存叶子块向量，bge-m3 进程内向量化，LLM 走 OpenAI 兼容在线 API。检索用 small-to-big：命中叶子块后从 SQLite 取父块/邻块组装上下文。

**Tech Stack:** Python 3.11+、FastAPI、SQLAlchemy 2.0(SQLite)、chromadb、sentence-transformers(bge-m3)、markitdown、langchain-text-splitters、openai(客户端，通吃所有 OpenAI 兼容端点)、sse-starlette、pytest。前端为零构建静态页（原生 JS + SSE），Demo 后按 spec 换 Vue3。

**Spec:** `docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md`（本计划实现其第 10 节 Demo 必须项）

**约定（所有任务适用）：**
- 工作目录：`D:\Claude Code\RAG`（仓库根）
- 每个任务完成即 commit，消息用 conventional commits
- 测试命令统一 `python -m pytest`，需要网络/模型的测试标记 `@pytest.mark.external`，CI 默认跳过：`python -m pytest -m "not external"`
- 国内网络下载 HF 模型需设 `HF_ENDPOINT=https://hf-mirror.com`

---

## 文件结构（M1 全量）

```
RAG/
├── pyproject.toml               # 项目定义与依赖
├── config/kbase.yaml            # 运行配置（插件选择、LLM providers）
├── kbase/
│   ├── __init__.py
│   ├── config.py                # YAML → pydantic 配置对象
│   ├── db.py                    # SQLAlchemy engine/session
│   ├── models.py                # ORM: KnowledgeBase/Document/Chunk（摄取状态在 Document.status）
│   ├── plugins/
│   │   ├── __init__.py
│   │   ├── base.py              # 四个 Protocol + 数据类型
│   │   ├── registry.py          # 注册表 + 按配置实例化
│   │   ├── chunkers/structure.py    # 结构分块+父子块（核心算法）
│   │   ├── embedders/bge_local.py   # sentence-transformers bge-m3
│   │   ├── vectorstores/chroma_store.py
│   │   └── llm/openai_compat.py     # 流式+非流式，多 provider
│   ├── ingest/pipeline.py       # markitdown→分块→向量化→入库，任务状态
│   ├── rag/retriever.py         # 检索 + small-to-big 上下文组装
│   ├── rag/generator.py         # prompt 组装 + 流式生成 + 引用
│   └── api/main.py              # FastAPI 全部路由 + 静态托管
├── web/                         # 零构建前端
│   ├── index.html
│   └── app.js
├── eval/
│   ├── questions.jsonl          # 评测集（人工准备）
│   └── run_eval.py              # 命中率/引用报告，--provider 对比
└── tests/
    ├── conftest.py              # 共享 fixture（内存库、FakeEmbedder 等）
    ├── test_config.py
    ├── test_registry.py
    ├── test_chunker.py
    ├── test_models.py
    ├── test_embedder.py
    ├── test_chroma_store.py
    ├── test_llm_provider.py
    ├── test_ingest.py
    ├── test_retriever.py
    ├── test_generator.py
    └── test_api.py
```

职责边界：`plugins/` 下每个文件只实现一个契约；内核（ingest/rag）只 import `plugins/base.py` 的抽象；`api/main.py` 只做 HTTP 编排不含业务逻辑。

---

### Task 1: 项目脚手架与测试基建

**Files:**
- Create: `pyproject.toml`
- Create: `kbase/__init__.py`, `kbase/plugins/__init__.py`, `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: 写 pyproject.toml**

```toml
[project]
name = "kbase"
version = "0.1.0"
description = "KBase 私有化知识库系统"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "pydantic>=2.7",
    "pyyaml>=6.0",
    "sqlalchemy>=2.0",
    "chromadb>=0.5",
    "markitdown[docx,pptx,xlsx,pdf]>=0.1",
    "langchain-text-splitters>=0.3",
    "openai>=1.40",
    "sse-starlette>=2.1",
    "python-multipart>=0.0.9",
]

[project.optional-dependencies]
local-embed = ["sentence-transformers>=3.0"]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "httpx>=0.27"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = ["external: 需要网络或大模型下载，默认跳过"]
addopts = "-m 'not external'"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["kbase"]
```

- [ ] **Step 2: 建包骨架与 conftest**

创建空文件 `kbase/__init__.py`、`kbase/plugins/__init__.py`、`tests/__init__.py`。

`tests/conftest.py`：

```python
import pytest


class FakeEmbedder:
    """确定性假向量器：不下载模型，向量由文本 hash 生成，维度 8。"""
    dimension = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            h = abs(hash(t))
            out.append([((h >> (i * 4)) % 100) / 100.0 for i in range(8)])
        return out


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
```

- [ ] **Step 3: 安装依赖并验证 pytest 能跑**

Run: `python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"`（PowerShell 下）
Run: `.venv\Scripts\python -m pytest --collect-only`
Expected: `no tests ran`（无报错即可）

注意：`sentence-transformers` 属 `local-embed` extra，此任务不装（Task 6 再装），避免脚手架阶段就拉 2GB 依赖。

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml kbase/ tests/
git commit -m "chore: kbase 项目脚手架与测试基建"
```

---

### Task 2: 配置加载（YAML → pydantic）

**Files:**
- Create: `kbase/config.py`
- Create: `config/kbase.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py
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
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'kbase.config'`

- [ ] **Step 3: 实现 kbase/config.py**

```python
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class EmbedderConfig(BaseModel):
    name: str = "bge-local"
    model: str = "BAAI/bge-m3"


class VectorStoreConfig(BaseModel):
    name: str = "chroma"


class ChunkerConfig(BaseModel):
    name: str = "structure"
    chunk_size: int = 512
    chunk_overlap: int = 64


class ProviderConfig(BaseModel):
    name: str
    base_url: str
    api_key_env: str          # 环境变量名，密钥不进配置文件
    model: str
    max_concurrency: int = 4


class LLMConfig(BaseModel):
    active: str
    providers: list[ProviderConfig]


class AppConfig(BaseModel):
    data_dir: Path = Path("./data")
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    vectorstore: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    llm: LLMConfig

    def get_provider(self, name: str) -> ProviderConfig:
        for p in self.llm.providers:
            if p.name == name:
                return p
        raise KeyError(f"LLM provider 未配置: {name}")


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: 2 passed

- [ ] **Step 5: 写正式配置文件 config/kbase.yaml**

内容与测试中的 YAML 相同（Step 1 里那份，`data_dir: ./data`），另加注释头：

```yaml
# KBase 运行配置。密钥一律走环境变量（api_key_env 指定变量名），本文件可入库。
```

- [ ] **Step 6: Commit**

```bash
git add kbase/config.py config/kbase.yaml tests/test_config.py
git commit -m "feat: YAML 配置加载与 LLM 多 provider 定义"
```

---

### Task 3: 插件契约与注册表

**Files:**
- Create: `kbase/plugins/base.py`
- Create: `kbase/plugins/registry.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_registry.py
import pytest
from kbase.plugins.registry import PluginRegistry


def test_register_and_create():
    reg = PluginRegistry()

    @reg.register("embedder", "fake")
    class Fake:
        def __init__(self, dim: int = 8):
            self.dimension = dim

        def embed(self, texts):
            return [[0.0] * self.dimension for _ in texts]

    inst = reg.create("embedder", "fake", dim=16)
    assert inst.dimension == 16


def test_unknown_plugin_raises():
    reg = PluginRegistry()
    with pytest.raises(KeyError, match="未注册"):
        reg.create("embedder", "nope")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_registry.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: 实现 base.py（契约与数据类型）**

```python
# kbase/plugins/base.py
"""插件契约。内核代码只允许 import 本文件，不得 import 具体实现。"""
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass
class ChunkData:
    """分块结果。parent_id 为 None 表示父块（章节级），否则为叶子块。"""
    id: str
    text: str
    heading_path: str            # 如 "文件名 > 第三章 > 第十二条"
    parent_id: str | None = None
    prev_id: str | None = None
    next_id: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Hit:
    chunk_id: str
    score: float
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Embedder(Protocol):
    dimension: int
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, collection: str, ids: list[str], vectors: list[list[float]],
               metas: list[dict]) -> None: ...
    def search(self, collection: str, vector: list[float], top_k: int,
               filters: dict | None = None) -> list[Hit]: ...
    def delete(self, collection: str, doc_id: str) -> None: ...


@runtime_checkable
class LLMProvider(Protocol):
    def stream(self, messages: list[dict], **params) -> AsyncIterator[str]: ...
    async def complete(self, messages: list[dict], **params) -> str: ...


@runtime_checkable
class Chunker(Protocol):
    def chunk(self, markdown: str, doc_name: str) -> list[ChunkData]: ...
```

- [ ] **Step 4: 实现 registry.py**

```python
# kbase/plugins/registry.py
class PluginRegistry:
    def __init__(self):
        self._plugins: dict[tuple[str, str], type] = {}

    def register(self, kind: str, name: str):
        def deco(cls):
            self._plugins[(kind, name)] = cls
            return cls
        return deco

    def create(self, kind: str, name: str, **kwargs):
        key = (kind, name)
        if key not in self._plugins:
            known = [n for k, n in self._plugins if k == kind]
            raise KeyError(f"插件未注册: {kind}/{name}，已注册: {known}")
        return self._plugins[key](**kwargs)


registry = PluginRegistry()   # 全局单例，实现文件在模块加载时向它注册
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_registry.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add kbase/plugins/base.py kbase/plugins/registry.py tests/test_registry.py
git commit -m "feat: 插件契约(Protocol)与注册表"
```

---

### Task 4: 结构分块器（父子块，核心算法）

**Files:**
- Create: `kbase/plugins/chunkers/__init__.py`（空）、`kbase/plugins/chunkers/structure.py`
- Test: `tests/test_chunker.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_chunker.py
from kbase.plugins.chunkers.structure import StructureChunker

DOC = """# 某某政策
## 第一章 总则
第一条 为了推进工作，制定本办法。
第二条 本办法适用于全体单位。
## 第二章 保障措施
第三条 各单位应当保障经费。
前款所述经费由财政承担。
"""


def test_parent_and_leaf_chunks():
    chunks = StructureChunker(chunk_size=500, chunk_overlap=0).chunk(DOC, "test.md")
    parents = [c for c in chunks if c.parent_id is None]
    leaves = [c for c in chunks if c.parent_id is not None]
    assert len(parents) == 2                      # 两章各一个父块
    assert all(l.parent_id in {p.id for p in parents} for l in leaves)


def test_heading_path():
    chunks = StructureChunker(chunk_size=500, chunk_overlap=0).chunk(DOC, "test.md")
    leaf = next(c for c in chunks if c.parent_id and "经费" in c.text)
    assert leaf.heading_path == "test.md > 某某政策 > 第二章 保障措施"


def test_prev_next_chain_within_parent():
    # 强制小 chunk_size 让章节切成多个叶子块
    chunks = StructureChunker(chunk_size=30, chunk_overlap=0).chunk(DOC, "test.md")
    parents = {c.id: c for c in chunks if c.parent_id is None}
    first_parent = next(p for p in parents.values() if "第一章" in p.heading_path)
    siblings = [c for c in chunks if c.parent_id == first_parent.id]
    assert len(siblings) >= 2
    assert siblings[0].prev_id is None
    assert siblings[0].next_id == siblings[1].id
    assert siblings[1].prev_id == siblings[0].id
    assert siblings[-1].next_id is None


def test_parent_holds_full_section_text():
    chunks = StructureChunker(chunk_size=30, chunk_overlap=0).chunk(DOC, "test.md")
    parent = next(c for c in chunks if c.parent_id is None and "第二章" in c.heading_path)
    assert "第三条" in parent.text and "前款所述" in parent.text
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_chunker.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: 实现 structure.py**

```python
# kbase/plugins/chunkers/structure.py
"""结构分块：沿 Markdown 标题切父块（章节），父块内按长度切叶子块。
chunk_size 按字符计（中文场景下与 token 数近似 1:1）。"""
import uuid

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from kbase.plugins.base import ChunkData
from kbase.plugins.registry import registry

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]


@registry.register("chunker", "structure")
class StructureChunker:
    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        self._header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=_HEADERS, strip_headers=True
        )
        self._text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", "；", " ", ""],
        )

    def chunk(self, markdown: str, doc_name: str) -> list[ChunkData]:
        out: list[ChunkData] = []
        for section in self._header_splitter.split_text(markdown):
            titles = [section.metadata[key] for key in ("h1", "h2", "h3", "h4")
                      if key in section.metadata]
            heading_path = " > ".join([doc_name, *titles])
            parent = ChunkData(id=str(uuid.uuid4()), text=section.page_content,
                               heading_path=heading_path)
            out.append(parent)
            pieces = self._text_splitter.split_text(section.page_content)
            leaves = [ChunkData(id=str(uuid.uuid4()), text=p,
                                heading_path=heading_path, parent_id=parent.id)
                      for p in pieces]
            for i, leaf in enumerate(leaves):
                leaf.prev_id = leaves[i - 1].id if i > 0 else None
                leaf.next_id = leaves[i + 1].id if i < len(leaves) - 1 else None
            out.extend(leaves)
        return out
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_chunker.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add kbase/plugins/chunkers/ tests/test_chunker.py
git commit -m "feat: 结构分块器（标题父块+长度叶子块+prev/next 链）"
```

---

### Task 5: 元数据库（SQLAlchemy + SQLite）

**Files:**
- Create: `kbase/db.py`、`kbase/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_models.py
from kbase.db import make_session_factory
from kbase.models import Chunk, Document, KnowledgeBase


def test_roundtrip(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        kb = KnowledgeBase(id="kb1", name="政策库")
        doc = Document(id="d1", kb_id="kb1", filename="a.docx",
                       content_hash="abc", status="ready")
        parent = Chunk(id="p1", doc_id="d1", kb_id="kb1", heading_path="a > 一章",
                       text="全文", is_leaf=False)
        leaf = Chunk(id="c1", doc_id="d1", kb_id="kb1", parent_id="p1",
                     heading_path="a > 一章", text="片段", is_leaf=True)
        s.add_all([kb, doc, parent, leaf])
        s.commit()
    with factory() as s:
        got = s.get(Chunk, "c1")
        assert got.parent_id == "p1"
        assert s.get(Document, "d1").status == "ready"


def test_duplicate_hash_lookup(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="x"))
        s.add(Document(id="d1", kb_id="kb1", filename="a.docx",
                       content_hash="same", status="ready"))
        s.commit()
        dup = s.query(Document).filter_by(kb_id="kb1", content_hash="same").first()
        assert dup is not None
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_models.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: 实现 db.py 与 models.py**

```python
# kbase/db.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kbase.models import Base


def make_session_factory(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
```

```python
# kbase/models.py
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    # pending -> parsing -> ready | failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    prev_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    next_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    heading_path: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    is_leaf: Mapped[bool] = mapped_column(Boolean, default=True)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_models.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add kbase/db.py kbase/models.py tests/test_models.py
git commit -m "feat: SQLite 元数据模型（知识库/文档/父子块）"
```

---

### Task 6: bge 本地向量器

**Files:**
- Create: `kbase/plugins/embedders/__init__.py`（空）、`kbase/plugins/embedders/bge_local.py`
- Test: `tests/test_embedder.py`

- [ ] **Step 1: 写测试（真实模型的用 external 标记，默认跳过）**

```python
# tests/test_embedder.py
import pytest


def test_module_importable_without_model():
    """import 本身不触发模型下载（sentence_transformers 延迟到实例化）。"""
    import kbase.plugins.embedders.bge_local  # noqa: F401


@pytest.mark.external
def test_bge_local_embed_shape():
    from kbase.plugins.embedders.bge_local import BgeLocalEmbedder
    e = BgeLocalEmbedder(model="BAAI/bge-m3")
    vecs = e.embed(["住房补贴的申领条件", "经费保障"])
    assert len(vecs) == 2
    assert len(vecs[0]) == e.dimension == 1024
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_embedder.py -v`
Expected: `test_module_importable_without_model` FAIL with ModuleNotFoundError（external 条 deselected）

- [ ] **Step 3: 实现 bge_local.py**

```python
# kbase/plugins/embedders/bge_local.py
from kbase.plugins.registry import registry


@registry.register("embedder", "bge-local")
class BgeLocalEmbedder:
    def __init__(self, model: str = "BAAI/bge-m3", device: str | None = None):
        # 延迟 import：未装 local-embed extra 时其他插件不受影响
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model, device=device)
        self.dimension = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True,
                                  batch_size=16).tolist()
```

- [ ] **Step 4: 安装 extra 并验证**

Run: `.venv\Scripts\pip install -e ".[local-embed]"`（首次约 2.2GB；国内先设 `$env:HF_ENDPOINT="https://hf-mirror.com"`）
Run: `.venv\Scripts\python -m pytest tests/test_embedder.py -v`
Expected: 1 passed, 1 deselected
Run: `.venv\Scripts\python -m pytest tests/test_embedder.py -m external -v`
Expected: 1 passed（首跑会下载模型）

- [ ] **Step 5: Commit**

```bash
git add kbase/plugins/embedders/ tests/test_embedder.py
git commit -m "feat: bge-m3 本地向量器插件"
```

---

### Task 7: Chroma 向量库适配器

**Files:**
- Create: `kbase/plugins/vectorstores/__init__.py`（空）、`kbase/plugins/vectorstores/chroma_store.py`
- Test: `tests/test_chroma_store.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_chroma_store.py
from kbase.plugins.vectorstores.chroma_store import ChromaStore


def _mk(tmp_path, fake_embedder):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    vecs = fake_embedder.embed(["甲", "乙", "丙"])
    store.upsert("kb1",
                 ids=["c1", "c2", "c3"],
                 vectors=vecs,
                 metas=[{"doc_id": "d1"}, {"doc_id": "d1"}, {"doc_id": "d2"}])
    return store, vecs


def test_search_returns_hits(tmp_path, fake_embedder):
    store, vecs = _mk(tmp_path, fake_embedder)
    hits = store.search("kb1", vecs[0], top_k=2)
    assert hits[0].chunk_id == "c1"          # 自身向量最相近
    assert hits[0].score >= hits[1].score


def test_filter_by_doc(tmp_path, fake_embedder):
    store, vecs = _mk(tmp_path, fake_embedder)
    hits = store.search("kb1", vecs[0], top_k=3, filters={"doc_id": "d2"})
    assert {h.chunk_id for h in hits} == {"c3"}


def test_delete_by_doc(tmp_path, fake_embedder):
    store, vecs = _mk(tmp_path, fake_embedder)
    store.delete("kb1", doc_id="d1")
    hits = store.search("kb1", vecs[0], top_k=3)
    assert {h.chunk_id for h in hits} == {"c3"}
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_chroma_store.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: 实现 chroma_store.py**

```python
# kbase/plugins/vectorstores/chroma_store.py
import chromadb

from kbase.plugins.base import Hit
from kbase.plugins.registry import registry


@registry.register("vectorstore", "chroma")
class ChromaStore:
    def __init__(self, persist_dir: str = "./data/chroma"):
        self._client = chromadb.PersistentClient(path=persist_dir)

    def _coll(self, collection: str):
        # cosine 距离，与 normalize 后的 bge 向量匹配
        return self._client.get_or_create_collection(
            collection, metadata={"hnsw:space": "cosine"})

    def upsert(self, collection, ids, vectors, metas):
        self._coll(collection).upsert(ids=ids, embeddings=vectors, metadatas=metas)

    def search(self, collection, vector, top_k, filters=None):
        res = self._coll(collection).query(
            query_embeddings=[vector], n_results=top_k,
            where=filters or None)
        hits = []
        for cid, dist, meta in zip(res["ids"][0], res["distances"][0],
                                   res["metadatas"][0]):
            hits.append(Hit(chunk_id=cid, score=1 - dist, meta=meta or {}))
        return hits

    def delete(self, collection, doc_id):
        self._coll(collection).delete(where={"doc_id": doc_id})
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_chroma_store.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add kbase/plugins/vectorstores/ tests/test_chroma_store.py
git commit -m "feat: Chroma 向量库适配器（cosine + doc 级过滤/删除）"
```

---

### Task 8: OpenAI 兼容 LLM Provider

**Files:**
- Create: `kbase/plugins/llm/__init__.py`（空）、`kbase/plugins/llm/openai_compat.py`
- Test: `tests/test_llm_provider.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_llm_provider.py
import pytest


def test_init_reads_env_key(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "sk-fake")
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(base_url="https://example.com/v1",
                             api_key_env="TEST_LLM_KEY",
                             model="test-model", max_concurrency=2)
    assert p.model == "test-model"


def test_init_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    with pytest.raises(RuntimeError, match="NO_SUCH_KEY"):
        OpenAICompatProvider(base_url="https://example.com/v1",
                             api_key_env="NO_SUCH_KEY", model="m")


@pytest.mark.external
async def test_real_stream():
    """需要 DASHSCOPE_API_KEY 环境变量，验证真实端点流式输出。"""
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY", model="qwen-turbo")
    text = "".join([t async for t in p.stream(
        [{"role": "user", "content": "回复两个字：你好"}])])
    assert len(text) > 0
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_llm_provider.py -v`
Expected: 2 FAIL with ModuleNotFoundError（external 条 deselected）

- [ ] **Step 3: 实现 openai_compat.py**

```python
# kbase/plugins/llm/openai_compat.py
import asyncio
import os
from typing import AsyncIterator

from kbase.plugins.registry import registry


@registry.register("llm", "openai-compat")
class OpenAICompatProvider:
    """一个实现通吃所有 OpenAI 兼容端点（DashScope/硅基流动/vLLM/DeepSeek）。"""

    def __init__(self, base_url: str, api_key_env: str, model: str,
                 max_concurrency: int = 4):
        from openai import AsyncOpenAI
        key = os.environ.get(api_key_env)
        if not key:
            raise RuntimeError(
                f"环境变量 {api_key_env} 未设置，无法初始化 LLM provider")
        self._client = AsyncOpenAI(base_url=base_url, api_key=key)
        self.model = model
        self._sem = asyncio.Semaphore(max_concurrency)

    async def stream(self, messages: list[dict], **params) -> AsyncIterator[str]:
        async with self._sem:
            resp = await self._client.chat.completions.create(
                model=self.model, messages=messages, stream=True, **params)
            async for chunk in resp:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

    async def complete(self, messages: list[dict], **params) -> str:
        async with self._sem:
            resp = await self._client.chat.completions.create(
                model=self.model, messages=messages, stream=False, **params)
            return resp.choices[0].message.content or ""
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_llm_provider.py -v`
Expected: 2 passed, 1 deselected

- [ ] **Step 5: Commit**

```bash
git add kbase/plugins/llm/ tests/test_llm_provider.py
git commit -m "feat: OpenAI 兼容 LLM provider（流式+并发信号量+密钥走环境变量）"
```

---

### Task 9: 摄取管道

**Files:**
- Create: `kbase/ingest/__init__.py`（空）、`kbase/ingest/pipeline.py`
- Test: `tests/test_ingest.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ingest.py
from pathlib import Path

from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import Chunk, Document, KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore

MD = """# 补贴办法
## 第一章 申领条件
连续工作满两年可申领住房补贴。
## 第二章 标准
每月补贴一千元。
"""


def _mk(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="测试库"))
        s.commit()
    pipeline = IngestPipeline(
        session_factory=factory,
        chunker=StructureChunker(chunk_size=200, chunk_overlap=0),
        embedder=fake_embedder,
        store=ChromaStore(persist_dir=str(tmp_path / "chroma")),
        files_dir=tmp_path / "files",
    )
    return factory, pipeline


def test_ingest_md_file(tmp_path, fake_embedder):
    factory, pipeline = _mk(tmp_path, fake_embedder)
    f = tmp_path / "补贴办法.md"
    f.write_text(MD, encoding="utf-8")

    doc_id = pipeline.ingest_file("kb1", f, original_name="补贴办法.md")

    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "ready"
        chunks = s.query(Chunk).filter_by(doc_id=doc_id).all()
        parents = [c for c in chunks if not c.is_leaf]
        leaves = [c for c in chunks if c.is_leaf]
        assert len(parents) == 2 and len(leaves) >= 2
    # markdown 中间产物已落盘
    assert (tmp_path / "files" / doc_id / "content.md").exists()


def test_ingest_dedup_by_hash(tmp_path, fake_embedder):
    factory, pipeline = _mk(tmp_path, fake_embedder)
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    d1 = pipeline.ingest_file("kb1", f, original_name="a.md")
    d2 = pipeline.ingest_file("kb1", f, original_name="a-重复.md")
    assert d1 == d2                      # 命中去重，返回已有文档


def test_ingest_failure_isolated(tmp_path, fake_embedder):
    factory, pipeline = _mk(tmp_path, fake_embedder)
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"\x00\x01not a real docx")
    doc_id = pipeline.ingest_file("kb1", bad, original_name="bad.docx")
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "failed"
        assert doc.error                 # 有失败原因，且没有抛异常
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_ingest.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: 实现 pipeline.py**

```python
# kbase/ingest/pipeline.py
"""摄取：文件 → markitdown → 标准 Markdown → 分块 → 叶子块向量化 → 入库。
单文件失败只标记该文档，不向外抛异常（批次隔离）。"""
import hashlib
import uuid
from pathlib import Path

from kbase.models import Chunk, Document
from kbase.plugins.base import Chunker, Embedder, VectorStore


class IngestPipeline:
    def __init__(self, session_factory, chunker: Chunker, embedder: Embedder,
                 store: VectorStore, files_dir: Path):
        self._sf = session_factory
        self._chunker = chunker
        self._embedder = embedder
        self._store = store
        self._files_dir = Path(files_dir)

    def ingest_file(self, kb_id: str, path: Path, original_name: str) -> str:
        content_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with self._sf() as s:
            dup = s.query(Document).filter_by(
                kb_id=kb_id, content_hash=content_hash).first()
            if dup:
                return dup.id
            doc = Document(id=str(uuid.uuid4()), kb_id=kb_id,
                           filename=original_name, content_hash=content_hash,
                           status="parsing")
            s.add(doc)
            s.commit()
            doc_id = doc.id
        try:
            self._process(kb_id, doc_id, path, original_name)
            self._set_status(doc_id, "ready")
        except Exception as e:  # noqa: BLE001 —— 批次隔离，失败落库
            self._set_status(doc_id, "failed", error=f"{type(e).__name__}: {e}")
        return doc_id

    def _process(self, kb_id: str, doc_id: str, path: Path, name: str):
        from markitdown import MarkItDown
        markdown = MarkItDown(enable_plugins=False).convert(str(path)).text_content
        if not markdown.strip():
            raise ValueError("解析结果为空（可能是扫描件，M1 不支持 OCR）")
        # 双存：Markdown 中间产物落盘，重建索引不用重新解析
        out_dir = self._files_dir / doc_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "content.md").write_text(markdown, encoding="utf-8")

        chunks = self._chunker.chunk(markdown, doc_name=name)
        leaves = [c for c in chunks if c.parent_id is not None]
        # 叶子块向量化文本 = 标题路径 + 正文（heading path 参与语义）
        vectors = self._embedder.embed(
            [f"{c.heading_path}\n{c.text}" for c in leaves])
        self._store.upsert(
            collection=kb_id,
            ids=[c.id for c in leaves],
            vectors=vectors,
            metas=[{"doc_id": doc_id, "parent_id": c.parent_id} for c in leaves],
        )
        with self._sf() as s:
            for c in chunks:
                s.add(Chunk(id=c.id, doc_id=doc_id, kb_id=kb_id,
                            parent_id=c.parent_id, prev_id=c.prev_id,
                            next_id=c.next_id, heading_path=c.heading_path,
                            text=c.text, is_leaf=c.parent_id is not None))
            s.commit()

    def _set_status(self, doc_id: str, status: str, error: str | None = None):
        with self._sf() as s:
            doc = s.get(Document, doc_id)
            doc.status = status
            doc.error = error
            s.commit()
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_ingest.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add kbase/ingest/ tests/test_ingest.py
git commit -m "feat: 摄取管道（markitdown+分块+向量化+去重+失败隔离）"
```

---

### Task 10: 检索器（small-to-big 上下文组装）

**Files:**
- Create: `kbase/rag/__init__.py`（空）、`kbase/rag/retriever.py`
- Test: `tests/test_retriever.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_retriever.py
from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.rag.retriever import Retriever

MD = """# 补贴办法
## 第一章 申领条件
连续工作满两年可申领住房补贴。
补贴对象为在编在岗人员。
## 第二章 标准
每月补贴一千元。
"""


def _setup(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=30, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "files")
    f = tmp_path / "补贴办法.md"
    f.write_text(MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "补贴办法.md")
    return Retriever(factory, fake_embedder, store)


def test_retrieve_returns_parent_context(tmp_path, fake_embedder):
    r = _setup(tmp_path, fake_embedder)
    # FakeEmbedder 是 hash 确定性的：用与某叶子块向量化文本一致的查询保证命中
    query = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    blocks = r.retrieve("kb1", query, top_k=3)
    assert blocks
    top = blocks[0]
    # small-to-big：返回的是父块全文，包含叶子块之外的兄弟内容
    assert "连续工作满两年" in top.text
    assert "在编在岗" in top.text
    assert top.doc_name == "补贴办法.md"
    assert "第一章" in top.heading_path


def test_parent_dedup(tmp_path, fake_embedder):
    """同一父块下多个叶子命中时，父块只出现一次。"""
    r = _setup(tmp_path, fake_embedder)
    query = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    blocks = r.retrieve("kb1", query, top_k=10)
    paths = [b.heading_path for b in blocks]
    assert len(paths) == len(set(paths))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_retriever.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: 实现 retriever.py**

```python
# kbase/rag/retriever.py
from dataclasses import dataclass

from kbase.models import Chunk, Document
from kbase.plugins.base import Embedder, VectorStore


@dataclass
class ContextBlock:
    doc_id: str
    doc_name: str
    heading_path: str
    text: str          # 父块全文（small-to-big 的"big"）
    snippet: str       # 命中的叶子块原文（引用展示用）
    score: float


class Retriever:
    def __init__(self, session_factory, embedder: Embedder, store: VectorStore):
        self._sf = session_factory
        self._embedder = embedder
        self._store = store

    def retrieve(self, kb_id: str, query: str, top_k: int = 5) -> list[ContextBlock]:
        vec = self._embedder.embed([query])[0]
        hits = self._store.search(kb_id, vec, top_k=top_k)
        blocks: list[ContextBlock] = []
        seen_parents: set[str] = set()
        with self._sf() as s:
            for hit in hits:
                leaf = s.get(Chunk, hit.chunk_id)
                if leaf is None:
                    continue
                parent = s.get(Chunk, leaf.parent_id) if leaf.parent_id else leaf
                if parent.id in seen_parents:
                    continue
                seen_parents.add(parent.id)
                doc = s.get(Document, leaf.doc_id)
                blocks.append(ContextBlock(
                    doc_id=leaf.doc_id,
                    doc_name=doc.filename if doc else "未知文档",
                    heading_path=parent.heading_path,
                    text=parent.text,
                    snippet=leaf.text,
                    score=hit.score,
                ))
        return blocks
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_retriever.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add kbase/rag/ tests/test_retriever.py
git commit -m "feat: 检索器（叶子命中→父块上下文组装+父块去重）"
```

---

### Task 11: 生成器（prompt 组装 + 流式 + 引用 + 拒答）

**Files:**
- Create: `kbase/rag/generator.py`
- Test: `tests/test_generator.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_generator.py
import pytest

from kbase.rag.generator import MIN_SCORE, Generator
from kbase.rag.retriever import ContextBlock


class FakeLLM:
    """回显收到的 user prompt，便于断言 prompt 组装。"""
    def __init__(self):
        self.last_messages = None

    async def stream(self, messages, **params):
        self.last_messages = messages
        for piece in ["根据资料[1]，", "满两年可申领。"]:
            yield piece


def _block(score=0.9):
    return ContextBlock(doc_id="d1", doc_name="补贴办法.docx",
                        heading_path="补贴办法.docx > 第一章",
                        text="连续工作满两年可申领住房补贴。",
                        snippet="满两年可申领", score=score)


async def test_stream_with_citations():
    llm = FakeLLM()
    gen = Generator(llm)
    chunks = [c async for c in gen.answer_stream("申领条件是什么", [_block()])]
    assert "".join(chunks) == "根据资料[1]，满两年可申领。"
    user_prompt = llm.last_messages[-1]["content"]
    assert "[1]" in user_prompt and "连续工作满两年" in user_prompt
    assert "申领条件是什么" in user_prompt
    cits = gen.citations([_block()])
    assert cits[0]["index"] == 1 and cits[0]["doc_name"] == "补贴办法.docx"


async def test_refusal_when_no_context():
    gen = Generator(FakeLLM())
    chunks = [c async for c in gen.answer_stream("随便问", [])]
    assert "未找到依据" in "".join(chunks)


async def test_refusal_when_low_score():
    gen = Generator(FakeLLM())
    low = _block(score=MIN_SCORE - 0.01)
    chunks = [c async for c in gen.answer_stream("随便问", [low])]
    assert "未找到依据" in "".join(chunks)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_generator.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: 实现 generator.py**

```python
# kbase/rag/generator.py
from typing import AsyncIterator

from kbase.rag.retriever import ContextBlock

MIN_SCORE = 0.3   # 相关度阈值，低于此值拒答（FakeEmbedder/真实模型下均可调）

REFUSAL = "知识库中未找到依据，无法回答该问题。请尝试换个问法，或确认相关文档已导入。"

SYSTEM_PROMPT = (
    "你是一个严谨的知识库问答助手。只依据提供的资料回答问题，"
    "禁止编造资料中不存在的内容。回答中引用资料时标注编号，如[1][2]。"
    "如果资料不足以回答问题，明确说明。使用简体中文回答。"
)

USER_TEMPLATE = """请依据以下资料回答问题。

{sources}

问题：{question}"""


class Generator:
    def __init__(self, llm):
        self._llm = llm

    def citations(self, blocks: list[ContextBlock]) -> list[dict]:
        return [{"index": i + 1, "doc_name": b.doc_name,
                 "heading_path": b.heading_path, "snippet": b.snippet,
                 "score": round(b.score, 3)}
                for i, b in enumerate(blocks)]

    def _build_messages(self, question: str, blocks: list[ContextBlock]) -> list[dict]:
        sources = "\n\n".join(
            f"[{i + 1}] 出处：{b.heading_path}\n{b.text}"
            for i, b in enumerate(blocks))
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": USER_TEMPLATE.format(sources=sources, question=question)},
        ]

    async def answer_stream(self, question: str,
                            blocks: list[ContextBlock]) -> AsyncIterator[str]:
        usable = [b for b in blocks if b.score >= MIN_SCORE]
        if not usable:
            yield REFUSAL
            return
        async for piece in self._llm.stream(self._build_messages(question, usable)):
            yield piece
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_generator.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add kbase/rag/generator.py tests/test_generator.py
git commit -m "feat: 生成器（引用编号 prompt+流式+低相关度拒答）"
```

---

### Task 12: FastAPI 接入层（含 SSE 问答）

**Files:**
- Create: `kbase/api/__init__.py`（空）、`kbase/api/main.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api.py
import json

from fastapi.testclient import TestClient

from kbase.api.main import create_app

CFG = """
data_dir: {data_dir}
chunker: {{name: structure, chunk_size: 200, chunk_overlap: 0}}
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
"""

MD = "# 补贴办法\n## 第一章 申领条件\n连续工作满两年可申领住房补贴。\n"


class FakeLLM:
    model = "fake"

    async def stream(self, messages, **params):
        yield "满两年"
        yield "可申领[1]。"


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()})
    return TestClient(app)


def test_kb_create_and_list(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/kb", json={"name": "政策库"})
    assert r.status_code == 200
    kb_id = r.json()["id"]
    assert any(k["id"] == kb_id for k in c.get("/api/kb").json())


def test_upload_and_document_status(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    r = c.post(f"/api/kb/{kb_id}/documents",
               files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    assert r.status_code == 200
    # TestClient 中 BackgroundTasks 在响应后同步执行完毕
    docs = c.get(f"/api/kb/{kb_id}/documents").json()
    assert docs[0]["filename"] == "补贴办法.md"
    assert docs[0]["status"] == "ready"


def test_query_sse_stream(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    # FakeEmbedder 哈希确定性：用叶子块向量化文本原文当查询保证命中
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    with c.stream("POST", f"/api/kb/{kb_id}/query",
                  json={"question": q}) as r:
        body = "".join(r.iter_text())
    assert "event: citations" in body
    assert "补贴办法.md" in body            # 引用里有文档名
    assert "event: token" in body
    assert "event: done" in body


def test_providers_endpoint(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.get("/api/providers").json()
    assert r["active"] == "fake"
    assert "fake" in r["providers"]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_api.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: 实现 api/main.py**

```python
# kbase/api/main.py
"""HTTP 编排层：只做参数校验与调度，业务逻辑在 ingest/rag 模块。"""
import json
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from kbase.config import load_config
from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import Document, KnowledgeBase
from kbase.plugins.registry import registry
from kbase.rag.generator import Generator
from kbase.rag.retriever import Retriever


def _load_builtin_plugins():
    """import 触发注册。新增插件实现时在此登记。"""
    import kbase.plugins.chunkers.structure      # noqa: F401
    import kbase.plugins.vectorstores.chroma_store  # noqa: F401
    import kbase.plugins.llm.openai_compat       # noqa: F401


class KBCreate(BaseModel):
    name: str


class QueryBody(BaseModel):
    question: str
    provider: str | None = None     # 不传用配置里的 active —— 模型对比入口
    top_k: int = 5


def create_app(config_path="config/kbase.yaml", *, embedder=None,
               store=None, llms: dict | None = None) -> FastAPI:
    _load_builtin_plugins()
    cfg = load_config(config_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    sf = make_session_factory(f"sqlite:///{cfg.data_dir}/kbase.sqlite")

    if embedder is None:   # 测试注入 FakeEmbedder，生产走配置
        # bge_local 依赖 local-embed extra 且加载慢，仅在真正需要时 import 注册
        import kbase.plugins.embedders.bge_local  # noqa: F401
        embedder = registry.create("embedder", cfg.embedder.name,
                                   model=cfg.embedder.model)
    if store is None:
        store = registry.create("vectorstore", cfg.vectorstore.name,
                                persist_dir=str(cfg.data_dir / "chroma"))
    chunker = registry.create("chunker", cfg.chunker.name,
                              chunk_size=cfg.chunker.chunk_size,
                              chunk_overlap=cfg.chunker.chunk_overlap)
    pipeline = IngestPipeline(sf, chunker, embedder, store,
                              files_dir=cfg.data_dir / "files")
    retriever = Retriever(sf, embedder, store)

    _llm_cache: dict = dict(llms or {})

    def get_llm(name: str | None):
        pname = name or cfg.llm.active
        if pname not in _llm_cache:      # 懒创建：没配密钥的 provider 不影响启动
            p = cfg.get_provider(pname)  # KeyError -> 404 在路由层转换
            _llm_cache[pname] = registry.create(
                "llm", "openai-compat", base_url=p.base_url,
                api_key_env=p.api_key_env, model=p.model,
                max_concurrency=p.max_concurrency)
        return _llm_cache[pname]

    app = FastAPI(title="KBase")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "embedder": type(embedder).__name__,
                "vectorstore": type(store).__name__}

    @app.get("/api/providers")
    def providers():
        return {"active": cfg.llm.active,
                "providers": [p.name for p in cfg.llm.providers]}

    @app.post("/api/kb")
    def create_kb(body: KBCreate):
        kb = KnowledgeBase(id=str(uuid.uuid4()), name=body.name)
        with sf() as s:
            s.add(kb)
            s.commit()
        return {"id": kb.id, "name": kb.name}

    @app.get("/api/kb")
    def list_kb():
        with sf() as s:
            return [{"id": k.id, "name": k.name}
                    for k in s.query(KnowledgeBase).all()]

    @app.post("/api/kb/{kb_id}/documents")
    def upload(kb_id: str, files: list[UploadFile], bg: BackgroundTasks):
        upload_dir = cfg.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        accepted = []
        for f in files:
            dest = upload_dir / f"{uuid.uuid4()}-{f.filename}"
            dest.write_bytes(f.file.read())
            bg.add_task(pipeline.ingest_file, kb_id, dest, f.filename)
            accepted.append(f.filename)
        return {"accepted": accepted}

    @app.get("/api/kb/{kb_id}/documents")
    def list_docs(kb_id: str):
        with sf() as s:
            docs = s.query(Document).filter_by(kb_id=kb_id).all()
            return [{"id": d.id, "filename": d.filename, "status": d.status,
                     "error": d.error} for d in docs]

    @app.post("/api/kb/{kb_id}/query")
    async def query(kb_id: str, body: QueryBody):
        try:
            llm = get_llm(body.provider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        blocks = retriever.retrieve(kb_id, body.question, top_k=body.top_k)
        gen = Generator(llm)

        async def events():
            yield {"event": "citations",
                   "data": json.dumps(gen.citations(blocks), ensure_ascii=False)}
            async for piece in gen.answer_stream(body.question, blocks):
                yield {"event": "token", "data": piece}
            yield {"event": "done", "data": ""}

        return EventSourceResponse(events())

    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_api.py -v`
Expected: 4 passed

- [ ] **Step 5: 全量回归**

Run: `.venv\Scripts\python -m pytest -v`
Expected: 此前所有测试 + 本任务全部 passed

- [ ] **Step 6: Commit**

```bash
git add kbase/api/ tests/test_api.py
git commit -m "feat: FastAPI 接入层（知识库/上传/SSE 问答/provider 切换）"
```

---

### Task 13: 零构建前端（问答 + 知识库管理）

**Files:**
- Create: `web/index.html`、`web/app.js`

零构建原生 JS，Demo 后按 spec 换 Vue3。无自动化测试，验收为手动清单（Step 3）。

- [ ] **Step 1: 写 index.html**

```html
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>KBase 知识库</title>
<style>
  body { font-family: system-ui, "Microsoft YaHei"; margin: 0; display: flex; height: 100vh; }
  #side { width: 300px; border-right: 1px solid #ddd; padding: 16px; overflow-y: auto; }
  #main { flex: 1; display: flex; flex-direction: column; }
  #chat { flex: 1; overflow-y: auto; padding: 16px; }
  #inputbar { display: flex; gap: 8px; padding: 16px; border-top: 1px solid #ddd; }
  #question { flex: 1; padding: 8px; }
  .msg { margin: 8px 0; padding: 10px 12px; border-radius: 8px; max-width: 80%; white-space: pre-wrap; }
  .user { background: #e3f0ff; margin-left: auto; }
  .bot { background: #f5f5f5; }
  .cite { font-size: 12px; color: #555; border-left: 3px solid #bbb; margin: 4px 0; padding: 4px 8px; cursor: pointer; }
  .cite .snippet { display: none; }
  .cite.open .snippet { display: block; margin-top: 4px; color: #333; }
  .doc { font-size: 13px; padding: 4px 0; display: flex; justify-content: space-between; }
  .status-ready { color: green; } .status-failed { color: red; }
  .status-pending, .status-parsing { color: #b8860b; }
  select, button, input[type=text] { padding: 6px; }
  h3 { margin: 12px 0 6px; }
</style>
</head>
<body>
  <div id="side">
    <h3>知识库</h3>
    <select id="kbSelect" style="width:100%"></select>
    <div style="margin-top:8px; display:flex; gap:4px">
      <input type="text" id="newKbName" placeholder="新知识库名">
      <button id="createKb">建库</button>
    </div>
    <h3>文档</h3>
    <input type="file" id="fileInput" multiple>
    <button id="uploadBtn">上传</button>
    <button id="refreshDocs">刷新</button>
    <div id="docList"></div>
    <h3>模型</h3>
    <select id="providerSelect" style="width:100%"></select>
  </div>
  <div id="main">
    <div id="chat"></div>
    <div id="inputbar">
      <input type="text" id="question" placeholder="输入问题，回车发送">
      <button id="sendBtn">发送</button>
    </div>
  </div>
<script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 2: 写 app.js**

```javascript
const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function loadKbs() {
  const kbs = await api("/api/kb");
  $("kbSelect").innerHTML = kbs.map(k => `<option value="${k.id}">${k.name}</option>`).join("");
  if (kbs.length) loadDocs();
}

async function loadProviders() {
  const p = await api("/api/providers");
  $("providerSelect").innerHTML = p.providers.map(
    n => `<option ${n === p.active ? "selected" : ""}>${n}</option>`).join("");
}

async function loadDocs() {
  const kb = $("kbSelect").value;
  if (!kb) return;
  const docs = await api(`/api/kb/${kb}/documents`);
  $("docList").innerHTML = docs.map(d =>
    `<div class="doc"><span title="${d.error || ""}">${d.filename}</span>
     <span class="status-${d.status}">${d.status}</span></div>`).join("");
}

$("createKb").onclick = async () => {
  const name = $("newKbName").value.trim();
  if (!name) return;
  await api("/api/kb", { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify({ name }) });
  $("newKbName").value = "";
  await loadKbs();
};

$("uploadBtn").onclick = async () => {
  const kb = $("kbSelect").value;
  const fd = new FormData();
  for (const f of $("fileInput").files) fd.append("files", f);
  await api(`/api/kb/${kb}/documents`, { method: "POST", body: fd });
  setTimeout(loadDocs, 1000);   // 解析是后台任务，稍后刷新
};

$("refreshDocs").onclick = loadDocs;
$("kbSelect").onchange = loadDocs;

function addMsg(cls, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.textContent = text;
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
  return div;
}

function addCitations(cits) {
  for (const c of cits) {
    const div = document.createElement("div");
    div.className = "cite";
    div.innerHTML = `[${c.index}] ${c.heading_path}（相关度 ${c.score}）
      <div class="snippet">${c.snippet}</div>`;
    div.onclick = () => div.classList.toggle("open");
    $("chat").appendChild(div);
  }
}

async function send() {
  const q = $("question").value.trim();
  const kb = $("kbSelect").value;
  if (!q || !kb) return;
  $("question").value = "";
  addMsg("user", q);
  const bot = addMsg("bot", "");
  const resp = await fetch(`/api/kb/${kb}/query`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question: q, provider: $("providerSelect").value }),
  });
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "", event = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) {
        const data = line.slice(5).replace(/^ /, "");
        if (event === "token") bot.textContent += data;
        else if (event === "citations") addCitations(JSON.parse(data));
      }
      $("chat").scrollTop = $("chat").scrollHeight;
    }
  }
}

$("sendBtn").onclick = send;
$("question").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });

loadKbs();
loadProviders();
```

- [ ] **Step 3: 手动验收**

Run: `$env:DASHSCOPE_API_KEY="<你的key>"; .venv\Scripts\uvicorn --factory kbase.api.main:create_app --port 8000`
浏览器打开 `http://localhost:8000`，逐项确认：

1. 建库 → 下拉框出现新库
2. 上传一个 .docx/.md → 文档列表状态从 parsing 变 ready（点刷新）
3. 提问 → 先出现引用条目，答案逐字流出
4. 点击引用 → 展开原文片段
5. 切换模型下拉 → 再次提问走另一个模型（后端日志确认 model 不同）

- [ ] **Step 4: Commit**

```bash
git add web/
git commit -m "feat: 零构建前端（知识库管理+SSE 流式问答+引用展开+模型切换）"
```

---

### Task 14: 评测集与模型对比报告

**Files:**
- Create: `eval/questions.jsonl`（示例 3 条，正式题目周一人工补齐至 20~30 条）
- Create: `eval/run_eval.py`

- [ ] **Step 1: 写评测集格式（示例）**

`eval/questions.jsonl`，每行一题：

```json
{"question": "住房补贴的申领条件是什么？", "expect_doc": "补贴办法", "expect_keywords": ["满两年", "在编在岗"]}
{"question": "补贴标准是每月多少？", "expect_doc": "补贴办法", "expect_keywords": ["一千元"]}
{"question": "差旅费报销需要什么凭证？", "expect_doc": "报销制度", "expect_keywords": ["发票", "审批单"]}
```

字段约定：`expect_doc` 为文档名子串（判检索命中）；`expect_keywords` 为答案应包含的关键词（判生成质量）。

- [ ] **Step 2: 写 run_eval.py**

```python
# eval/run_eval.py
"""检索命中率 + 答案关键词覆盖率评测，支持多 provider 对比。
用法：
  python eval/run_eval.py --kb <kb_id> --providers qwen-72b,qwen-32b
输出：eval/report.md
"""
import argparse
import asyncio
import json
from pathlib import Path

from kbase.config import load_config
from kbase.db import make_session_factory
from kbase.plugins.registry import registry
from kbase.rag.generator import Generator
from kbase.rag.retriever import Retriever


def build_components(cfg):
    import kbase.plugins.embedders.bge_local      # noqa: F401
    import kbase.plugins.llm.openai_compat        # noqa: F401
    import kbase.plugins.vectorstores.chroma_store  # noqa: F401
    sf = make_session_factory(f"sqlite:///{cfg.data_dir}/kbase.sqlite")
    embedder = registry.create("embedder", cfg.embedder.name, model=cfg.embedder.model)
    store = registry.create("vectorstore", cfg.vectorstore.name,
                            persist_dir=str(cfg.data_dir / "chroma"))
    return Retriever(sf, embedder, store)


async def run(args):
    cfg = load_config(args.config)
    retriever = build_components(cfg)
    questions = [json.loads(l) for l in
                 Path("eval/questions.jsonl").read_text(encoding="utf-8").splitlines()
                 if l.strip()]
    providers = args.providers.split(",")
    rows, hit_count = [], 0

    retrievals = {}
    for q in questions:
        blocks = retriever.retrieve(args.kb, q["question"], top_k=args.top_k)
        hit = any(q["expect_doc"] in b.doc_name for b in blocks)
        hit_count += hit
        retrievals[q["question"]] = (blocks, hit)

    for pname in providers:
        p = cfg.get_provider(pname)
        llm = registry.create("llm", "openai-compat", base_url=p.base_url,
                              api_key_env=p.api_key_env, model=p.model)
        gen = Generator(llm)
        for q in questions:
            blocks, hit = retrievals[q["question"]]
            answer = "".join([t async for t in
                              gen.answer_stream(q["question"], blocks)])
            covered = [k for k in q["expect_keywords"] if k in answer]
            rows.append({"provider": pname, "question": q["question"],
                         "retrieval_hit": hit,
                         "keyword_coverage": f"{len(covered)}/{len(q['expect_keywords'])}",
                         "answer": answer[:200]})

    lines = [f"# KBase 评测报告", "",
             f"- 题目数：{len(questions)}，top_k={args.top_k}",
             f"- 检索命中率：{hit_count}/{len(questions)}", "",
             "| Provider | 问题 | 检索命中 | 关键词覆盖 | 答案(截断) |",
             "|---|---|---|---|---|"]
    for r in rows:
        ans = r["answer"].replace("\n", " ").replace("|", "\\|")
        lines.append(f"| {r['provider']} | {r['question']} | "
                     f"{'✓' if r['retrieval_hit'] else '✗'} | "
                     f"{r['keyword_coverage']} | {ans} |")
    Path("eval/report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"完成：eval/report.md（{len(rows)} 行）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/kbase.yaml")
    ap.add_argument("--kb", required=True)
    ap.add_argument("--providers", required=True)
    ap.add_argument("--top-k", type=int, default=5)
    asyncio.run(run(ap.parse_args()))
```

- [ ] **Step 3: 冒烟验证（用已导入文档的库）**

Run: `.venv\Scripts\python eval/run_eval.py --kb <上一任务建的 kb_id> --providers qwen-72b,qwen-32b`
Expected: 生成 `eval/report.md`，两个 provider 各出一组行；无 API Key 时报错信息明确指向环境变量名

- [ ] **Step 4: Commit**

```bash
git add eval/
git commit -m "feat: 检索命中率+关键词覆盖评测，多 provider 对比报告"
```

---

### Task 15: README、全量回归与 Demo 演练

**Files:**
- Create: `README.md`

- [ ] **Step 1: 写 README.md**

内容包含（各节 3~10 行，写实际可执行的命令）：

```markdown
# KBase 私有化知识库系统

## 快速开始（lite 模式）
python -m venv .venv
.venv\Scripts\pip install -e ".[dev,local-embed]"
$env:HF_ENDPOINT="https://hf-mirror.com"     # 国内网络
$env:DASHSCOPE_API_KEY="<你的key>"
.venv\Scripts\uvicorn --factory kbase.api.main:create_app --port 8000
# 浏览器打开 http://localhost:8000

## 配置
config/kbase.yaml：Embedding / 向量库 / 分块 / LLM providers。
密钥一律走环境变量（api_key_env），不写进配置文件。

## 测试
.venv\Scripts\python -m pytest              # 快速套件
.venv\Scripts\python -m pytest -m external  # 含真实模型/API

## 评测（模型对比）
python eval/run_eval.py --kb <kb_id> --providers qwen-72b,qwen-32b
产出 eval/report.md

## 架构
见 docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md
```

- [ ] **Step 2: 全量回归**

Run: `.venv\Scripts\python -m pytest -v`
Expected: 全部 passed（external 除外）
Run: `.venv\Scripts\python -m pytest -m external -v`（有网络与 API Key 时）
Expected: 全部 passed

- [ ] **Step 3: Demo 演练（真实数据彩排）**

用 `材料/` 下的脱敏文档完整走一遍：建库 → 批量上传 → 全部 ready → 中文提问 3 个 → 引用正确 → 切换 32B/72B 各答一遍 → 跑 eval 出报告。记录卡点。

**注意**：`材料/` 内文档及生成的 `data/` 目录已被 .gitignore 排除，确认 `git status` 里没有任何内部资料再提交。

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README 快速开始与运维说明"
```

---

## 任务依赖与并行度

```
Task 1 → 2 → 3 → {4, 5, 6, 7, 8 可并行} → 9 → 10 → 11 → 12 → {13, 14 可并行} → 15
```

评测集正式题目（20~30 条）依赖需求方脱敏文档，与开发并行准备，不阻塞任何任务。
