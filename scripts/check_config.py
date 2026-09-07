"""CI 配置可加载检查：遍历 config/*.yaml，逐份用应用的真实加载器校验。

历史背景：config/kbase.yaml 加 LLM provider 时（2026-09-04 等多次）都靠人手
在 venv 里 yaml 校验，CI 没有这道闸。load_config 是纯 pydantic 结构校验
（不读密钥、不建服务、不出网），可以直接在 CI lint job 里跑；比 yaml.safe_load
强在它同时验证字段类型/必填/枚举，能拦住"字段名写错、缩进把整段并进别的键"
这类 safe_load 发现不了的问题。

用法：python scripts/check_config.py [路径...]；缺省 = config/ 下全部 *.yaml。
任一文件加载失败即非零退出，并打印文件名与 pydantic 错误摘要。
"""
import sys
from pathlib import Path

from kbase.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    if argv:
        candidates = [Path(a) for a in argv]
    else:
        candidates = sorted((REPO_ROOT / "config").glob("*.yaml"))

    if not candidates:
        print("check_config: 没有找到任何 config/*.yaml", file=sys.stderr)
        return 2

    failed = 0
    for path in candidates:
        try:
            load_config(path)
        except Exception as exc:  # pydantic ValidationError 等——统一摘要
            failed += 1
            print(f"FAIL  {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            print(f"ok    {path}")

    if failed:
        print(f"check_config: {failed}/{len(candidates)} 份配置加载失败", file=sys.stderr)
        return 1
    print(f"check_config: {len(candidates)} 份配置全部可加载")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
