"""E2E 冒烟套件的后端进程工厂（由 playwright.config.ts 的 webServer 启动）。

为什么不是直接 `--factory scripts.dev_app:create_dev_app`：dev_app 已经解决了
"不下载模型、不要向量服务"（注入确定性假向量 embedder、auth="off" 免登录），
但**问答链路还要一个 LLM**：dev 配置里的 provider 指向 DashScope/月之暗面真云
端点，缺 DASHSCOPE_API_KEY 时 /api/kb/{id}/query、/api/conversations/{id}/query
在 svc.get_llm() 就 503（见 kbase/plugins/llm/openai_compat.py 构造期校验），
浏览器用例永远走不到"答案 + 引用"那一步。CI 里没有、也不该有密钥，所以这里在
create_app 的**官方测试注入口**（llms 参数，见 kbase/api/main.py create_app 与
kbase/api/services.py get_llm 的 _llm_cache）上补一个确定性桩 LLM：

- 其它一切照旧：配置模板、假向量 embedder、auth="off"、真实摄取/分块/向量化/
  检索链路全部沿用 scripts/dev_app，不复制、不改动任何后端代码；
- 系统路径自己插到 sys.path[0]（与 scripts/dev_app.py 同一手法），所以无论从哪个
  cwd 启动，跑的都是本 worktree 的 kbase 代码（editable 安装指向别的 worktree）。

用法（见 playwright.config.ts；也可手工起在别的端口做无密钥自检）：
    python -m uvicorn --factory dev_server:create_dev_app --host 127.0.0.1 --port 8100
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]   # web-app/e2e/dev_server.py -> 仓库根
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# scripts/dev_app.py 的 llm.active + providers 清单（桩要按名字注入，否则
# get_llm 会去 DB 里按 provider 名建真实 openai-compat 实例、又要密钥）。
PROVIDER_NAMES = ("qwen-plus", "kimi-k3")

# 桩回答：固定文本 + "[1]" 角标。角标是产品核心卖点，CI 里必须能被断言到，
# 所以桩回答刻意带上编号——引用数据本身仍来自真实检索（citations 事件），
# 只有"模型正文"这一段是桩的（真实 LLM 由复用本机 dev 栈时提供）。
STUB_ANSWER_PIECES = ("（E2E-STUB）端到端冒烟测试", "的固定回答，", "依据资料 [1] 生成。")


class StubLLM:
    """确定性 LLM：不联网、不读密钥，按固定分片流式返回。

    只实现问答链路真正消费的两个方法（见 kbase/plugins/base.py LLMProvider）：
    - stream(): Generator.answer_stream 逐片 yield 文本；
    - complete(): QueryRewriter 触发改写时调用，返回空串 = "未改写"，检索继续
      用原问题——与 LazyRewriter "改写失败不阻塞主链路"的既有降级语义一致。
    """

    model = "e2e-stub"

    async def stream(self, messages, *, usage_sink=None, **params):
        for piece in STUB_ANSWER_PIECES:
            yield piece

    async def complete(self, messages, **params):
        return ""


def create_dev_app():
    """uvicorn --factory 入口：dev 配置 + 假向量 + auth=off + 确定性桩 LLM。"""
    from kbase.api import main as api_main

    original_create_app = api_main.create_app

    def create_app_with_stub_llm(config_path="config/kbase.yaml", **kwargs):
        kwargs.setdefault("llms", {name: StubLLM() for name in PROVIDER_NAMES})
        return original_create_app(config_path, **kwargs)

    # dev_app.create_dev_app() 内部才 `from kbase.api.main import create_app`，
    # 所以在这里换掉模块属性即可生效；返回前还原，避免影响同进程的后续调用。
    api_main.create_app = create_app_with_stub_llm
    try:
        from scripts import dev_app
        return dev_app.create_dev_app()
    finally:
        api_main.create_app = original_create_app
