"""HTTP 编排层：只做参数校验与调度，业务逻辑在 ingest/rag 模块。"""
import json
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
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
        if pname not in _llm_cache:      # 懒创建：请求时在应用事件循环内构建，
            p = cfg.get_provider(pname)  # 未配密钥的 provider 不影响启动
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
            safe_name = Path(f.filename or "unnamed").name   # 去除路径分隔符，防穿越；兜底 None
            dest = upload_dir / f"{uuid.uuid4()}-{safe_name}"
            dest.write_bytes(f.file.read())
            bg.add_task(pipeline.ingest_file, kb_id, dest, safe_name)
            accepted.append(safe_name)
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
        except RuntimeError as e:      # 环境变量未设置等初始化失败：给前端可读信息
            raise HTTPException(503, str(e)) from e
        # 检索（含向量化）是同步 CPU/IO 混合操作，进线程池避免阻塞事件循环
        blocks = await run_in_threadpool(
            retriever.retrieve, kb_id, body.question, body.top_k)
        gen = Generator(llm)
        # 关键契约：usable_blocks 只算一次，citations 与 answer_stream 用同一份列表，
        # 保证引用编号与答案中的 [n] 标记对齐（拒答时 citations 为空列表）
        usable = gen.usable_blocks(blocks)

        async def events():
            yield {"event": "citations",
                   "data": json.dumps(gen.citations(usable), ensure_ascii=False)}
            async for piece in gen.answer_stream(body.question, usable):
                yield {"event": "token", "data": piece}
            yield {"event": "done", "data": ""}

        return EventSourceResponse(events())

    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app
