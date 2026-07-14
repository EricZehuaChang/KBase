"""模型目录：从 OpenAI 兼容端点拉取 /models 清单并按 base_url 缓存。

用途（M5-2 Provider UI）：添加/编辑 Provider 时"获取模型列表"后下拉选模型，
免手抄型号名；对任何 OpenAI 兼容端点生效——含企业内部自有大模型平台
（vLLM/网关等），私有化场景只要平台实现了 GET /models 就能接。

缓存策略：AppSetting 表按 base_url 存 JSON（models + fetched_at），
CATALOG_TTL_DAYS=7 天视为过期（stale）。读取端点对 stale 且能从已配置
provider 找到凭据的目录自动触发后台刷新——等效"每周自动更新"，由管理页
访问驱动，不引入独立调度器（私有化部署少一个常驻运维部件，代价是无人
访问就不刷新，对"选型号"这个用途足够）。
"""
import json
from datetime import datetime, timedelta

import httpx

from kbase.models import AppSetting

CATALOG_KEY_PREFIX = "model_catalog:"
CATALOG_TTL_DAYS = 7


def _key(base_url: str) -> str:
    return CATALOG_KEY_PREFIX + base_url.rstrip("/")


def fetch_models(base_url: str, api_key: str, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> list[str]:
    """GET {base_url}/models，返回排序后的模型 id 列表。
    错误统一包成 RuntimeError（带 base_url 与原因），路由层转 502 给前端展示。"""
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.get(f"{base_url.rstrip('/')}/models",
                              headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        raise RuntimeError(f"模型列表拉取失败（{base_url} 不可达）: {e}") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"模型列表拉取失败（{base_url} 返回 {e.response.status_code}，"
            f"请检查 API Key 是否有效）") from e
    data = resp.json().get("data", [])
    ids = sorted({m.get("id", "") for m in data if m.get("id")})
    if not ids:
        raise RuntimeError(f"{base_url}/models 返回了空列表（端点兼容性问题？）")
    return ids


def save_catalog(sf, base_url: str, models: list[str]) -> dict:
    """写缓存（upsert），返回目录 dict（含 fetched_at）。"""
    fetched_at = datetime.utcnow().isoformat()
    payload = json.dumps({"models": models, "fetched_at": fetched_at},
                         ensure_ascii=False)
    with sf() as s:
        row = s.get(AppSetting, _key(base_url))
        if row is None:
            s.add(AppSetting(key=_key(base_url), value=payload))
        else:
            row.value = payload
        s.commit()
    return {"base_url": base_url.rstrip("/"), "models": models,
            "fetched_at": fetched_at, "stale": False}


def _row_to_catalog(row: AppSetting) -> dict:
    data = json.loads(row.value)
    fetched_at = data.get("fetched_at")
    stale = True
    if fetched_at:
        try:
            stale = (datetime.utcnow() - datetime.fromisoformat(fetched_at)
                     > timedelta(days=CATALOG_TTL_DAYS))
        except ValueError:
            stale = True
    return {"base_url": row.key[len(CATALOG_KEY_PREFIX):],
            "models": data.get("models", []),
            "fetched_at": fetched_at, "stale": stale}


def load_catalog(sf, base_url: str) -> dict | None:
    with sf() as s:
        row = s.get(AppSetting, _key(base_url))
        return _row_to_catalog(row) if row else None


def load_all(sf) -> list[dict]:
    with sf() as s:
        rows = (s.query(AppSetting)
                .filter(AppSetting.key.like(CATALOG_KEY_PREFIX + "%")).all())
        return [_row_to_catalog(r) for r in rows]
