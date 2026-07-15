"""Embedder 工厂与 KB 级解析池。

KB 级向量模型（M5-2）的核心不变量：**一个 KB 只绑定一个向量模型**——不同
模型的向量空间互不可比，混存进同一 collection 会让检索打分失效。绑定发生在
建库时（KB.config JSON 的 "embedder" 键 = cfg.embedders 里某项的 id），
之后所有摄取与查询都按该 id 解析同一个 embedder 实例；未绑定的 KB（含
存量老库）一律走默认 embedder（cfg.embedder），行为与改造前完全一致。

EmbedderPool 是惰性单例缓存：bge-local 加载慢、openai-embed 要求密钥就绪，
都推迟到第一个真正使用该选项的 KB 摄取/查询时才构建。
"""
import json

from kbase.plugins.registry import registry

DEFAULT_EMBEDDER_ID = "default"


def build_default_embedder(cfg):
    """按 cfg.embedder 构建默认 embedder（原 create_app 启动路径，逻辑不变）。"""
    if cfg.embedder.name == "tei":
        import kbase.plugins.embedders.tei  # noqa: F401
        if not cfg.embedder.endpoint:
            raise ValueError("embedder.name=tei 但未配置 embedder.endpoint")
        return registry.create("embedder", "tei", endpoint=cfg.embedder.endpoint)
    # bge_local 依赖 local-embed extra 且加载慢，仅在真正需要时 import 注册
    import kbase.plugins.embedders.bge_local  # noqa: F401
    return registry.create("embedder", cfg.embedder.name, model=cfg.embedder.model)


def build_option_embedder(opt, api_key: str | None = None):
    """按 cfg.embedders 里的一项（EmbedderOption）构建 embedder 实例。
    api_key：页面配置的 DB 覆盖密钥（优先于 api_key_env），仅 openai-embed 用。"""
    if opt.plugin == "tei":
        import kbase.plugins.embedders.tei  # noqa: F401
        if not opt.endpoint:
            raise ValueError(f"embedder 选项 {opt.id}: plugin=tei 必须配置 endpoint")
        return registry.create("embedder", "tei", endpoint=opt.endpoint)
    if opt.plugin == "openai-embed":
        import kbase.plugins.embedders.openai_compat  # noqa: F401
        if not opt.base_url or not opt.model:
            raise ValueError(
                f"embedder 选项 {opt.id}: plugin=openai-embed 必须配置 base_url 与 model")
        kwargs = {"base_url": opt.base_url, "model": opt.model,
                  "api_key_env": opt.api_key_env}
        if api_key:
            kwargs["api_key"] = api_key
        if opt.batch_size is not None:
            kwargs["batch_size"] = opt.batch_size
        return registry.create("embedder", "openai-embed", **kwargs)
    if opt.plugin == "bge-local":
        import kbase.plugins.embedders.bge_local  # noqa: F401
        return registry.create("embedder", "bge-local",
                               model=opt.model or "BAAI/bge-m3")
    raise ValueError(f"embedder 选项 {opt.id}: 未知插件 {opt.plugin!r}，"
                     f"支持: bge-local / tei / openai-embed")


class EmbedderPool:
    """default + cfg.embedders 各选项的惰性单例缓存（进程内共享）。"""

    def __init__(self, cfg, default_embedder=None, key_resolver=None):
        """key_resolver: (option_id) -> str | None，页面配置的 DB 覆盖密钥
        （优先于选项的 api_key_env）；None=不启用覆盖（行为与改造前一致）。"""
        self._cfg = cfg
        self._options = {opt.id: opt for opt in cfg.embedders}
        self._cache: dict = {}
        self._key_resolver = key_resolver
        if default_embedder is not None:      # 测试注入 FakeEmbedder 走这里
            self._cache[DEFAULT_EMBEDDER_ID] = default_embedder

    def get(self, option_id: str | None):
        """option_id 为 None/"default" → 默认 embedder；否则按清单构建并缓存。
        未知 id 抛 KeyError（KB 绑定的选项被人从配置里删了：宁可失败也不能
        悄悄换成默认模型——那会让该库的检索打分静默失效）。"""
        oid = option_id or DEFAULT_EMBEDDER_ID
        if oid in self._cache:
            return self._cache[oid]
        if oid == DEFAULT_EMBEDDER_ID:
            instance = build_default_embedder(self._cfg)
        else:
            opt = self._options.get(oid)
            if opt is None:
                raise KeyError(
                    f"向量模型选项未配置: {oid}（已配置: "
                    f"{[DEFAULT_EMBEDDER_ID, *self._options]}）")
            api_key = self._key_resolver(oid) if self._key_resolver else None
            instance = build_option_embedder(opt, api_key=api_key)
        self._cache[oid] = instance
        return instance

    def invalidate(self, option_id: str) -> None:
        """密钥在页面被改/清后调用：丢弃缓存实例，下次使用按新密钥重建。
        不失效的话旧实例（旧 key）会一直用到进程重启。"""
        self._cache.pop(option_id, None)

    def catalog(self) -> dict:
        """给前端建库下拉用的公开清单（不含 api_key_env 等部署细节）。"""
        default_desc = {"id": DEFAULT_EMBEDDER_ID,
                        "plugin": self._cfg.embedder.name,
                        "model": self._cfg.embedder.model}
        options = [{"id": o.id, "plugin": o.plugin, "model": o.model}
                   for o in self._cfg.embedders]
        return {"default": default_desc, "options": options}

    def known_ids(self) -> set[str]:
        return {DEFAULT_EMBEDDER_ID, *self._options}


def kb_embedder_id(sf, kb_id: str) -> str | None:
    """读 KB.config JSON 的 embedder 绑定；无绑定/解析失败 → None（默认模型）。"""
    from kbase.models import KnowledgeBase
    with sf() as s:
        kb = s.get(KnowledgeBase, kb_id)
    if kb is None or not kb.config:
        return None
    try:
        return json.loads(kb.config).get("embedder")
    except (json.JSONDecodeError, TypeError):
        return None
