"""i18n 覆盖表存取(方案 A)。译文基线在前端 locales/*.json;这里只管
运营在管理端改过的覆盖增量。key 是语义点分 key(如 kb.create)。
网络无关的纯 DB 层,测试直接调不出网。"""
from datetime import datetime

from kbase.models import Translation


def get_overrides(sf, lang: str) -> dict[str, str]:
    """某语言的全部覆盖 {key: value}——喂 GET /api/i18n/{lang},前端合并
    进基线。空 dict = 该语言无覆盖(全用基线)。"""
    with sf() as s:
        rows = s.query(Translation).filter_by(lang=lang).all()
        return {r.key: r.value for r in rows}


def get_all_overrides(sf) -> dict[str, dict[str, str]]:
    """全部语言覆盖 {lang: {key: value}}——喂管理页,标出哪些 key 已被改过
    (与基线区分)。"""
    out: dict[str, dict[str, str]] = {}
    with sf() as s:
        for r in s.query(Translation).all():
            out.setdefault(r.lang, {})[r.key] = r.value
    return out


def set_override(sf, lang: str, key: str, value: str,
                 actor: str | None = None) -> str:
    """写覆盖(upsert)。value 空串 = 删除覆盖,该 key 回落基线(运营"撤销
    我的修改、用回机翻底"的语义)。返回 "set" | "deleted"。"""
    with sf() as s:
        row = s.get(Translation, (lang, key))   # 复合主键按 (lang, key) 顺序
        if not value:
            if row is not None:
                s.delete(row)
                s.commit()
            return "deleted"
        if row is None:
            s.add(Translation(lang=lang, key=key, value=value,
                              updated_by=actor, updated_at=datetime.utcnow()))
        else:
            row.value = value
            row.updated_by = actor
            row.updated_at = datetime.utcnow()
        s.commit()
        return "set"
