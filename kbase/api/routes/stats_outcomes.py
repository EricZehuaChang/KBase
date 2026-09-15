"""问答归因接口（T12 后半）：清单 / 下钻 / CSV 导出。

数据源是 kbase/qa_outcomes.py 的 qa_outcomes 表（每次问答一行，四个互斥桶）。
这里只做三件事：解析过滤参数、按查看者做可见性过滤、把行投影成响应/CSV。
聚合口径全部在 kbase/qa_outcomes.py 里（list_outcomes/export_rows 共用一份
_filtered），路由层不复刻任何判定——否则"分布说 3 条、清单只有 2 条"这种
对不上的账最招人烦。

可见性两条与既有看板完全一致（不新造一套）：
- 库级：KbGuard（ACL + API Key scope）——带 kb_id 的查询与按 id 下钻都过一遍
  库守卫，库外资源统一 404，不泄漏存在性；不带 kb_id 的清单则按"我有权看的
  库"过滤（否则面板不选库就等于把全站缺口看一遍）。
- 审计分层：非超管查看者排除超管 actor 的行（qa_outcomes.hidden_actors，与
  routes/admin.py 的 _hidden_actors 同口径）。归因行带 actor，不过滤会从侧面
  泄漏超管的活动痕迹。

**历史数据无法回填**：发布之前的问答当时没有记录命中数/最高分，重跑历史
问题拿到的是"今天的检索结果"而不是当时的事故现场（见 kbase/qa_outcomes.py
的模块 docstring）。所以本接口上线初期只会返回很少的行，第一份有意义的归因
报告要等数据攒够（周量级）——不存在"上线即见归因"。
"""
import csv
import io
from datetime import datetime, timezone

from fastapi import Query, Request
from fastapi.responses import Response

from kbase import qa_outcomes
from kbase.api.guards import KbGuard
from kbase.api.routes import RouteDeps
from kbase.api.services import Services
from kbase.errors import AppError
from kbase.models import KnowledgeBase

# 桶取值不是自由文本：写死在 qa_outcomes.BUCKETS 里（模块常量），路由只做白名单
# 校验——脏值直接 422，不要静默返回空清单（"这个桶没数据"与"你传错了"必须能分清）。


def _not_found(outcome_id: str) -> AppError:
    """下钻的"不存在/无权"统一响应（不区分原因，不泄漏存在性）。"""
    return AppError("error.outcome_not_found", "归因记录不存在: {id}",
                    status=404, id=outcome_id)


def _parse_ts(value: str, field: str) -> datetime:
    """ISO 时间参数 → naive UTC。归因表的 ts 是 naive UTC（与全仓 models 同
    口径，见 kbase/models.py），带 Z/偏移的入参先转 UTC 再去 tzinfo，否则
    "UTC 的 12:00" 与 "本地 12:00" 会差出一个时区却看不出错。

    解析不了回 422 而不是"当作没传"：静默放宽时间范围会让运营拿着一份覆盖
    全时段的数据以为是自己选的那一周。
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise AppError("error.bad_time_param", "{field} 不是合法时间: {value}",
                       status=422, field=field, value=value) from e
    return (parsed.astimezone(timezone.utc).replace(tzinfo=None)
            if parsed.tzinfo else parsed)


def _csv_cell(value) -> str:
    """CSV 单元格：None → 空串（不是字面 "None"——导出给人看，空就是没有）。
    换行/逗号交给 csv 模块转义，不在这里手工处理（与 routes/import_batches.py
    的 _csv_cell 同一手法）。"""
    return "" if value is None else str(value)


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf
    guard = KbGuard(sf)

    def _actor(request: Request) -> dict:
        """查看者身份；取不到时按 admin 兜底（与 KbGuard._actor 同口径：
        auth="off" 下路由级依赖已写入合成超管 actor，这里只是防御性兜底）。"""
        return getattr(request.state, "actor", None) or {"role": "admin"}

    def _exclude_actors(request: Request) -> set[str] | None:
        """审计分层：非超管查看者要排除的 actor 集合（超管看全量返回 None）。
        实现收在 qa_outcomes.hidden_actors（含"actor 可为 NULL"的 SQL 处理），
        本模块只负责把 actor 传进去——判定只有一处。"""
        return qa_outcomes.hidden_actors(sf, _actor(request))

    def _visible_kb_ids(request: Request) -> set[str] | None:
        """查看者可见的库集合；None=不设限（admin 豁免 ACL 且 key 无 scope）。

        与 list_kb / routes/import_batches.py 的多库可见性同一手法：没带 kb_id
        的清单不能因为"没指定库"就把别人的库漏出来。key 的 scope 在 admin 角色
        上同样挡（受限 admin key 只看白名单内的库），故两条闸门都要过。
        """
        actor = _actor(request)
        if actor.get("role") in ("admin", "superadmin") \
                and actor.get("scope_kb_ids") is None:
            return None
        with sf() as s:
            ids = {row[0] for row in s.query(KnowledgeBase.id).all()}
        return {k for k in ids if guard.allows(k, request)}

    def _guard_kb_filter(kb_id: str | None, request: Request) -> None:
        """带了 kb_id 就必须过库守卫：确认这个库存在且有权看（否则 404）。
        "库不存在"与"无权"同样 404——与全仓不泄漏存在性的原则一致。"""
        if kb_id:
            guard.kb(kb_id, request)

    # 注意注册顺序：export.csv 必须在 {outcome_id} 之前，否则被当成一条归因
    # 记录的 id 去下钻（"这个 id 不存在"的 404，而不是导出）。

    @router.get("/stats/outcomes", dependencies=[deps.require_admin])
    def list_outcomes(request: Request,
                      bucket: str | None = Query(default=None),
                      channel: str | None = Query(default=None),
                      kb_id: str | None = Query(default=None),
                      since: str | None = Query(default=None, alias="from"),
                      until: str | None = Query(default=None, alias="to"),
                      limit: int = Query(default=50, ge=1, le=200),
                      offset: int = Query(default=0, ge=0)):
        """归因清单 + 同条件总数 + 按桶分布（分布不叠加 bucket 过滤，见
        qa_outcomes.list_outcomes）。

        from/to 是闭区间（含当天边界）：运营按"某日到某日"导出时两端都算，
        与审计接口的时间语义保持一致。
        """
        if bucket and bucket not in qa_outcomes.BUCKETS:
            raise AppError("error.bad_bucket", "未知的归因桶: {bucket}",
                           status=422, bucket=bucket)
        _guard_kb_filter(kb_id, request)
        result = qa_outcomes.list_outcomes(
            sf, bucket=bucket, channel=channel, kb_id=kb_id,
            since=_parse_ts(since, "from") if since else None,
            until=_parse_ts(until, "to") if until else None,
            limit=limit, offset=offset, exclude_actors=_exclude_actors(request),
            # 指定了库就不必再按可见性过滤（守卫已经确认了这一次查询的库）
            kb_ids_in=None if kb_id else _visible_kb_ids(request))
        return {**result, "limit": limit, "offset": offset,
                "buckets_known": list(qa_outcomes.BUCKETS)}

    @router.get("/stats/outcomes/export.csv", dependencies=[deps.require_admin])
    def export_outcomes(request: Request,
                        bucket: str | None = Query(default=None),
                        channel: str | None = Query(default=None),
                        kb_id: str | None = Query(default=None),
                        since: str | None = Query(default=None, alias="from"),
                        until: str | None = Query(default=None, alias="to")):
        """CSV 导出：**列完整**（qa_outcomes.EXPORT_COLUMNS 是列顺序的唯一
        事实源，含 question 全文与 feedback）。与清单同一套过滤，只是不分页
        （上限 EXPORT_MAX_ROWS，导出是人工排障产物，不做无限全量）。

        口径提示写在 CSV 里没有意义（机器读），所以第 0 行不加注释——但导出方
        必须在界面上知道"历史数据无法回填"（面板上有一行说明）。
        """
        if bucket and bucket not in qa_outcomes.BUCKETS:
            raise AppError("error.bad_bucket", "未知的归因桶: {bucket}",
                           status=422, bucket=bucket)
        _guard_kb_filter(kb_id, request)
        rows = qa_outcomes.export_rows(
            sf, bucket=bucket, channel=channel, kb_id=kb_id,
            since=_parse_ts(since, "from") if since else None,
            until=_parse_ts(until, "to") if until else None,
            exclude_actors=_exclude_actors(request),
            kb_ids_in=None if kb_id else _visible_kb_ids(request))
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(list(qa_outcomes.EXPORT_COLUMNS))
        for row in rows:
            # kb_ids 是 JSON 数组列，导出时拍平成逗号分隔（CSV 里再嵌一层
            # JSON 引号，Excel 打开会变成一团转义符，没法读）
            flat = {**row, "kb_ids": (",".join(row["kb_ids"])
                                      if row.get("kb_ids") else None)}
            writer.writerow([_csv_cell(flat.get(col))
                             for col in qa_outcomes.EXPORT_COLUMNS])
        # utf-8-sig 带 BOM：Excel（中文/马来语 Windows 环境）否则会乱码
        body = "\ufeff" + buf.getvalue()
        return Response(
            content=body.encode("utf-8"), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     'attachment; filename="qa-outcomes.csv"'})

    @router.get("/stats/outcomes/{outcome_id}", dependencies=[deps.require_admin])
    def get_outcome(outcome_id: str, request: Request):
        """单条下钻：归因行 + 该轮助手消息原文与 citations。

        两条可见性都要过（缺一就会出现"清单里看不到、按 id 却能捞出来"）：
        先看行在不在（同时拿到它的 kb_id），再过库守卫；行本身带 kb_id 时按它
        判定，多库联查行为 NULL（kb_id 未记录）——此时没有可判定的库，按
        查看者可见性集合兜底；最后叠审计分层（超管行对非超管不可见）。
        任何一条不过都回 404 outcome_not_found，不区分原因。
        """
        found = qa_outcomes.get_outcome(
            sf, outcome_id, exclude_actors=_exclude_actors(request))
        if found is None:
            raise _not_found(outcome_id)
        kb_id = found["kb_id"]
        kb_ids = found["kb_ids"] or []
        if kb_id:
            guard.kb(kb_id, request)
        else:
            visible = _visible_kb_ids(request)
            if visible is not None and not ({*kb_ids} & visible):
                raise _not_found(outcome_id)
        return found


def _not_found(outcome_id: str) -> AppError:
    """无权与不存在同一响应（见 get_outcome 的注释）。"""
    return AppError("error.outcome_not_found", "归因记录不存在: {id}",
                    status=404, id=outcome_id)
