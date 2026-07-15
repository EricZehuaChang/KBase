"""向量模型密钥的页面级配置：cfg.embedders 各选项的 API Key 可在设置页
维护（与 LLM Provider 同规矩）。

优先级：**DB 覆盖 > 环境变量（api_key_env）**——部署时可以只在页面配 key，
不再要求改机器环境变量重启。存 AppSetting KV（key=embedder_api_key:{id}），
GET 只出脱敏状态（has_db_key + 尾4位），原文永不出站。
"""
from kbase.models import AppSetting

KEY_PREFIX = "embedder_api_key:"


def get_key(sf, option_id: str) -> str | None:
    with sf() as s:
        row = s.get(AppSetting, KEY_PREFIX + option_id)
        return row.value if row else None


def set_key(sf, option_id: str, api_key: str) -> None:
    with sf() as s:
        row = s.get(AppSetting, KEY_PREFIX + option_id)
        if row is None:
            s.add(AppSetting(key=KEY_PREFIX + option_id, value=api_key))
        else:
            row.value = api_key
        s.commit()


def delete_key(sf, option_id: str) -> bool:
    with sf() as s:
        row = s.get(AppSetting, KEY_PREFIX + option_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def list_status(sf, cfg) -> list[dict]:
    """管理页清单：每个可配 key 的选项的脱敏状态。bge-local/tei 无密钥概念，
    过滤掉只留 openai-embed（云端向量服务）。"""
    items = []
    for opt in cfg.embedders:
        if opt.plugin != "openai-embed":
            continue
        db_key = get_key(sf, opt.id)
        items.append({
            "id": opt.id, "plugin": opt.plugin, "model": opt.model,
            "api_key_env": opt.api_key_env,
            "has_db_key": db_key is not None,
            "key_hint": (f"…{db_key[-4:]}" if db_key and len(db_key) >= 4 else None),
        })
    return items
