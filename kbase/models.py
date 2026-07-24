from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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
    # M6-6 预埋：GLM-OCR layout_details 的版式元数据（JSON：bbox_2d/label/
    # 表格结构等），本迁移波一并加列避免二次迁移；当前摄取暂不写入。
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


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(20), index=True)   # 前8字符明文，列表展示用
    key_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256 hex
    role: Mapped[str] = mapped_column(String(20))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
