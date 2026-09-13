"""CI 检查：`docker-compose*.yml` 与 `config/*.yaml` 引用的环境变量，是否都被
受跟踪的 `.env.example` 覆盖（T06/G04 的验收口径，机械化而非靠人肉核对）。

为什么需要它：部署缺一个变量不会报错，而是**静默降级**——compose 没列
`IRUIDONG_API_KEY` 时，即使 .env 里有值也传不进容器，症状是"某个模型一直
401/未配密钥"，排查成本远高于在这里拦一下。

用法：python scripts/check_env_example.py
compose/config 引用但 .env.example 没有的变量 → 非零退出（真缺）；
.env.example 有但 compose/config 不引用的 → 仅警告（这些是应用代码直接读
的运行期变量，如 KBASE_PDF_PARSER / MCP 侧变量，属预期）。
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# compose 里的 ${VAR} / ${VAR:-default} / ${VAR:?message}
_COMPOSE_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
# config yaml 里的 api_key_env: NAME / password_env: NAME
_YAML_REF = re.compile(r"^\s*(?:api_key_env|password_env):\s*([A-Za-z_][A-Za-z0-9_]*)\s*$",
                       re.MULTILINE)

# 已知的运行期变量：由部署/命令行直接给，且不进 .env.example 也不该拦
# （例如 entrypoint.sh 内部用的等待列表，见 compose 的 KBASE_WAIT_FOR）。
_ALLOWED_UNCOVERED: set[str] = set()


def _env_example_names() -> set[str]:
    names: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.add(line.split("=", 1)[0].strip())
    return names


def _referenced_names() -> dict[str, list[str]]:
    """变量名 → 引用它的文件清单（便于报错时直接定位）。"""
    found: dict[str, list[str]] = {}
    targets = sorted(REPO_ROOT.glob("docker-compose*.yml"))
    targets += sorted((REPO_ROOT / "config").glob("*.yaml"))
    for path in targets:
        text = path.read_text(encoding="utf-8")
        names = set(_COMPOSE_REF.findall(text)) | set(_YAML_REF.findall(text))
        for name in names:
            found.setdefault(name, []).append(path.name)
    return found


def main() -> int:
    if not ENV_EXAMPLE.exists():
        print("check_env_example: 缺少仓库根 .env.example", file=sys.stderr)
        return 1

    declared = _env_example_names()
    referenced = _referenced_names()

    missing = sorted(n for n in referenced if n not in declared
                     and n not in _ALLOWED_UNCOVERED)
    extra = sorted(declared - set(referenced))

    for name in extra:
        print(f"warn  {name} 在 .env.example 里但 compose/config 未引用")
    if missing:
        for name in missing:
            print(f"FAIL  {name} 被 {', '.join(sorted(referenced[name]))} 引用，"
                  f"但 .env.example 里没有", file=sys.stderr)
        print(f"check_env_example: {len(missing)} 个变量未覆盖", file=sys.stderr)
        return 1
    print(f"check_env_example: compose/config 引用的 {len(referenced)} 个变量"
          f"全部被 .env.example 覆盖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
