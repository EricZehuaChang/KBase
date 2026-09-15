from datetime import datetime

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    config: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON: 分块/增强配置
    # M6-3 库级权限：建库人 user_id（owner 永远可访问自己建的库）。
    # 老库/auth=off 建的库为 NULL（无 owner，只受 grant 规则约束）。
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class KbGrant(Base):
    """M6-3 库级授权：某用户对某知识库的访问权。
    语义（"不配就公开，一配就收紧"，与检索策略同哲学，向后兼容）：某 KB
    没有任何 grant 行=公开（所有登录用户可见）；一旦有 grant 行=仅 grant
    内 user_id + owner + admin 可见。principal 目前只到 user_id 级。"""
    __tablename__ = "kb_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
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
    # MonkeyOCR 无置信度信号，固定 1.0=未知；后续做质量门控时勿当作高置信
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # F VLM 深度识别：解析模式（NULL/"auto"=既有管道；"vlm"=满血视觉模型
    # 理解性转写，识别后停 pending_review 等人工校验确认才向量化）。
    # 重试按此模式重走，故必须落库。
    parse_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)


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
    enrich_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    # M5-2 引用溯源定位：该块在源文件中的页码（1 起）。仅文本层 PDF 摄取时
    # 回填（pdfminer 逐页文本前缀匹配，见 ingest/pipeline.py）；其他格式或
    # 匹配失败为 NULL——引用定位是尽力而为的增强，不是硬保证。
    page: Mapped[int | None] = mapped_column(nullable=True)
    # M6-1 chunk 运营开关：false=从向量库+关键词索引摘除（不可被检索），
    # 行保留可随时恢复。默认 true 与存量行为一致。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 方案卡 front matter 元数据（JSON dict，ztenith 流水线/通用）：整份文档
    # 所有 chunk 共享同一份，关键词路元数据后过滤与展示用；非方案卡文档 NULL。
    meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 块级版式元数据（JSON）：表格块存 {kind:"table", linearized:..., params:{...}}，
    # params 为结构化参数区间（kbase/params.py，供范围过滤）。摄取由
    # ingest/pipeline.py 写入、reindex.py 读取回填 payload。
    # （原注释写"当前摄取暂不写入"已过时——M6 表格版起就在写。）
    layout: Mapped[str | None] = mapped_column(Text, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)   # 主库（向后兼容/列表分组）
    # M6-2 多库联合问答：会话绑定的全部知识库 id（JSON 列表）。NULL/空=单库
    # （只用 kb_id，老会话行为不变）；非空时检索跨这些库联合召回。
    kb_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    # M5-1 F2：会话归属（鉴权改造前，会话是全局的，没有归属概念）。可空——
    # ①历史遗留会话没有归属，迁移时不倒推补全（谁都不该被动认领别人的老会话）；
    # ②API Key actor 发起的会话也存 NULL（API Key 不绑定具体用户，见
    # auth/deps.py 的 actor["user_id"] 取值注释）。归属过滤逻辑见
    # kbase/conversations.py 的 _visible_filter。
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conv_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    # 会话内单调递增的显式序列：消息排序唯一依据。created_at 仅作展示——
    # Windows 下 utcnow 时钟刻度 0.5~8ms，连续轮次可落在同一刻度，时间戳排序会乱。
    seq: Mapped[int] = mapped_column(default=0, index=True)
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
    # M5-2：管理端页面直配密钥（私有化内网部署场景）。非空时优先于
    # api_key_env 环境变量；对外 API 永不返回原文（只回 has_api_key+尾4位
    # 提示，见 providers_store.to_public）。
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # 账号资料邮箱（建号时维护）：当前用于资料记录与后续邮件找回密码；
    # 老库存量用户为 NULL
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))            # admin | editor | viewer
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 高级界面开关（仅对 viewer 生效）：控制使用端顶栏的模型选择/多库联查
    # 等高级菜单可见性。editor/admin 恒可见；viewer 默认简化界面，个别
    # 需要的用户由管理员打开。
    advanced_ui: Mapped[bool] = mapped_column(Boolean, default=False)
    # 账号级界面语言偏好（zh|en|ms；NULL=未设置，跟随客户端检测 localStorage/
    # 浏览器）。登录后前端据此覆盖本地检测；用户手动切语言时回写本列，实现
    # 跨设备一致的母语界面（P2-4）。老库存量用户补列后为 NULL=沿用检测。
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RoleDef(Base):
    """自定义角色（仅超管可维护）：name 即角色名（users.role 存的值），
    permissions 存权限词表子集的 JSON 数组（词表见 kbase/auth/roles.py）。
    内置角色 viewer/editor/admin/superadmin **不落库**——它们的权限是常量，
    避免"改内置角色权限"把系统改锁死。"""
    __tablename__ = "roles"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    label: Mapped[str] = mapped_column(String(100), default="")   # 展示名（可中文）
    permissions: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ShareLink(Base):
    """知识库免登录分享链接（对标 Dify WebApp/FastGPT 免登录窗模式）：
    token 即授权——持有链接者可对绑定库匿名问答；模型在建链接侧绑定
    （provider，空=系统默认），终端用户无任何配置项。撤销即失效。"""
    __tablename__ = "share_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    # 多库联查分享（复用 M6-2 retrieve_multi）：JSON 数组存全部检索库（首项
    # 即 kb_id 主库），NULL=单库（既有行为不变）。kb_id 保留为主库——链接
    # 归属/管理列表入口/失效判定仍以主库为准（主库删=链接死，与单库语义
    # 一致；联查副库删=静默缩小检索范围，链接不死）。
    kb_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # T10：有效期/访问口令/次数上限与访问计数。四列老库补列一律 NULL，读取端
    # 统一按"NULL=不限"解释（与 T09 api_keys 同一约定——SQLite ALTER 没法带
    # DEFAULT 回填存量行，语义只能由读取端给）：expires_at=NULL 永不过期；
    # password_hash=NULL 免口令；max_visits=NULL 不限次数；visit_count=NULL
    # 视作 0（计次语句用 COALESCE 归一，见 api/routes/share.py）。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # bcrypt 哈希（kbase/auth/security.py，与账号口令同一套），明文永不落库；
    # 校验用请求头 X-Share-Password（不放 query——query 会进访问日志）。
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_visits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 只有免登录问答计次（meta 取库名/附图直链不计——访客看一眼不算访问）。
    visit_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(20), index=True)   # 前8字符明文，列表展示用
    key_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256 hex
    role: Mapped[str] = mapped_column(String(20))
    # 吊销=不可恢复（DELETE 端点只置这一位）；disabled=可恢复的临时停用。
    # 两者在 Bearer 通道上同样是 401，但语义与操作路径不同（见 routes/admin.py
    # 的 PATCH 与 DELETE）。
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    # T09：恢复性停用开关。NULL（老库补列）=未停用——读取端按"仅显式 True 才
    # 拒"解释（SQLite ALTER 无法带 DEFAULT false 回填存量行）。
    disabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 库级 scope（JSON 数组=允许访问的 kb_id 白名单；NULL=不限）。scope 由
    # 服务端强制：受限 key 越权查询静默返回空集（不报错不提示，防探测）。
    scope_kb_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    # T09：NULL=永不过期（老库/未设置）。naive UTC，与 created_at 同口径。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # T09：最近使用时间。写侧节流到每分钟至多一次（见 kbase/ratelimit.py），
    # 不在每个请求上写库。
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # T09：来源 IP 白名单（JSON 数组，精确 IP 或 CIDR，最多 20 条）；
    # NULL=不限来源。校验在写入侧（api/schemas.py），匹配在 kbase/ratelimit.py。
    ip_allow: Mapped[str | None] = mapped_column(Text, nullable=True)
    # T09：每分钟请求上限 / 每日请求上限；NULL=不限。进程内滑窗+逐日计数判定
    # （kbase/ratelimit.py），lite 单进程精确、standard 多 worker 为每进程近似。
    rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ApiKeyUsageDaily(Base):
    """T09：API Key 逐日用量（(key_id, day) 主键，day=UTC 日期 YYYY-MM-DD）。

    只经管理端鉴权接口读出（GET /api/settings/api-keys/{id}/usage）；
    无鉴权的 /metrics 绝不暴露任何 per-key 数据（见 kbase/metrics.py）。
    tokens_estimated=false 表示 token 数是上游回传的真实 usage；true=上游没
    回传（端点不认 stream_options.include_usage 等）时的字符数兜底估算
    ——见 kbase/ratelimit.py 与 kbase/api/routes/openai_compat.py。"""
    __tablename__ = "api_key_usage_daily"
    key_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tokens_estimated: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(100))          # 用户名或 api key name
    action: Mapped[str] = mapped_column(String(100))
    resource: Mapped[str | None] = mapped_column(String(500), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON，截断
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DocumentImage(Base):
    """文档内嵌图片索引（多模态回答第一期）：文本层 PDF 摄取时提取的
    插图，按 (doc_id, page) 关联——回答引用命中某页时，该页图片随
    citations 一起返回，前端在答案下方渲染缩略图。filename 是相对
    files/{doc_id}/images/ 的纯文件名（服务端点凭它回文件）。"""
    __tablename__ = "document_images"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(36), index=True)
    # 锚点二选一：PDF=页码（1 起）；docx 无页概念用 0 作哨兵、锚在 heading
    #（避免 SQLite 改列可空的重建成本，0 与真实页码永不冲突）。
    page: Mapped[int] = mapped_column(Integer, index=True)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str] = mapped_column(String(200))
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MessageFeedback(Base):
    """问答反馈（M6-4）：对助手消息点赞/点踩+可选备注。一条消息至多一条
    反馈（重复提交覆盖），差评清单喂运营看板定位坏答案。"""
    __tablename__ = "message_feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    conv_id: Mapped[str] = mapped_column(String(36), index=True)
    rating: Mapped[int] = mapped_column(Integer)              # 1=赞 | -1=踩
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EvalSet(Base):
    """检索评测集（B 评测回归）：一组"问题+期望命中"用例，绑定单库。
    cases 存 JSON 数组 [{question, expect_doc?, expect_text?}, ...]——
    用例量级是几十到几百条，整包读写，不值得拆行表。"""
    __tablename__ = "eval_sets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(200))
    cases: Mapped[str] = mapped_column(Text)                  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EvalRun(Base):
    """一次评测回归的结果快照：整体指标 + 逐用例明细（JSON），
    历史对比就是按 created_at 排的多行 run。"""
    __tablename__ = "eval_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    set_id: Mapped[str] = mapped_column(String(36), index=True)
    top_k: Mapped[int] = mapped_column(Integer, default=5)
    hit_rate: Mapped[float] = mapped_column(Float)            # hit@k
    mrr: Mapped[float] = mapped_column(Float)
    total: Mapped[int] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text)                 # JSON 逐用例
    # T15 答案级评测三列（迁移已在 kbase/migrations.py 登记）：mode="retrieval"
    # |"answer"（老库存量行补列后为 NULL，读取端一律按 retrieval 解释，与升级前
    # 行为一致）；answer_score=逐用例裁判分（0~1）的平均值，NULL=没跑过答案级
    # 判分；judge_provider 记下当时用的裁判 provider 名，避免"换了裁判模型分数
    # 变了"无从追查。
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    answer_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    judge_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Connector(Base):
    """同步连接器实例（对标清单#3）：某知识库绑定一个外部数据源，由调度器
    按 interval_minutes 定时增量同步——把"一次性导入"升级为"持续同步的
    活知识库"。type 一期只有 "feishu"（飞书 wiki），Notion/Confluence 等
    后续类型在 kbase/connectors.py 的 _SOURCE_TYPES 注册即可复用全套
    增量/清单 diff/调度逻辑。

    同步状态直接存本行（last_sync_*）而不复用 jobs 表：jobs 是 kb 级
    长任务（步骤模型+产物路径），与逐文档 diff 循环不匹配，且定时同步会
    把 jobs 列表刷屏。前端连接器列表轮询本行即可。"""
    __tablename__ = "connectors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(20))             # "feishu"
    name: Mapped[str] = mapped_column(String(200), default="")
    config: Mapped[str] = mapped_column(Text)                 # JSON 类型专属（飞书: {"source": url|space_id}）
    # 停用=调度器跳过；手动"立即同步"不受影响（排查/补数场景仍可用）。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 定时间隔（分钟）；0=仅手动同步。默认一天一次——wiki 类源的变更频率
    # 与 API 配额（整树遍历逐层调用）的平衡点。
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    # 镜像语义：源侧删除的文档本地也删（活知识库承诺——源头删掉的错误
    # 文档不该留在库里继续污染答案）。可关（本地保留成普通文档）。
    prune: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # NULL(从未同步) | running | done | done_with_errors | failed
    last_sync_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_stats: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {added,updated,skipped,pruned,failed}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConnectorDoc(Base):
    """连接器→本地文档映射 + 增量指纹。source_key=源侧稳定标识（飞书=
    obj_token）；fingerprint=源侧版本信号（飞书=obj_edit_time，变了才拉
    正文）；content_hash=转换后 markdown 的 sha256（与 Document.content_hash
    同算法）——版本信号变但内容没变（权限类改动碰 edit_time）只刷新
    fingerprint，不重摄取。"""
    __tablename__ = "connector_docs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(36), index=True)
    source_key: Mapped[str] = mapped_column(String(200), index=True)
    doc_id: Mapped[str] = mapped_column(String(36), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(500), default="")   # 源侧标题（排障展示）
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(20))            # proposal | digest
    # pending -> running -> done | done_with_errors | failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    params: Mapped[str] = mapped_column(Text)                 # JSON
    progress: Mapped[str | None] = mapped_column(Text, nullable=True)      # JSON
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Translation(Base):
    """i18n 覆盖表(方案 A):只存运营在管理端「多语言」页改过的 UI 文案
    译文。译文基线在前端 locales/*.json（机翻+校准，随版本走）；这里的行
    按 (lang, key) 覆盖基线,前端 mergeLocaleMessage 合并(DB 优先)。空表
    =全用基线;删某行=该 key 回落基线。lang 不做后端白名单——语言清单以
    前端 languages.ts 为单一事实源,加新语言零改后端。"""
    __tablename__ = "translations"
    lang: Mapped[str] = mapped_column(String(10), primary_key=True)   # zh/en/ms/...
    key: Mapped[str] = mapped_column(String(200), primary_key=True)   # 语义点分 key，如 kb.create
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class QaOutcome(Base):
    """问答结果归因（T12）：每一次问答落一行，供运营看板按"桶"归因。

    为什么独立成表而不是往 audit_logs 塞：审计行只留 100 字问题前缀、没有会话
    与消息关联，也没法按桶聚合；而"用户问了什么我们答不上"是知识缺口的第一手
    信号，需要能下钻到具体问答与当次 citations、能按时间/渠道/库筛、能导出。

    bucket 取值写死在 kbase/qa_outcomes.py 的模块常量里（不是自由文本）：
    empty_retrieval（检索为空）/ below_threshold（检索到了但全低于阈值、无可用
    依据）/ scope_denied（越权静默空集）/ answered（正常作答）。
    downvoted 不单列桶——由 feedback=-1 叠加表示（同一行既可 answered 又被点踩）。
    """
    __tablename__ = "qa_outcomes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    # 入口渠道：web（登录态会话/直问）/ share（免登录分享）/ v1（OpenAI 兼容）
    # / feishu（飞书机器人）/ mcp。各入口的归因行要能分开看——同一批问题从
    # 不同入口进来，缺口含义不同。
    channel: Mapped[str] = mapped_column(String(20), index=True)
    kb_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 多库联查时记全部库（JSON 数组）；单库为 NULL（与 conversations.kb_ids 同约定）
    kb_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    conv_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 会话问答才有的助手消息 id：append_round 生成后可回填（T12 改造点）
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bucket: Mapped[str] = mapped_column(String(30), index=True)
    retrieved_count: Mapped[int] = mapped_column(Integer, default=0)
    usable_count: Mapped[int] = mapped_column(Integer, default=0)
    # 本轮最高检索分（可为 NULL：检索为空时没有分数）。用它判断"差一点就够"
    top_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 问题原文（上限 2000 字）：比审计的 100 字前缀长得多，够用来提标问
    question: Mapped[str] = mapped_column(Text)
    # 用户反馈叠加：NULL=未评，1=赞，-1=踩（T12 与 feedback.upsert_feedback 联动）
    feedback: Mapped[int | None] = mapped_column(Integer, nullable=True)


class StandardAnswer(Base):
    """标问库（T13）：人工策展的"标准问题 + 标准答案"，**必须先过人工审核**。

    红线（方案已定案）：审核通过的标问**只用于两处**——回灌评测集，以及可选地
    作为问答型文档走正常摄取管道（这样仍可溯源、仍受 ACL 约束）。
    **严禁任何"相似度命中就绕过检索直接返回答案"的代码路径**，这一条不做。
    """
    __tablename__ = "standard_answers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    question: Mapped[str] = mapped_column(Text)
    # 相似问法（JSON 数组）：标问的价值一半在这里——同一意图的多种问法
    similar_questions: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # pending_review -> approved | rejected（状态机照抄 documents 的人工审核：
    # 非 pending_review 再审核返回 409，见 routes/kb.py 的 review_document）
    status: Mapped[str] = mapped_column(String(20), default="pending_review", index=True)
    # 来源：ops（运营看板一键提取）/ mcp（Agent 提交）/ manual（手工录入）
    source: Mapped[str] = mapped_column(String(20), default="manual")
    # 由哪条归因记录提取而来（T12 的 qa_outcomes.id），手工录入为 NULL
    source_outcome_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ImportBatch(Base):
    """批量导入批次（T18）：一次 CLI 灌库运行在数据库里留一行。

    为什么要有这张表：`kbase/bulk_import.py` 是清单驱动（JSONL 追加）的 CLI，
    它的续传状态只在**服务器本地文件**里——交付后现场问"昨天那轮灌库跑了没、
    成功多少、哪些文件失败"，只能登机器 tail 清单文件；清单文件会被重跑追加、
    会被误删，不是可审计的记录。本表把"哪次运行、谁发起、什么时候开始/结束、
    最终计数"落库，管理端知识库详情页的只读「导入记录」tab 直接读它。

    只有 CLI 会写本表（`kbase/bulk_import.py`），HTTP 侧**只读**：刻意不提供
    任何"从网页触发导入"的端点——BackgroundTasks 进程内任务重启即丢，
    万级文件的首轮灌库必须能断点续传（理由见 bulk_import.py 的文件头）。

    状态机（取值写死在 kbase/bulk_import.py 的模块常量里，不是自由文本）：
    running -> done | done_with_errors | failed | interrupted。
    前四者由 CLI 正常收尾时写入；interrupted 是"进程没能自己收尾"的现场
    （Ctrl-C/被杀/掉电），由下一次启动同一清单的运行或读接口的一致性回收
    写入——否则批次会永久停在 running，变成"看着在跑其实早就死了"的支持负担。
    """
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    # 相对 data_dir 的清单路径（如 import-kb1.jsonl）。**不存绝对路径**：
    # 读接口按 data_dir 做包含性校验后才会打开它，绝对路径会让"路径穿越"
    # 无从设防（见 bulk_import.resolve_manifest_path）。
    manifest_path: Mapped[str] = mapped_column(String(500))
    # 发起人（CLI 上的操作系统用户名；取不到时 NULL，不阻塞导入）
    started_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 冗余列（counts 里也有 status）：列表页按它排序/过滤，不让读接口为了
    # 一个状态把每行的 JSON 都解析一遍；写侧两个地方同时写，不会漂移。
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    # NULL=这一行还在 running（收尾时写入）。崩溃残留的 running 行由
    # reconcile_stale_running 回收成 interrupted。
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 存活心跳（运行中每 ~60 秒写一次，见 bulk_import._heartbeat）。判"这行是
    # 不是死了"必须用**心跳**而不是 started_at：大目录（数万文件）跑几小时很
    # 正常，拿起始时间判久会把正在跑的批次误判成崩溃。NULL=老库补列或还没跳。
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # JSON：{status, total, pending, done, failed, elapsed_s, workers,
    # parse_mode, dir, interrupted, exit_code, error}——运行结束才完整；
    # 逐文件明细不在这里（那在清单文件里，本表只做批次汇总，避免万行级
    # JSON 撑爆一行）。
    counts: Mapped[str | None] = mapped_column(Text, nullable=True)


class ChannelIdentity(Base):
    """渠道身份映射（T19）：外部渠道的用户 → KBase 用户。

    为什么需要这张表：渠道入口（飞书机器人，将来钉钉/企微同构）进来的提问者
    在 KBase 里**没有身份**——飞书事件里只有一个 open_id（应用内唯一、跨应用
    不同），拿它当 actor 既认不出人也没法做库级授权，机器人只能用一个笼统的
    "feishu-bot" 身份查库。把外部 id 映射到 KBase 用户后，同一句提问由不同的人
    发出会**得到不同的结果**（有权的人查得到、无权的人查不到），这与登录态问答
    的权限语义完全对齐——渠道适配层不该是权限旁路。

    唯一约束 (channel, external_user_id)：一个外部账号在一个渠道里只能绑一个
    KBase 用户（改绑就是覆盖那一行，不留双份——否则"这人到底是谁"有两个答案）。
    反向不唯一：一个 KBase 用户可以绑多个外部账号（同一个人在不同群里的两个
    飞书号），这是允许的。

    external_user_id 的取值语义由渠道决定（飞书=事件 sender.sender_id.open_id，
    应用内唯一）；本表只当它是渠道内的不透明字符串，不做任何格式解析。
    user_id 指向 users.id（不加数据库外键：SQLite 默认不强制外键，加了也只是
    装饰）：用户被删除后凭这行查不到用户，resolve_actor 即按未映射处理（回落
    默认策略，不会越权），脏行由 channels.purge_orphans 在清单读取时顺手清理。
    """
    __tablename__ = "channel_identities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 渠道名：feishu（现役）/ 将来 dingtalk、wecom。取值写死在渠道注册表里
    # （kbase/channels/core.py 的 CHANNELS），不是自由文本。
    channel: Mapped[str] = mapped_column(String(20), index=True)
    external_user_id: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 复合唯一：同一渠道内一个外部账号只能绑一个 KBase 用户（跨渠道互不影响，
    # 同一个人在不同渠道是两条独立的绑定）
    __table_args__ = (UniqueConstraint("channel", "external_user_id",
                                       name="uq_channel_identity"),)
