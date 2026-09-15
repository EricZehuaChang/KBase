from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class EmbedderConfig(BaseModel):
    name: str = "bge-local"
    model: str = "BAAI/bge-m3"
    endpoint: str | None = None   # name="tei" 时必填：TEI 服务地址


class EmbedderOption(BaseModel):
    """cfg.embedders 清单中的一个 KB 级可选向量模型（M5-2）。

    id 是 KB 绑定引用的稳定标识（写进 KnowledgeBase.config JSON），**改名等于
    让已绑定的库失联**——上线后只增不改。"default" 为保留 id，指默认 embedder。
    plugin: bge-local（进程内）| tei（自建推理服务）| openai-embed（云 API）。"""
    id: str
    plugin: str
    model: str | None = None
    endpoint: str | None = None    # tei 用
    base_url: str | None = None    # openai-embed 用
    api_key_env: str = ""          # openai-embed 用；密钥不进配置文件
    batch_size: int | None = None  # openai-embed 用；默认 10（DashScope 上限）


class DBConfig(BaseModel):
    # {data_dir} 占位符由 create_app 替换成实际路径，保持 sqlite 默认语义
    # 与改造前的 f"sqlite:///{cfg.data_dir}/kbase.sqlite" 字节级一致；
    # postgresql+psycopg:// 等其他 URL 原样透传，不做占位替换。
    url: str = "sqlite:///{data_dir}/kbase.sqlite"
    # T05/G03：PG 密码走环境变量的**变量名**（与 provider 的 api_key_env 同一
    # 写法：配置文件只写变量名，密钥值永不进仓库）。设置后 resolve_db_url 用
    # sqlalchemy.engine.make_url(url).set(password=...) 渲染——make_url 负责
    # 转义（密码含 @ : / % { 等字符不会再拼出坏 URL，这是"直接字符串替换"
    # 做不到的）。不设时 url 原样使用，行为与改造前一致。
    password_env: str | None = None


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
    # M4-2 H6.5：单次查询的重排是一次同步网络调用（TEI 交叉编码器），单卡
    # GPU 的推理吞吐有物理上限（H6 压测实测约 260ms/次，与批大小/并发无关）。
    # max_concurrency 限制同时在途的重排调用数——超过这个数的查询不排队，
    # 直接跳过重排、降级为融合排序（见 kbase/rag/retriever.py 的
    # threading.BoundedSemaphore 用法）。8 是经验默认值：正常负载下几乎不会
    # 触碰到这个上限（不引入降级），100 并发这种极端场景下能把多余请求的
    # 尾延迟从"排队等 GPU"降到"融合排序的毫秒级"，用可控的 shed 率换 P95。
    max_concurrency: int = 8


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


class VLMParseConfig(BaseModel):
    # F 满血 VLM 深度识别（上传时用户显式选择的模式）：
    # provider=None 表示用当前活跃 provider（应配成视觉模型才有效）；
    # 建议单独配一个视觉模型 provider 名（如 glm-5.2 视觉版/qwen3.7-max）。
    provider: str | None = None


class OCRConfig(BaseModel):
    # enabled=False（默认）：不创建 OCR 后端，扫描件/图片直接判 failed（M1 行为）。
    # backend 两种实现：
    # - monkey-http：MonkeyOCR 自建服务（kbase/plugins/ocr/monkey_http.py）
    # - glm-ocr：智谱云 layout_parsing / vLLM 本地同模型（kbase/plugins/ocr/glm_http.py）
    # endpoint 留空用各后端自己的默认值（monkey-http=http://localhost:7861，
    # glm-ocr=智谱官方云端点）；显式配置则覆盖。
    enabled: bool = False
    backend: str = "monkey-http"
    endpoint: str = ""
    # 仅 glm-ocr 用：密钥环境变量名与模型名（密钥不进配置文件，与 LLM 同规矩）
    api_key_env: str = "ZHIPU_API_KEY"
    model: str = "glm-ocr"


class ServerConfig(BaseModel):
    # M4-2 H7（H6.5 发现的下一层瓶颈）：Starlette/AnyIO 的 run_in_threadpool
    # 默认线程池容量是 40（anyio.to_thread.current_default_thread_limiter().
    # total_tokens），/api/kb/{id}/search 等端点的检索全流程（embed+dense+
    # keyword+DB 组装）都经这个线程池执行。100 并发压测下，重排信号量把
    # TEI 侧排队从 2.8s 压到 0.3~0.4s 后，线程池槽位排队成为新的主导延迟
    # ——40 个线程槽位是与重排完全独立、且发生在请求路径更早阶段的人为上限。
    # 默认值 40 与 AnyIO 库默认一致，不配置=零行为变化。注意：4 vCPU 参考机
    # 实测调到 120 反而使 P95 回退约 20%（线程数超过 CPU 真并行能力后只增加
    # GIL/调度争抢，见 loadtest/report-standard.md 线程池调优后一节）——仅当
    # 部署机 vCPU 充裕（≥16）且压测验证有收益时才调大。
    threadpool_size: int = 40


class SsoConfig(BaseModel):
    """企业 SSO（M6-8，OIDC 授权码流）。enabled=false（默认）时零行为变化。
    client_secret 走环境变量（密钥不进配置文件，与 ProviderConfig 同规矩）。"""
    enabled: bool = False
    issuer: str = ""                     # 如 https://idp.corp.com/realms/main
    client_id: str = ""
    client_secret_env: str = "KBASE_OIDC_CLIENT_SECRET"
    # IdP 回调后新用户的默认角色；已有同名用户直接复用其现有角色
    default_role: str = "viewer"


class LoginGuardConfig(BaseModel):
    """登录/口令端点的人机闸（T11）：窗口内失败次数达阈值 → 429 + Retry-After，
    退避时长随失败档位指数增长。

    计数源是 audit_logs 表（见 kbase/ratelimit.py 的 LoginGuard），不新建表——
    因此和 T09 的进程内限流不同，**多 worker/多副本下计数一致**（都在同一个
    库里），重启也不丢锁。四个键都可在部署侧按规模调；默认 5 次 / 15 分钟是
    "正常人连错 5 次之前基本都会去走忘记密码"的量级，而在线爆破会被立刻按住。
    """
    # 窗口内同一用户名 / 同一 IP 的失败次数阈值，达到（>=）即锁。
    # 必须 >=1：0 会让"0 >= 0"恒真，等于把登录永久锁死。
    max_attempts: int = Field(default=5, ge=1)
    # 计数窗口（秒），同时是锁定时长的上界——窗口内所有失败滑出后锁自动解开
    # （见 LoginGuard.check），所以 Retry-After 报得比它还大只是虚报。
    window_seconds: int = Field(default=900, ge=1)
    # 退避基数（秒）：刚好达阈值时 Retry-After = base，之后每多一档（一次失败
    # 或一次被拒）翻一倍。
    backoff_base_seconds: int = Field(default=30, ge=1)
    # 退避上限（秒）：指数增长到此为止（默认与窗口同长，即最多让客户端等一个
    # 窗口——等满一个窗口后锁必然已解开）。
    backoff_max_seconds: int = Field(default=900, ge=1)


class AnswerJudgeConfig(BaseModel):
    """答案级评测（T15）：是否需要跑"检索→生成→裁判打分"的重回归。

    enabled=False（默认）时**零行为变化**：评测集回归仍只跑检索（hit@k/MRR），
    一个 LLM 调用都不会发（判分路径整段短路，见 api/routes/evals.py 与
    jobs/eval_answer.py）——既有部署升级后行为字节级不变，这也正是本开关
    默认关闭的原因：答案级判分按用例数计费、耗时以分钟计，且它评的是"当前
    生成配置"而不是"检索配置"，不该被升级悄悄打开。

    provider=None 表示用 llm.active（与 rewrite/enrich 的 provider 语义一致）；
    生产建议**显式**指一个便宜档模型（如 qwen-turbo/deepseek-v3 这类）——
    裁判是"读参考答案+模型答案给 0~1 分"的结构化任务，不需要旗舰模型的
    推理深度，用旗舰评全量用例纯属烧钱（judge_provider 会落进 eval_runs，
    报告里标明用的哪个模型，避免"换了模型分数变了"无从追查）。"""
    enabled: bool = False
    provider: str | None = None


class EvalsConfig(BaseModel):
    """评测域配置。目前只有 answer_judge 一项：答案级判分（T15）。"""
    answer_judge: AnswerJudgeConfig = Field(default_factory=AnswerJudgeConfig)


class AppConfig(BaseModel):
    data_dir: Path = Path("./data")
    sso: SsoConfig = Field(default_factory=SsoConfig)
    # T15：答案级评测开关（默认关，见 AnswerJudgeConfig）。
    evals: EvalsConfig = Field(default_factory=EvalsConfig)
    # T11：登录/忘记密码/重置密码的失败锁定（默认值与改造前"只记审计不拦截"
    # 相比是行为变化，阈值给得足够宽，正常使用不会撞上）。
    login_guard: LoginGuardConfig = Field(default_factory=LoginGuardConfig)
    db: DBConfig = Field(default_factory=DBConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    # KB 级可选向量模型清单（M5-2）：建库时可从 [default]+embedders 中选一个
    # 绑定；空清单=只有默认模型可选（改造前行为）。
    embedders: list[EmbedderOption] = Field(default_factory=list)
    vectorstore: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    enrich: EnrichConfig = Field(default_factory=EnrichConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    vlm_parse: VLMParseConfig = Field(default_factory=VLMParseConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm: LLMConfig

    def get_provider(self, name: str) -> ProviderConfig:
        for p in self.llm.providers:
            if p.name == name:
                return p
        raise KeyError(f"LLM provider 未配置: {name}")

    @model_validator(mode="after")
    def _check_embedder_option_ids(self) -> "AppConfig":
        ids = [o.id for o in self.embedders]
        if len(ids) != len(set(ids)):
            raise ValueError(f"embedders 清单存在重复 id: {ids}")
        if "default" in ids:
            raise ValueError('embedders 清单不得使用保留 id "default"（它指默认 embedder）')
        return self

    @model_validator(mode="after")
    def _check_answer_judge_provider(self) -> "AppConfig":
        """T15：裁判 provider 指到未配置的 provider 上，要在**启动期**报错。

        放到运行期才炸的代价是：任务建出来了、逐用例生成（真金白银的 LLM
        调用）全部跑完，最后落快照时报"provider 未配置"——白花钱还拿不到分。
        只在 enabled=True 时校验：关着的时候 provider 字段只是留给部署侧预填
        的注释性配置，不该因为写了个还没配的模型名导致服务起不来（默认关+
        provider 预填是最自然的写法，见 config/kbase.standard.yaml）。"""
        name = self.evals.answer_judge.provider
        if self.evals.answer_judge.enabled and name is not None:
            if name not in {p.name for p in self.llm.providers}:
                raise ValueError(
                    f"evals.answer_judge.provider 指向未配置的 provider: {name}，"
                    f"已配置: {sorted(p.name for p in self.llm.providers)}")
        return self


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
    不做占位替换"的约定。

    T05/G03：db.password_env 非空时，用环境变量里的值渲染 URL 的密码段。
    走 sqlalchemy.engine.make_url 而不是字符串拼接——密码含 @ : / % { } 等
    字符时，字符串拼接会拼出解析错误的 URL（密码被当成 host/端口），
    make_url 会正确转义并保留其余部分。变量缺失**必须报错**（不静默退回
    字面 PASSWORD）：静默失败会让连接用错密码，症状是部署期莫名其妙的
    auth failed，而不明说"环境变量没设"。
    """
    url = cfg.db.url
    if "{data_dir}" in url:
        url = url.format(data_dir=str(cfg.data_dir))
    if not cfg.db.password_env:
        return url
    import os

    from sqlalchemy.engine import make_url

    password = os.environ.get(cfg.db.password_env)
    if password is None:
        raise RuntimeError(
            f"数据库密码环境变量 {cfg.db.password_env} 未设置"
            f"（配置项 db.password_env 指定了它）；"
            f"请在部署环境注入该变量，或从配置里删掉 db.password_env")
    return make_url(url).set(password=password).render_as_string(
        hide_password=False)
