"""前端联调/浏览器验证用的开发后端工厂（不用于生产）。

与生产 create_app 的差异只有两点：
1. 默认 embedder 注入确定性假向量（不下载 bge-m3，秒启动）——但 cfg.embedders
   清单里的云端向量模型（openai-embed）是真实装配，绑定它的 KB 走真 API；
2. auth="off"（本机联调免登录，管理端守卫拿到合成 admin 直接放行）。

密钥从仓库根 .env 读取（gitignored；ZHIPU_API_KEY / DASHSCOPE_API_KEY），
GLM-OCR 与 DashScope 向量/LLM 均为真实调用。

用法（或经 .claude/launch.json 的 api-dev 配置启动）：
    uvicorn --factory scripts.dev_app:create_dev_app --port 8100
"""
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))     # worktree 代码优先于主仓 editable 安装

_DEV_CONFIG = """
data_dir: __DATA_DIR__
chunker: {name: structure, chunk_size: 512, chunk_overlap: 64}
embedders:
  - id: qwen-embed-v3
    plugin: openai-embed
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
    model: text-embedding-v3
retrieval:
  rerank: {enabled: false}
ocr:
  enabled: true
  backend: glm-ocr
# F 满血 VLM 深度识别：实测本机在手模型中 gpt-5.4-mini 视觉直通
# （glm-5.2/qwen3.7-max 文本端点不收图，需各家 VL 专门型号）
vlm_parse:
  provider: gpt-5.4-mini
llm:
  active: qwen-plus
  providers:
    - name: qwen-plus
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key_env: DASHSCOPE_API_KEY
      model: qwen-plus
    - name: kimi-k3
      base_url: https://api.moonshot.cn/v1
      api_key_env: MOONSHOT_API_KEY
      model: kimi-k3
      params:
        extra_body:
          reasoning_effort: high
"""


class DevFakeEmbedder:
    """确定性假向量（与 tests/conftest.py 同思路）：默认库不依赖任何模型下载。"""
    dimension = 8

    def embed(self, texts):
        out = []
        for t in texts:
            h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
            out.append([((h >> (i * 4)) % 100) / 100.0 for i in range(8)])
        return out


def _load_dotenv(path: Path) -> None:
    """最小 .env 解析：KEY=VALUE 行注入环境（不覆盖已存在的变量）。"""
    import os
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _load_secrets_yaml() -> None:
    """本地开发便利：若存在集中密钥库（默认 D:/Claude Code/env/secrets.yaml，
    可用环境变量 KBASE_SECRETS_DIR 覆盖路径），经 load_keys 注入环境变量——
    secrets.yaml 单一来源，改 yaml 即可，无需再往仓库 .env 复制一份（如
    MOONSHOT_API_KEY）。找不到就静默跳过：生产走 compose/.env、他机无此路径，
    dev_app 本就不用于生产，故 best-effort 不报错。"""
    import os
    import sys as _sys
    env_dir = os.environ.get("KBASE_SECRETS_DIR", r"D:/Claude Code/env")
    try:
        if env_dir not in _sys.path:
            _sys.path.insert(0, env_dir)
        from load_keys import load_keys
        load_keys()
    except Exception:
        pass  # 无集中密钥库 → 回落到仓库 .env


def create_dev_app():
    _load_secrets_yaml()          # 先从集中密钥库注入（secrets.yaml，若有）
    _load_dotenv(REPO / ".env")   # 仓库 .env 兜底/补缺（setdefault 不覆盖已注入的）
    cfg_dir = REPO / "data" / "dev"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "kbase.dev.yaml"
    # data_dir 用绝对路径写死到仓库 data/dev 下（gitignored）：dev 服务可能
    # 从任意 cwd 启动（如 IDE/preview 工具），相对路径会把数据写到别处。
    # 用 __DATA_DIR__ 占位符替换而不是 str.format——模板里 YAML 流式映射的
    # 字面花括号（如 {name: structure}）会让 .format 抛 KeyError（同
    # kbase/config.py resolve_db_url 记载的坑）。
    cfg_path.write_text(
        _DEV_CONFIG.replace("__DATA_DIR__", str(cfg_dir).replace("\\", "/")),
        encoding="utf-8")

    from kbase.api.main import create_app
    return create_app(config_path=str(cfg_path), embedder=DevFakeEmbedder(),
                      auth="off")
