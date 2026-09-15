"""批量导入批次（T18）的**只读**接口：清单 / 明细 / CSV 导出。

红线（与 bulk_import.py 的文件头、models.ImportBatch 的注释同一条）：
**本文件刻意不提供任何触发导入的端点**。触发路径只有命令行
`python -m kbase.bulk_import`——万级文件的首轮灌库要能断点续传，而
BackgroundTasks 是进程内任务、重启即丢（bulk_import.py 的模块 docstring 已把
这个取舍写在明处）。任何人想加 POST /api/import-batches 之前，请先回去读那段。

写入方只有 CLI：批次行由 bulk_import.start_batch/finish_batch 落库。本模块
读到的 manifest_path 是相对 data_dir 的相对路径（写侧已做越界拒绝），读侧
再走一遍 resolve_manifest_path 做包含性校验——历史/手工改库的数据不能借一次
GET 换出任意文件读；越界或文件没了只是"清单不可读"，不把接口打成 500。

权限：列表/明细 viewer 起（只读运维视角，与审计/归因下钻同一档），库级可见性
一律走 T02 的 KbGuard（ACL + API Key scope，库外资源统一 404）。
"""
import csv
import io
from pathlib import Path

from fastapi import Query, Request
from fastapi.responses import Response

from kbase import bulk_import as bi
from kbase.api.guards import KbGuard
from kbase.api.routes import RouteDeps
from kbase.api.services import Services
from kbase.errors import AppError


def _not_found(batch_id: str) -> AppError:
    return AppError("error.import_batch_not_found", "导入批次不存在: {id}",
                    status=404, id=batch_id)


def _csv_cell(value) -> str:
    """CSV 单元格：None → 空串（不是字面 "None"——导出给人看，空就是没有）。
    换行/逗号交给 csv 模块转义，不在这里手工处理。"""
    return "" if value is None else str(value)


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf, data_dir = svc.sf, Path(svc.cfg.data_dir)
    guard = KbGuard(sf)

    def _guarded(batch_id: str, request: Request) -> dict:
        """批次行 + 库级守卫 + id 形态校验，三件事一处做。

        路线与 routes/jobs.py 一致：先确认批次存在（**非法 id 形态在这一步就
        按不存在处理**，见 bulk_import.safe_batch_id），再按它的 kb_id 过
        ACL+scope，无权一律 404——不泄漏"存在但无权"，也不给批量探测
        （"401 vs 404"的差别）留通道。"""
        batch = bi.get_batch(sf, batch_id)
        if batch is None:
            raise _not_found(batch_id)
        guard.kb(batch["kb_id"], request)
        return batch

    @router.get("/import-batches", dependencies=[deps.require_viewer])
    def list_import_batches(request: Request, kb_id: str | None = Query(default=None),
                            limit: int = Query(default=50, ge=1, le=200)):
        """批次清单（新→旧）。kb_id 给了就按库过滤并过库守卫；不给则是"我有权
        看的全部"——逐行按各自 kb_id 过滤（与 list_kb 的多库可见性同一手法：
        不能因为没带 kb_id 就把别人的批次漏出来）。

        顺带做一次一致性回收：停在 running 但清单已覆盖全部待处理项的批次
        其实是跑完了（收尾时进程挂了），这里读的时候把它收尾——页面不需要
        重试刷新就能看到真实状态。
        """
        bi.reconcile_stale_running(sf, data_dir)
        if kb_id:
            guard.kb(kb_id, request)
        out = bi.list_batches(sf, kb_id=kb_id, limit=limit)
        if kb_id:
            return out
        # 无 kb_id：逐行复核可见性（ACL+scope 都为真才留）
        out["items"] = [b for b in out["items"]
                        if guard.allows(b["kb_id"], request)]
        out["total"] = len(out["items"])
        return out

    @router.get("/import-batches/{batch_id}", dependencies=[deps.require_viewer])
    def import_batch_detail(batch_id: str, request: Request):
        """单批次汇总。明细在 /entries（清单文件可能上万行，不塞进详情）。"""
        batch = _guarded(batch_id, request)
        batch["manifest_available"] = bi.manifest_available(
            data_dir, batch["manifest_path"])
        return batch

    @router.get("/import-batches/{batch_id}/entries",
                dependencies=[deps.require_viewer])
    def import_batch_entries(batch_id: str, request: Request,
                             failures_only: bool = Query(default=False),
                             limit: int = Query(default=200, ge=1, le=2000),
                             offset: int = Query(default=0, ge=0)):
        """批次明细（逐文件台账，读清单 JSONL）。failures_only=true 只看失败
        ——批量导入的支持工单 90% 是"哪些文件没进去"，这一条就是给他们用的。"""
        _guarded(batch_id, request)
        return bi.read_entries(sf, data_dir, batch_id,
                              failures_only=failures_only, limit=limit,
                              offset=offset)

    @router.get("/import-batches/{batch_id}/export.csv",
                dependencies=[deps.require_viewer])
    def import_batch_export(batch_id: str, request: Request,
                            failures_only: bool = Query(default=False)):
        """明细 CSV 导出：**列完整**（IMPORT_COLUMNS 是列顺序的唯一事实源，
        含失败原因的原始 error 文本——排障时缺这一列等于白导）。
        utf-8-sig 带 BOM：Excel（中文/马来语 Windows 环境）否则会乱码。
        """
        batch = _guarded(batch_id, request)
        rows = bi.read_entries(sf, data_dir, batch_id,
                              failures_only=failures_only, offset=0,
                              limit=bi.EXPORT_MAX_ROWS)["items"]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(list(bi.IMPORT_COLUMNS))
        for row in rows:
            writer.writerow([_csv_cell(row.get(col)) for col in bi.IMPORT_COLUMNS])
        body = "\ufeff" + buf.getvalue()
        # 文件名带批次 id 前 8 位：同时下多轮导出时不会互相覆盖
        name = f"import-{batch['id'][:8]}.csv"
        return Response(
            content=body.encode("utf-8"), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'})
