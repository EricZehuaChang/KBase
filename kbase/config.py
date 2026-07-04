from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


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
    model: str = "BAAI/bge-reranker-v2-m3"


class RetrievalConfig(BaseModel):
    hybrid: bool = True
    candidates: int = 20          # 每路召回数与融合候选数
    rrf_k: int = 60
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    min_score_dense: float = 0.3
    min_score_rerank: float = 0.35


class EnrichConfig(BaseModel):
    # 全局开关：kb 是否可以启用上下文增强（真正是否增强由每个 kb 自己的
    # KnowledgeBase.config JSON 里的 enrich.enabled 决定，见 ingest/pipeline.py）。
    # provider=None 表示用 llm.active 对应的 provider 做增强调用。
    provider: str | None = None


class OCRConfig(BaseModel):
    # enabled=False（默认）：不创建 OCR 后端，扫描件/图片直接判 failed（M1 行为）。
    # backend 目前只有 monkey-http 一种实现（kbase/plugins/ocr/monkey_http.py）。
    enabled: bool = False
    backend: str = "monkey-http"
    endpoint: str = "http://localhost:7861"


class AppConfig(BaseModel):
    data_dir: Path = Path("./data")
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    vectorstore: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    enrich: EnrichConfig = Field(default_factory=EnrichConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    llm: LLMConfig

    def get_provider(self, name: str) -> ProviderConfig:
        for p in self.llm.providers:
            if p.name == name:
                return p
        raise KeyError(f"LLM provider 未配置: {name}")


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)
