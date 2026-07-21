"""i18n 机翻脚本(方案 A 的基线生成):以 zh.json 为源,把缺失的 key 机翻
到 en/ms(或 --lang 指定的新语言),生成前端 locales 基线。

设计:
- **只翻缺失 key**(默认):已有译文保留——人工校准过的不被机翻覆盖;
  --force 全量重翻。
- 输出对齐源 key 集合与顺序:源删掉的 key 从目标一并移除(保三语 key 一致,
  配套 CI 的 key 一致性测试)。
- 调 DeepSeek(deepseek-v4-flash,api.deepseek.com/v1,DEEPSEEK_API_KEY),
  关思考省时省钱;response_format=json_object 保证可解析。
- 术语规则:占位符 {id}/{name} 原样保留、产品名 KBase 不译。

用法:
    DEEPSEEK_API_KEY=... python scripts/i18n_machine_translate.py            # 补 en/ms 缺失
    DEEPSEEK_API_KEY=... python scripts/i18n_machine_translate.py --lang th  # 新增泰语
    DEEPSEEK_API_KEY=... python scripts/i18n_machine_translate.py --force    # 全量重翻
"""
import argparse
import json
import os
import sys
from pathlib import Path

import httpx

LOCALES = (Path(__file__).resolve().parents[1]
           / "web-app" / "src" / "i18n" / "locales")
SRC_LANG = "zh"
# 目标语言的自然语言名(喂给模型;新语言在这里加一行即可机翻)
LANG_NAMES = {
    "en": "English",
    "ms": "Bahasa Melayu (Malaysian Malay)",
}
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-flash"
BATCH = 40   # 每批 key 数:平衡上下文与请求数


def flatten(obj, prefix=""):
    out = {}
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def unflatten(flat):
    out = {}
    for k, v in flat.items():
        parts = k.split(".")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return out


def translate_batch(api_key, target_name, items):
    """items={key:中文} -> {key:译文}。占位符/产品名保持,只译 value。"""
    sys_prompt = (
        "You are a professional software UI localizer. Translate the VALUES of "
        f"the given JSON object from Chinese into {target_name}. Rules: (1) keep "
        "every key unchanged; (2) keep placeholders such as {id} {name} {count} "
        "exactly as-is; (3) keep the product name 'KBase' untranslated; (4) use "
        "natural, concise wording fit for buttons/menus/labels; (5) return ONLY a "
        "JSON object mapping the same keys to translated strings, nothing else."
    )
    resp = httpx.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user",
                 "content": json.dumps(items, ensure_ascii=False)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", action="append",
                    help="目标语言码(可重复;默认 en ms)")
    ap.add_argument("--force", action="store_true",
                    help="重翻已有 key(默认只补缺失,保留人工校准)")
    args = ap.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("需要环境变量 DEEPSEEK_API_KEY")

    targets = args.lang or ["en", "ms"]
    src = flatten(json.loads(
        (LOCALES / f"{SRC_LANG}.json").read_text(encoding="utf-8")))

    for lang in targets:
        name = LANG_NAMES.get(lang, lang)
        path = LOCALES / f"{lang}.json"
        existing = (flatten(json.loads(path.read_text(encoding="utf-8")))
                    if path.exists() else {})
        todo = {k: v for k, v in src.items()
                if args.force or not existing.get(k)}
        if not todo:
            print(f"[{lang}] 无缺失,跳过")
            continue
        print(f"[{lang}] 待翻 {len(todo)} 条(共 {len(src)} key)...")
        result = dict(existing)
        keys = list(todo)
        for i in range(0, len(keys), BATCH):
            batch = {k: todo[k] for k in keys[i:i + BATCH]}
            result.update(translate_batch(api_key, name, batch))
            print(f"  {min(i + BATCH, len(keys))}/{len(keys)}")
        # 对齐源 key 集合与顺序:源删掉的 key 一并移除(保三语 key 一致)
        merged = {k: result.get(k) or existing.get(k, "") for k in src}
        path.write_text(
            json.dumps(unflatten(merged), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"[{lang}] 写入 {path}")


if __name__ == "__main__":
    main()
