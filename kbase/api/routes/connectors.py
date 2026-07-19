"""同步连接器路由（对标清单#3）：连接器 CRUD + 手动立即同步。
同步引擎与调度器在 kbase/connectors.py；本模块只做 HTTP 编排。
权限：全部 editor 起步（连接器属内容管理域，与上传/删除文档同级）。"""
import json
import uuid

from fastapi import BackgroundTasks, HTTPException

from kbase import connectors as conn_mod
from kbase import feishu
from kbase.api.routes import RouteDeps
from kbase.api.schemas import ConnectorCreate, ConnectorUpdate
from kbase.api.services import Services
from kbase.models import Connector, ConnectorDoc, KnowledgeBase


def register(router, svc: Services, deps: RouteDeps):
    sf, cfg, store, keyword_index, pipeline = (
        svc.sf, svc.cfg, svc.store, svc.keyword_index, svc.pipeline)

    def _sync(connector_id: str):
        """调度器与手动同步共用的入口（闭包捆绑服务依赖）。"""
        return conn_mod.sync_connector(sf, pipeline, store, keyword_index,
                                       cfg.data_dir, connector_id)

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
    def list_connectors(kb_id: str):
        with sf() as s:
            if s.get(KnowledgeBase, kb_id) is None:
                raise HTTPException(404, f"知识库不存在: {kb_id}")
            rows = (s.query(Connector).filter_by(kb_id=kb_id)
                    .order_by(Connector.created_at.asc()).all())
            counts = {r.id: (s.query(ConnectorDoc)
                             .filter_by(connector_id=r.id).count())
                      for r in rows}
            return [_to_public(r, counts[r.id]) for r in rows]

    @router.post("/kb/{kb_id}/connectors",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def create_connector(kb_id: str, body: ConnectorCreate,
                         bg: BackgroundTasks):
        """创建即触发首次同步（后台）。凭据未配置 409——前端据此就地引导
        （与一次性导入端点同约定）。"""
        with sf() as s:
            if s.get(KnowledgeBase, kb_id) is None:
                raise HTTPException(404, f"知识库不存在: {kb_id}")
        if body.type == "feishu":
            app_id, app_secret = feishu.get_credentials(sf)
            if not (app_id and app_secret):
                raise HTTPException(409, "未配置飞书应用凭据（app_id/app_secret）")
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
    def update_connector(connector_id: str, body: ConnectorUpdate):
        with sf() as s:
            row = s.get(Connector, connector_id)
            if row is None:
                raise HTTPException(404, f"连接器不存在: {connector_id}")
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
    def delete_connector(connector_id: str, purge_docs: bool = False):
        """删除连接器。purge_docs=true 连带删除已同步文档；默认保留
        （转普通文档，脱离同步管理）。"""
        ok = conn_mod.delete_connector(sf, store, keyword_index, cfg.data_dir,
                                       connector_id, purge_docs)
        if not ok:
            raise HTTPException(404, f"连接器不存在: {connector_id}")
        return {"ok": True, "purged": purge_docs}

    @router.post("/connectors/{connector_id}/sync",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def sync_now(connector_id: str, bg: BackgroundTasks):
        """手动立即同步（后台执行，前端轮询列表看进度）。已在同步中 409
        ——查询与后台抢锁之间的窄竞态由抢锁兜底（输家静默退出）。"""
        with sf() as s:
            row = s.get(Connector, connector_id)
            if row is None:
                raise HTTPException(404, f"连接器不存在: {connector_id}")
            if row.last_sync_status == "running":
                raise HTTPException(409, "该连接器正在同步中")
        bg.add_task(_sync, connector_id)
        return {"accepted": True}

    return _sync
