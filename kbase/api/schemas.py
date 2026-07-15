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


class UrlImportBody(BaseModel):
    """URL 连接器（M6-7）：从网页地址导入内容。"""
    url: str


class FeedbackBody(BaseModel):
    """问答反馈（M6-4）：rating 只收 1（赞）/-1（踩），note 可选补充说明。"""
    rating: Literal[1, -1]
    note: str | None = None


class EvalCaseIn(BaseModel):
    """评测用例（B）：question 必填，expect_doc（命中文档名）与 expect_text
    （命中块含此子串）至少给一个——两个都没有的用例永远判不中，直接拒收。"""
    question: str
    expect_doc: str | None = None
    expect_text: str | None = None

    @model_validator(mode="after")
    def _require_expectation(self):
        if not self.expect_doc and not self.expect_text:
            raise ValueError("用例必须给 expect_doc 或 expect_text 之一")
        return self


class EvalSetCreate(BaseModel):
    name: str
    cases: list[EvalCaseIn] = Field(min_length=1)


class EvalRunBody(BaseModel):
    top_k: int = Field(default=5, ge=1, le=50)


class ChatMessageIn(BaseModel):
    """OpenAI 兼容消息（M6-5）：content 允许纯文本或分段数组（多模态段忽略）。"""
    role: str
    content: str | list = ""


class ChatCompletionsBody(BaseModel):
    """POST /v1/chat/completions（M6-5）：model=kb_id 或唯一库名；
    top_k 为 KBase 扩展参数（标准客户端不传即用默认）。"""
    model: str
    messages: list[ChatMessageIn]
    stream: bool = False
    top_k: int = 5


class SearchBody(BaseModel):
    query: str
    top_k: int = 5
    debug: bool = False
    # M6-1.5 请求级策略覆盖（检索分析页试跑用，不落库）：None=按 KB 策略/
    # 全局默认。只能关闭已安装能力，开不出部署里没有的路（retriever 门控）。
    use_keyword: bool | None = None
    use_rerank: bool | None = None
    candidates: StrictInt | None = Field(default=None, ge=1, le=100)


class KBRetrievalBody(BaseModel):
    """KB 级检索策略（M6-1.5）：各键缺省=跟随全局默认（"通用方式"）。"""
    model_config = {"extra": "forbid"}

    hybrid: StrictBool | None = None      # 多路召回（关键词路）用不用
    rerank: StrictBool | None = None      # 重排用不用
    rewrite: Literal["off", "conditional", "always"] | None = None
    candidates: StrictInt | None = Field(default=None, ge=1, le=100)


class ConversationCreate(BaseModel):
    kb_id: str
    # M6-2 多库联合问答：额外绑定的库（含 kb_id 或不含都可，服务端会并入
    # 并去重）。缺省/空=单库会话（老行为）。
    kb_ids: list[str] | None = None


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
    # M6-1.5 KB 级检索策略段（缺省=全局默认）
    retrieval: KBRetrievalBody | None = None

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


class DocumentReview(BaseModel):
    """PUT /api/documents/{id}/review（F）：markdown=None 按落盘的识别结果
    原样入库；给了则以编辑稿为准（先写回 content.md 再入库）。"""
    markdown: str | None = None


class KbGrantsBody(BaseModel):
    """PUT /api/kb/{id}/grants（M6-3）：全量设置该库授权用户 id 集合
    （空列表=恢复公开，所有登录用户可见）。"""
    user_ids: list[str] = []


class ChunkUpdate(BaseModel):
    """PUT /api/chunks/{id}：enabled 启停与 text 编辑，至少给一项。
    叶子块编辑触发重嵌入+重索引；父块编辑仅落库（见 kbase/chunk_admin.py）。"""
    enabled: bool | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _check_any_field(self):
        if self.enabled is None and self.text is None:
            raise ValueError("enabled 与 text 至少提供一项")
        if self.text is not None and not self.text.strip():
            raise ValueError("text 不能为空白（要移除请用 enabled=false 停用）")
        return self


class ModelRefreshBody(BaseModel):
    """POST /api/settings/models/refresh：拉取某端点的模型清单。
    凭据解析顺序：provider_name（用已存 provider 的 base_url+密钥）>
    body 里的 api_key > api_key_env。表单"获取模型列表"场景走后两者
    （provider 还没建，用户刚在表单里填了 key）。"""
    base_url: str = ""
    api_key: str | None = None
    api_key_env: str = ""
    provider_name: str | None = None


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
