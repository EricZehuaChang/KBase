from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class EmbedderConfig(BaseModel):
    name: str = "bge-local"
    model: str = "BAAI/bge-m3"
    endpoint: str | None = None   # name="tei" 时必填：TEI 服务地址


class DBConfig(BaseModel):
    # {data_dir} 占位符由 create_app 替换成实际路径，保持 sqlite 默认语义
    # 与改造前的 f"sqlite:///{cfg.data_dir}/kbase.sqlite" 字节级一致；
    # postgresql+psycopg:// 等其他 URL 原样透传，不做占位替换。
    url: str = "sqlite:///{data_dir}/kbase.sqlite"


class VectorStoreConfig(BaseModel):
    name: str = "chroma"
    endpoint: str | None = None   # name="qdrant" 时必填：Qdrant 服务地址
    api_key: str | None = None    # Qdrant Cloud 等需要鉴权的部署可选填


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
    params: dict = Field(default_factory=dict)   # 每次调用透传给 chat.completions.create 的默认参数（如 extra_body）


class LLMConfig(BaseModel):
    active: str
    providers: list[ProviderConfig]

    @model_validator(mode="after")
    def _check_active_in_providers(self) -> "LLMConfig":
        names = {p.name for p in self.providers}
        if self.active not in names:
            raise ValueError(
                f"llm.active 指向未配置的 provider: {self.active}，"
                f"已配置: {sorted(names)}")
        return self


class RerankConfig(BaseModel):
    enabled: bool = True
    name: str = "bge-local"
    model: str = "BAAI/bge-reranker-v2-m3"
    endpoint: str | None = None   # name="tei" 时必填：TEI 服务地址


class RewriteConfig(BaseModel):
    # mode: "off"=从不改写；"conditional"=按 should_rewrite 的启发式判断触发；
    # "always"=只要有历史就触发（仍需要非空 history）。
    # provider=None 表示用 llm.active 对应的 provider 做改写调用。
    mode: str = "conditional"
    provider: str | None = None
    max_wait_s: float = 5.0


class RetrievalConfig(BaseModel):
    hybrid: bool = True
    candidates: int = 20          # 每路召回数与融合候选数
    rrf_k: int = 60
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    min_score_dense: float = 0.3
    min_score_rerank: float = 0.35
    min_include_score: float = 0.1   # 收录底线：低于它的块视为噪声剔除（拒答门另看最高分）
    rewrite: RewriteConfig = Field(default_factory=RewriteConfig)
    max_parent_chars: int = 4000   # D6：父块截窗上限


class EnrichConfig(BaseModel):
    # 全局开关：kb 是否可以启用上下文增强（真正是否增强由每个 kb 自己的
    # KnowledgeBase.config JSON 里的 enrich.enabled 决定，见 ingest/pipeline.py）。
    # provider=None 表示用 llm.active 对应的 provider 做增强调用。
    provider: str | None = None


class IngestConfig(BaseModel):
    # D5：上传路由用 ThreadPoolExecutor 并行摄取多个文件，workers 控制并发度。
    workers: int = 2


class OCRConfig(BaseModel):
    # enabled=False（默认）：不创建 OCR 后端，扫描件/图片直接判 failed（M1 行为）。
    # backend 目前只有 monkey-http 一种实现（kbase/plugins/ocr/monkey_http.py）。
    enabled: bool = False
    backend: str = "monkey-http"
    endpoint: str = "http://localhost:7861"


class AppConfig(BaseModel):
    data_dir: Path = Path("./data")
    db: DBConfig = Field(default_factory=DBConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    vectorstore: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    enrich: EnrichConfig = Field(default_factory=EnrichConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    llm: LLMConfig

    def get_provider(self, name: str) -> ProviderConfig:
        for p in self.llm.providers:
            if p.name == name:
                return p
        raise KeyError(f"LLM provider 未配置: {name}")


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)


def resolve_db_url(cfg: AppConfig) -> str:
    """把 cfg.db.url 里的 {data_dir} 占位符替换成实际路径；不含占位符的 URL
    （如 postgresql+psycopg://...）原样透传。

    不能无条件 .format(data_dir=...)：PG 密码/URL 中若含字面 "{"（如密码里
    恰好有花括号），.format 会因缺少匹配字段名而抛 KeyError/ValueError 崩溃；
    反过来若 URL 中恰好含字面 "{data_dir}" 之外的花括号内容也可能被误替换。
    只在确认存在 "{data_dir}" 占位符时才调用 .format，其余情况一律原样返回，
    这样才符合本文件顶部注释里"postgresql+psycopg:// 等其他 URL 原样透传，
    不做占位替换"的约定。"""
    url = cfg.db.url
    return url.format(data_dir=str(cfg.data_dir)) if "{data_dir}" in url else url
