"""API 请求体模型：全部路由共用的 Pydantic schema 集中在此。

只放"请求体的形状与校验"，不放业务逻辑——路由端点在 kbase/api/routes/ 各
领域模块，服务装配在 kbase/api/services.py。"""
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, StrictInt, model_validator


class LoginBody(BaseModel):
    username: str
    password: str


class KBCreate(BaseModel):
    name: str
    # M5-2：建库时绑定向量模型（GET /api/embedders 清单里的 id）。
    # None/"default" = 默认模型。建库后不可改（换模型=全库向量作废，需重建）。
    embedder: str | None = None


class QueryBody(BaseModel):
    question: str
    provider: str | None = None     # 不传用配置里的 active —— 模型对比入口
    top_k: int = 5


class SearchBody(BaseModel):
    query: str
    top_k: int = 5
    debug: bool = False


class ConversationCreate(BaseModel):
    kb_id: str


class ConversationRename(BaseModel):
    title: str


class EnrichConfigBody(BaseModel):
    enabled: StrictBool


class KBConfigBody(BaseModel):
    """PUT /api/kb/{kb_id}/config 请求体：只接受已知 key，未知 key 由
    model_config extra="forbid" 拒绝（422），避免前端笔误的字段被静默丢弃。"""
    model_config = {"extra": "forbid"}

    chunk_size: StrictInt | None = Field(default=None, ge=64, le=4096)
    chunk_overlap: StrictInt | None = Field(default=None, ge=0, le=512)
    enrich: EnrichConfigBody | None = None

    @model_validator(mode="after")
    def _check_overlap_lt_size(self):
        if (self.chunk_size is not None and self.chunk_overlap is not None
                and self.chunk_overlap >= self.chunk_size):
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class ProviderCreate(BaseModel):
    name: str
    base_url: str
    # M5-2：密钥两种来源二选一（也可都给，api_key 优先）——
    # api_key：页面直配，存 DB（私有化内网部署的主路径）；
    # api_key_env：环境变量名，密钥不进 DB（运维托管密钥的部署用）。
    api_key_env: str = ""
    api_key: str | None = None
    model: str
    max_concurrency: int = 4
    params: dict = {}

    @model_validator(mode="after")
    def _check_key_source(self):
        if not self.api_key and not self.api_key_env:
            raise ValueError("必须提供 api_key（页面直配）或 api_key_env（环境变量名）之一")
        return self


class ProviderUpdate(BaseModel):
    base_url: str | None = None
    api_key_env: str | None = None
    # PATCH 语义：请求体不含 api_key 字段则不动；显式传 ""/null 表示清除
    # 直配密钥、回退到 api_key_env（见 providers_store.update_provider）。
    api_key: str | None = None
    model: str | None = None
    max_concurrency: int | None = None
    params: dict | None = None


class ActiveProviderBody(BaseModel):
    name: str


class OutlineBody(BaseModel):
    kb_id: str
    topic: str
    requirements: str = ""
    provider: str | None = None


class JobCreate(BaseModel):
    type: str
    kb_id: str
    provider: str | None = None
    params: dict = {}


# 合法角色枚举——pydantic 用它约束请求体的 role 字段，拒绝形如
# "superadmin"/"root" 的伪角色（否则会被写进 DB，之后每个 require_role 请求都
# 在 deps.py 的 _ROLE_RANK[actor["role"]] 处以未捕获 KeyError 抛 500）。
Role = Literal["admin", "editor", "viewer"]


class ApiKeyCreate(BaseModel):
    name: str
    role: Role


class UserCreate(BaseModel):
    username: str
    role: Role
    password: str


class UserUpdate(BaseModel):
    role: Role | None = None
    disabled: bool | None = None
    password: str | None = None
