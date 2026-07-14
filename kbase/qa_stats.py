"""问答运营统计（C 运营看板）：从审计表聚合问答量/拒答率与无答案清单。

数据来源是既有 audit_logs 表——问答时端点写 action="query"，检索无依据
（拒答）时 _run_query 额外写 action="query_refused"，detail 存问题前 100 字。
本模块只读聚合，不引入新表：审计本就是这些事件的权威记录，再建统计表会
产生双写一致性问题。
"""
from datetime import datetime, timedelta

from sqlalchemy import func

from kbase.models import AuditLog


def qa_overview(sf, days: int = 7) -> dict:
    """近 days 天的问答概览：总问答数、拒答数、拒答率、按日趋势。"""
    since = datetime.utcnow() - timedelta(days=days)
    with sf() as s:
        total = (s.query(func.count(AuditLog.id))
                 .filter(AuditLog.action == "query", AuditLog.ts >= since).scalar()) or 0
        refused = (s.query(func.count(AuditLog.id))
                   .filter(AuditLog.action == "query_refused",
                           AuditLog.ts >= since).scalar()) or 0

        # 按日分组（func.date 在 sqlite/pg 都可用）；两类事件各自计数后在应用层
        # 对齐，避免依赖方言特定的条件聚合语法。
        def by_day(action):
            rows = (s.query(func.date(AuditLog.ts), func.count(AuditLog.id))
                    .filter(AuditLog.action == action, AuditLog.ts >= since)
                    .group_by(func.date(AuditLog.ts)).all())
            return {str(d): int(c) for d, c in rows}

        q_by_day, r_by_day = by_day("query"), by_day("query_refused")

    trend = [{"date": d, "total": q_by_day[d], "refused": r_by_day.get(d, 0)}
             for d in sorted(q_by_day)]
    return {
        "days": days,
        "total": int(total),
        "refused": int(refused),
        "refusal_rate": round(refused / total, 4) if total else 0.0,
        "trend": trend,
    }


def unanswered_questions(sf, limit: int = 50) -> list[dict]:
    """最近的无答案（拒答）问题清单——知识缺口的直接信号，运营据此补文档。"""
    with sf() as s:
        rows = (s.query(AuditLog)
                .filter(AuditLog.action == "query_refused")
                .order_by(AuditLog.ts.desc()).limit(limit).all())
        return [{"ts": r.ts.isoformat(), "question": r.detail,
                 "actor": r.actor, "resource": r.resource} for r in rows]
