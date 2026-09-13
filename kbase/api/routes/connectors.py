"""同步连接器路由（对标清单#3）：连接器 CRUD + 手动立即同步。
同步引擎与调度器在 kbase/connectors.py；本模块只做 HTTP 编排。
权限：全部 editor 起步（连接器属内容管理域，与上传/删除文档同级）。"""
import json
import uuid

from fastapi import BackgroundTasks, Request

from kbase import connectors as conn_mod
from kbase import feishu
from kbase.api.guards import KbGuard
from kbase.api.routes import RouteDeps
from kbase.api.schemas import ConnectorCreate, ConnectorUpdate
from kbase.api.services import Services
from kbase.errors import AppError
from kbase.models import Connector, ConnectorDoc


def register(router, svc: Services, deps: RouteDeps):
    sf, cfg, store, keyword_index, pipeline = (
        svc.sf, svc.cfg, svc.store, svc.keyword_index, svc.pipeline)
    # T02/G09：连接器全端点按所属 kb 过 ACL + scope（修前只判"kb 是否存在"
    # + 角色，受限 key 与无授权 editor 都能读/建他库连接器）。
    guard = KbGuard(sf)

    def _sync(connector_id: str):
        """调度器与手动同步共用的入口（闭包捆绑服务依赖）。"""
        return conn_mod.sync_connector(sf, pipeline, store, keyword_index,
                                       cfg.data_dir, connector_id)

    def _guard_connector(connector_id: str, request) -> str:
        """按连接器所属 kb 过 ACL + scope；连接器不存在/无权统一 404。"""
        with sf() as s:
            row = s.get(Connector, connector_id)
            if row is None:
                raise AppError("error.connector_not_found", "连接器不存在: {id}", status=404, id=connector_id)
            kb_id = row.kb_id
        guard.kb(kb_id, request)
        return kb_id

    def _to_public(row: Connector, doc_count: int) -> dict:
        return {
            "id": row.id, "kb_id": row.kb_id, "type": row.type,
            "name": row.name, "config": json.loads(row.config or "{}"),
            "enabled": bool(row.enabled),
            "interval_minutes": row.interval_minutes,
            "prune": bool(row.prune),
            "last_sync_at": row.last_sync_at,
            "last_sync_status": row.last_sync_status,
            "last_sync_error": row.last_sync_error,
            "last_sync_stats": (json.loads(row.last_sync_stats)
                                if row.last_sync_stats else None),
            "doc_count": doc_count,
            "created_at": row.created_at,
        }

    @router.get("/kb/{kb_id}/connectors", dependencies=[deps.require_editor])
    def list_connectors(kb_id: str, request: Request):
        guard.kb(kb_id, request)
        with sf() as s:
            rows = (s.query(Connector).filter_by(kb_id=kb_id)
                    .order_by(Connector.created_at.asc()).all())
            counts = {r.id: (s.query(ConnectorDoc)
                             .filter_by(connector_id=r.id).count())
                      for r in rows}
            return [_to_public(r, counts[r.id]) for r in rows]

    @router.post("/kb/{kb_id}/connectors",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def create_connector(kb_id: str, body: ConnectorCreate, request: Request,
                         bg: BackgroundTasks):
        """创建即触发首次同步（后台）。凭据未配置 409——前端据此就地引导
        （与一次性导入端点同约定）。"""
        guard.kb(kb_id, request)
        if body.type == "feishu":
            app_id, app_secret = feishu.get_credentials(sf)
            if not (app_id and app_secret):
                raise AppError("error.feishu_creds_missing",
                               "未配置飞书应用凭据（app_id/app_secret）", status=409)
        row = Connector(id=str(uuid.uuid4()), kb_id=kb_id, type=body.type,
                        name=body.name.strip(),
                        config=json.dumps({"source": body.source.strip()},
                                          ensure_ascii=False),
                        interval_minutes=body.interval_minutes,
                        prune=body.prune)
        with sf() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
            public = _to_public(row, 0)
        bg.add_task(_sync, public["id"])
        return public

    @router.put("/connectors/{connector_id}",
                dependencies=[deps.require_editor, deps.audit_mutation])
    def update_connector(connector_id: str, body: ConnectorUpdate,
                         request: Request):
        _guard_connector(connector_id, request)
        with sf() as s:
            row = s.get(Connector, connector_id)
            if body.name is not None:
                row.name = body.name.strip()
            if body.enabled is not None:
                row.enabled = body.enabled
            if body.interval_minutes is not None:
                row.interval_minutes = body.interval_minutes
            if body.prune is not None:
                row.prune = body.prune
            s.commit()
            s.refresh(row)
            count = (s.query(ConnectorDoc)
                     .filter_by(connector_id=connector_id).count())
            return _to_public(row, count)

    @router.delete("/connectors/{connector_id}",
                   dependencies=[deps.require_editor, deps.audit_mutation])
    def delete_connector(connector_id: str, request: Request,
                         purge_docs: bool = False):
        """删除连接器。purge_docs=true 连带删除已同步文档；默认保留
        （转普通文档，脱离同步管理）。"""
        _guard_connector(connector_id, request)
        ok = conn_mod.delete_connector(sf, store, keyword_index, cfg.data_dir,
                                       connector_id, purge_docs)
        if not ok:
            raise AppError("error.connector_not_found", "连接器不存在: {id}", status=404, id=connector_id)
        return {"ok": True, "purged": purge_docs}

    @router.post("/connectors/{connector_id}/sync",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def sync_now(connector_id: str, request: Request, bg: BackgroundTasks):
        """手动立即同步（后台执行，前端轮询列表看进度）。已在同步中 409
        ——查询与后台抢锁之间的窄竞态由抢锁兜底（输家静默退出）。"""
        _guard_connector(connector_id, request)
        with sf() as s:
            row = s.get(Connector, connector_id)
            if row.last_sync_status == "running":
                raise AppError("error.connector_syncing", "该连接器正在同步中", status=409)
        bg.add_task(_sync, connector_id)
        return {"accepted": True}

    return _sync
