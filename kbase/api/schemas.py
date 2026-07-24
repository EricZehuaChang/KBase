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


class RebindEmbedderBody(BaseModel):
    """换绑向量模型（重操作：全库向量作废，按新模型重嵌入重建）。"""
    embedder: str


class EmbedderKeyBody(BaseModel):
    """向量模型选项的页面密钥（DB 覆盖 > api_key_env）。"""
    api_key: str = Field(min_length=1)


class UrlImportBody(BaseModel):
    """URL 连接器（M6-7）：从网页地址导入内容。"""
    url: str


class SmtpSettingsBody(BaseModel):
    """发件箱配置（账号通知/找回密码等系统邮件）。password=None 保留旧值。"""
    host: str = Field(min_length=1)
    port: int = Field(default=465, ge=1, le=65535)
    user: str = Field(min_length=1)
    password: str | None = None
    from_addr: str = ""
    from_name: str = "KBase"


class SmtpTestBody(BaseModel):
    to: str = Field(min_length=3)


class FeishuCredentialsBody(BaseModel):
    """飞书自建应用凭据（页面维护）。"""
    app_id: str = Field(min_length=1)
    app_secret: str = Field(min_length=1)


class FeishuBotSettingsBody(BaseModel):
    """飞书群机器人配置：token/encrypt_key 传 None=保留旧值（只写不回显）；
    kb_id 必填（机器人回答依据的知识库）；provider 空=系统默认模型。"""
    verification_token: str | None = None
    encrypt_key: str | None = None
    kb_id: str = Field(min_length=1)
    provider: str | None = None


class FeishuImportBody(BaseModel):
    """飞书知识库导入：source 收 wiki 节点链接（导该子树）或 space_id
    （导整个空间）。"""
    source: str = Field(min_length=1)


class ConnectorCreate(BaseModel):
    """创建同步连接器（对标#3）：type 一期只有 feishu；source 同一次性
    导入语义（wiki 链接=该子树 / space_id=整空间）；interval_minutes=0
    表示仅手动同步，上限 30 天。"""
    type: Literal["feishu"]
    source: str = Field(min_length=1)
    name: str = ""
    interval_minutes: int = Field(default=1440, ge=0, le=43200)
    prune: bool = True


class ConnectorUpdate(BaseModel):
    """更新连接器运行参数（源不可改——换源=删旧建新，映射指纹全部失效）。"""
    model_config = {"extra": "forbid"}

    name: str | None = None
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=0, le=43200)
    prune: bool | None = None


class TranslationPut(BaseModel):
    """管理端「多语言」编辑:写某语言某 key 的译文覆盖。value 空串=删除
    覆盖、回落基线(撤销修改用回机翻底)。lang 不做枚举白名单(语言清单
    以前端 languages.ts 为准,加语言零改后端)。"""
    lang: str = Field(min_length=1, max_length=10)
    key: str = Field(min_length=1, max_length=200)
    value: str


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
    # 对标#8 多库联查权重：该库命中分数的乘法因子（1.0=中性）。
    union_weight: float | None = Field(default=None, ge=0.1, le=10.0)

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
    email: str | None = None
    advanced_ui: bool | None = None   # viewer 高级界面开关（缺省=关）


class UserUpdate(BaseModel):
    role: Role | None = None
    disabled: bool | None = None
    password: str | None = None
    email: str | None = None      # 传空串=清除邮箱
    advanced_ui: bool | None = None   # viewer 高级界面开关


class ChangePasswordBody(BaseModel):
    """登录用户自助改密：必须携带旧密码复核（防止离席被人改密顶号）。"""
    old_password: str
    new_password: str = Field(min_length=6)


class ProfileBody(BaseModel):
    """登录用户维护自己的资料（目前只有邮箱——用于忘记密码重置）。"""
    email: str = Field(min_length=3)


class LanguageBody(BaseModel):
    """账号级界面语言偏好（P2-4）。code 为 BCP-47 简码（zh|en|ms），后端按
    SUPPORTED_LANGUAGES 白名单校验；与 email 独立成端点，语言切换不牵动邮箱。"""
    language: str = Field(min_length=2, max_length=10)


class ShareLinkCreate(BaseModel):
    """建免登录分享链接：name 备注用；provider 绑定回答模型（None=系统默认，
    对标 Dify/FastGPT——模型在建链接侧配置，终端用户无感）；extra_kb_ids
    为联查副库（与路径主库合并去重，匿名问答跨全部库散射检索，M6-2 复用）。"""
    name: str = ""
    provider: str | None = None
    extra_kb_ids: list[str] = []


class ForgotBody(BaseModel):
    """忘记密码：account 收用户名或邮箱。"""
    account: str = Field(min_length=1)


class ResetPasswordBody(BaseModel):
    token: str = Field(min_length=10)
    new_password: str = Field(min_length=6)
