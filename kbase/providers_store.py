"""Provider 与应用设置的数据库存取：YAML 首次启动种子导入 + CRUD + 活跃 provider。

设计：providers 表为空时才从 YAML（cfg.llm）种子导入（一次性），之后 DB 是唯一
真源——YAML 仅用于首次部署引导，运行期改配置走 API/DB。"""
import json

from kbase.models import AppSetting, ProviderRow

ACTIVE_PROVIDER_KEY = "active_provider"


def seed_from_config(sf, cfg) -> None:
    """providers 表为空时，从 cfg.llm.providers 导入并把 active 写入 app_settings。
    非空（已种子过或用户已通过 API 管理）则跳过，不覆盖用户改动。"""
    with sf() as s:
        if s.query(ProviderRow).first() is not None:
            return
        for p in cfg.llm.providers:
            s.add(ProviderRow(
                name=p.name, base_url=p.base_url, api_key_env=p.api_key_env,
                model=p.model, max_concurrency=p.max_concurrency,
                params=json.dumps(p.params, ensure_ascii=False) if p.params else None))
        if s.get(AppSetting, ACTIVE_PROVIDER_KEY) is None:
            s.add(AppSetting(key=ACTIVE_PROVIDER_KEY, value=cfg.llm.active))
        s.commit()


def _row_to_dict(row: ProviderRow) -> dict:
    return {
        "name": row.name,
        "base_url": row.base_url,
        "api_key_env": row.api_key_env,
        "model": row.model,
        "max_concurrency": row.max_concurrency,
        "params": json.loads(row.params) if row.params else {},
    }


def get_provider_dict(sf, name: str) -> dict | None:
    with sf() as s:
        row = s.get(ProviderRow, name)
        if row is None:
            return None
        return _row_to_dict(row)


def list_providers(sf) -> list[dict]:
    with sf() as s:
        rows = s.query(ProviderRow).order_by(ProviderRow.name).all()
        return [_row_to_dict(r) for r in rows]


def get_active(sf) -> str | None:
    with sf() as s:
        setting = s.get(AppSetting, ACTIVE_PROVIDER_KEY)
        return setting.value if setting else None


def set_active(sf, name: str) -> None:
    with sf() as s:
        setting = s.get(AppSetting, ACTIVE_PROVIDER_KEY)
        if setting is None:
            s.add(AppSetting(key=ACTIVE_PROVIDER_KEY, value=name))
        else:
            setting.value = name
        s.commit()


def create_provider(sf, data: dict) -> None:
    params = data.get("params") or {}
    with sf() as s:
        s.add(ProviderRow(
            name=data["name"], base_url=data["base_url"],
            api_key_env=data["api_key_env"], model=data["model"],
            max_concurrency=data.get("max_concurrency", 4),
            params=json.dumps(params, ensure_ascii=False) if params else None))
        s.commit()


def update_provider(sf, name: str, data: dict) -> bool:
    """部分更新（PATCH 语义）：只覆盖 data 中出现的字段。返回是否找到该 provider。"""
    with sf() as s:
        row = s.get(ProviderRow, name)
        if row is None:
            return False
        if "base_url" in data and data["base_url"] is not None:
            row.base_url = data["base_url"]
        if "api_key_env" in data and data["api_key_env"] is not None:
            row.api_key_env = data["api_key_env"]
        if "model" in data and data["model"] is not None:
            row.model = data["model"]
        if "max_concurrency" in data and data["max_concurrency"] is not None:
            row.max_concurrency = data["max_concurrency"]
        if "params" in data and data["params"] is not None:
            row.params = json.dumps(data["params"], ensure_ascii=False)
        s.commit()
        return True


def delete_provider(sf, name: str) -> bool:
    with sf() as s:
        row = s.get(ProviderRow, name)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True
