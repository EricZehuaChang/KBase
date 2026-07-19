"""连接器框架（对标清单#3）：外部数据源 → 定时增量同步 → 活知识库。

三块职责，都在本模块（一期只有飞书一个源，不值得拆包）：
1. 源适配（_SOURCE_TYPES 分派）：源类型只需实现两个方法——
   list_docs() 拉清单+版本指纹（不拉正文，省 API 配额）、
   fetch(entry) 拉单篇正文转 Markdown+图片任务。新增 Notion/Confluence
   等类型在 _SOURCE_TYPES 注册即可复用全套 diff/调度/摄取逻辑。
2. 同步引擎 sync_connector：清单 vs connector_docs 映射做 diff——
   新增→摄取；指纹变→拉正文，内容 hash 变才删旧摄新（防"版本信号变
   但内容没变"的无谓重摄）；源缺失→prune 开关决定是否删除（镜像语义）；
   单篇失败不拖垮整批（与摄取管道同哲学）。
3. 调度器 ConnectorScheduler：daemon 线程每 tick 扫到期连接器串行同步
   （lite 档单进程友好，不引入任务队列依赖）。并发防护=DB 抢锁
   （UPDATE ... WHERE last_sync_status != 'running'），standard 档多
   worker 部署下同一连接器也不会双跑。
"""
import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from kbase import feishu
from kbase.audit import write_audit
from kbase.doc_delete import delete_document_cascade
from kbase.models import Connector, ConnectorDoc

logger = logging.getLogger(__name__)

SOURCE_TYPES = ("feishu",)


# ---- 飞书源适配 ----

class FeishuSource:
    """飞书 wiki 源：config.source 收 wiki 节点链接（同步该子树）或
    space_id（同步整个空间）——与一次性导入端点同语义。"""

    def __init__(self, sf, config: dict):
        app_id, app_secret = feishu.get_credentials(sf)
        if not (app_id and app_secret):
            raise ValueError("未配置飞书应用凭据（app_id/app_secret）")
        self._token = feishu._get_token(app_id, app_secret)
        self._source = str(config.get("source") or "").strip()
        if not self._source:
            raise ValueError("连接器缺少 source（wiki 链接或 space_id）")

    @property
    def token(self) -> str:
        return self._token

    def list_docs(self) -> list[dict]:
        """清单+指纹：[{key, title, fingerprint, raw}]。raw 是 fetch 需要的
        原始条目（obj_token/path/title），框架层不关心其结构。"""
        docs = _resolve_feishu_docs(self._token, self._source)
        return [{"key": d["obj_token"], "title": d["title"],
                 "fingerprint": d.get("edit_time") or "", "raw": d}
                for d in docs]

    def fetch(self, entry: dict) -> tuple[str, list]:
        """单篇正文：(markdown, 图片下载任务列表)。"""
        images: list = []
        markdown = feishu.doc_to_markdown(self._token, entry["raw"],
                                          images_out=images)
        return markdown, images

    def save_images(self, sf, data_dir, doc_id: str, images: list) -> None:
        feishu.save_doc_images(sf, data_dir, self._token, doc_id, images)


def _resolve_feishu_docs(token: str, source: str) -> list[dict]:
    """source（wiki 链接或 space_id）→ 可导入文档清单。与一次性导入端点
    同解析规则：链接=定位空间与子树根（根自身是 docx 也纳入），否则当
    space_id 导整个空间。"""
    from urllib.parse import urlparse

    if source.startswith("http"):
        seg = [p for p in urlparse(source).path.split("/") if p]
        if "wiki" not in seg:
            raise ValueError("不是飞书 wiki 链接（路径需含 /wiki/）")
        node_token = seg[seg.index("wiki") + 1]
        node = feishu.resolve_node(token, node_token)
        space_id = str(node.get("space_id"))
        root_title = node.get("title") or "未命名"
        docs = feishu.collect_wiki_docs(
            token, space_id, root_node_token=node.get("node_token"),
            root_path=[root_title])
        if node.get("obj_type") == "docx" and node.get("obj_token"):
            docs.insert(0, {"obj_token": node["obj_token"],
                            "title": root_title, "path": [],
                            "edit_time": str(node.get("obj_edit_time") or "")})
        return docs
    return feishu.collect_wiki_docs(token, source)


def build_source(type_: str, sf, config: dict):
    """源类型分派。未知类型抛 ValueError（创建端点已校验，这里兜底）。"""
    if type_ == "feishu":
        return FeishuSource(sf, config)
    raise ValueError(f"未知的连接器类型: {type_}")


# ---- 同步引擎 ----

def _acquire_lock(sf, connector_id: str) -> bool:
    """DB 抢锁：running 状态即持锁。单条 UPDATE 的 WHERE 保证并发/多进程
    下只有一个赢家（SQLite 写串行化、PG 行锁，两个方言都成立）。"""
    with sf() as s:
        n = (s.query(Connector)
             .filter(Connector.id == connector_id,
                     Connector.last_sync_status.is_distinct_from("running"))
             .update({"last_sync_status": "running"},
                     synchronize_session=False))
        s.commit()
        return bool(n)


def _finish(sf, connector_id: str, status: str, stats: dict | None,
            error: str | None) -> None:
    with sf() as s:
        row = s.get(Connector, connector_id)
        if row is None:
            return                        # 同步期间连接器被删：结果无处落，丢弃
        row.last_sync_status = status
        row.last_sync_at = datetime.utcnow()
        row.last_sync_error = error
        row.last_sync_stats = (json.dumps(stats, ensure_ascii=False)
                               if stats else None)
        s.commit()


def sync_connector(sf, pipeline, store, keyword_index, data_dir: Path,
                   connector_id: str) -> dict | None:
    """执行一次增量同步。返回 stats（未抢到锁/连接器不存在返回 None）。
    diff 语义（对标 Onyx/RAGFlow 的同步型数据源）：
    - added：清单有、映射无 → 拉正文摄取，记映射；
    - updated：指纹变 → 拉正文；markdown hash 变 → 删旧摄新（活文档），
      hash 没变 → 只刷新指纹（skipped 计数）；
    - skipped：指纹没变 → 不拉正文（一次 list 调用即完成，增量的意义）；
    - pruned：映射有、清单无且 prune=True → 删除本地文档（该文档仍被其他
      连接器映射引用时只删自己的映射行，防跨连接器误删共享文档）；
    - failed：单篇异常计数，不拖垮整批。"""
    if not _acquire_lock(sf, connector_id):
        return None
    with sf() as s:
        row = s.get(Connector, connector_id)
        if row is None:
            return None
        kb_id, type_, name = row.kb_id, row.type, row.name
        config = json.loads(row.config or "{}")

    stats = {"added": 0, "updated": 0, "skipped": 0, "pruned": 0, "failed": 0}
    try:
        source = build_source(type_, sf, config)
        listing = source.list_docs()
    except Exception as e:  # noqa: BLE001 —— 源不可达/凭据失效：整体失败
        logger.warning("连接器 %s 同步失败（清单阶段）: %s", connector_id, e)
        _finish(sf, connector_id, "failed", None, str(e)[:500])
        write_audit(sf, actor="connector", action="connector_sync_failed",
                    resource=f"connector={connector_id}", detail=str(e)[:400])
        return None

    with sf() as s:
        mapping = {m.source_key: m for m in
                   s.query(ConnectorDoc)
                   .filter_by(connector_id=connector_id).all()}
        s.expunge_all()                   # 后续跨 session 只读用（id/hash 字段）

    uploads = Path(data_dir) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    seen_keys: set[str] = set()

    for item in listing:
        key = item["key"]
        seen_keys.add(key)
        known = mapping.get(key)
        if known is not None and known.fingerprint == item["fingerprint"]:
            stats["skipped"] += 1        # 指纹没变：不拉正文
            continue
        try:
            markdown, images = source.fetch(item)
            content_hash = hashlib.sha256(
                markdown.encode("utf-8")).hexdigest()
            if known is not None and known.content_hash == content_hash:
                # 版本信号变了但内容没变（如权限调整碰了 edit_time）：
                # 只刷新指纹，不动文档
                _update_mapping(sf, known.id, fingerprint=item["fingerprint"])
                stats["skipped"] += 1
                continue
            if known is not None:
                # 变更重摄：删旧才摄新——(kb_id, content_hash) 唯一索引下，
                # 旧行不删新行进不去；删除级联与手动删除同一事实源
                delete_document_cascade(sf, store, keyword_index, data_dir,
                                        kb_id, known.doc_id)
            filename = f"{Path(item['title']).name or '未命名'}.md"
            dest = uploads / f"{uuid.uuid4()}-{filename}"
            dest.write_text(markdown, encoding="utf-8")
            doc_id = pipeline.ingest_file(kb_id, dest, filename, "auto")
            source.save_images(sf, data_dir, doc_id, images)
            if known is not None:
                _update_mapping(sf, known.id, doc_id=doc_id,
                                fingerprint=item["fingerprint"],
                                content_hash=content_hash,
                                title=item["title"])
                stats["updated"] += 1
            else:
                with sf() as s:
                    s.add(ConnectorDoc(
                        id=str(uuid.uuid4()), connector_id=connector_id,
                        source_key=key, doc_id=doc_id,
                        fingerprint=item["fingerprint"],
                        content_hash=content_hash, title=item["title"]))
                    s.commit()
                stats["added"] += 1
        except Exception as e:  # noqa: BLE001 —— 单篇隔离
            logger.warning("连接器 %s 文档 %r 同步失败: %s",
                           connector_id, item.get("title"), e)
            stats["failed"] += 1

    # prune：映射有、源清单没有 → 源侧已删除
    with sf() as s:
        row = s.get(Connector, connector_id)
        prune = bool(row.prune) if row is not None else False
    for key, known in mapping.items():
        if key in seen_keys:
            continue
        try:
            if prune:
                _prune_doc(sf, store, keyword_index, data_dir, kb_id,
                           connector_id, known.doc_id)
                stats["pruned"] += 1
            with sf() as s:
                gone = s.get(ConnectorDoc, known.id)
                if gone is not None:
                    s.delete(gone)
                    s.commit()
        except Exception as e:  # noqa: BLE001 —— 单篇隔离
            logger.warning("连接器 %s prune %r 失败: %s",
                           connector_id, known.title, e)
            stats["failed"] += 1

    status = "done_with_errors" if stats["failed"] else "done"
    _finish(sf, connector_id, status, stats, None)
    write_audit(sf, actor="connector", action="connector_sync",
                resource=f"kb_id={kb_id} connector={name or connector_id}",
                detail=json.dumps(stats, ensure_ascii=False))
    return stats


def _update_mapping(sf, mapping_id: str, **fields) -> None:
    with sf() as s:
        m = s.get(ConnectorDoc, mapping_id)
        if m is None:
            return
        for k, v in fields.items():
            setattr(m, k, v)
        m.updated_at = datetime.utcnow()
        s.commit()


def _prune_doc(sf, store, keyword_index, data_dir, kb_id: str,
               connector_id: str, doc_id: str) -> None:
    """删除被 prune 的文档；该 doc 仍被其他连接器的映射引用时不删文档
    （两个连接器子树重叠 + content_hash 去重会共享同一 Document 行）。"""
    with sf() as s:
        others = (s.query(ConnectorDoc)
                  .filter(ConnectorDoc.doc_id == doc_id,
                          ConnectorDoc.connector_id != connector_id).count())
    if others == 0:
        delete_document_cascade(sf, store, keyword_index, data_dir,
                                kb_id, doc_id)


def delete_connector(sf, store, keyword_index, data_dir: Path,
                     connector_id: str, purge_docs: bool) -> bool:
    """删除连接器。purge_docs=True 连带删除全部已同步文档（同样尊重
    跨连接器共享防御）；False=文档保留，转成普通文档。"""
    with sf() as s:
        row = s.get(Connector, connector_id)
        if row is None:
            return False
        kb_id = row.kb_id
        docs = (s.query(ConnectorDoc)
                .filter_by(connector_id=connector_id).all())
        s.expunge_all()
    if purge_docs:
        for m in docs:
            _prune_doc(sf, store, keyword_index, data_dir, kb_id,
                       connector_id, m.doc_id)
    with sf() as s:
        s.query(ConnectorDoc).filter_by(connector_id=connector_id).delete()
        row = s.get(Connector, connector_id)
        if row is not None:
            s.delete(row)
        s.commit()
    return True


# ---- 调度器 ----

class ConnectorScheduler:
    """轻量定时器：daemon 线程每 tick_seconds 醒一次，扫出到期连接器
    （enabled、interval>0、非 running、last_sync_at 空或已过期）串行同步。
    连接器数量是个位数量级，全查内存滤，不做 SQL 侧到期计算。

    启动路径：api/main.py 的 startup 钩子（TestClient 非 with 用法不触发
    startup，既有测试零线程泄漏）；shutdown 钩子 stop() 响应式退出。"""

    def __init__(self, sf, sync_fn, tick_seconds: int = 60):
        self._sf = sf
        self._sync_fn = sync_fn          # (connector_id) -> stats|None
        self._tick = tick_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="connector-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 首 tick 也等一个周期：启动风暴（迁移/索引恢复）先落定
        while not self._stop.wait(self._tick):
            try:
                for cid in self.due_connector_ids():
                    if self._stop.is_set():
                        return
                    self._sync_fn(cid)
            except Exception:  # noqa: BLE001 —— 调度循环永不因单轮异常退出
                logger.exception("连接器调度 tick 异常")

    def due_connector_ids(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.utcnow()
        due: list[str] = []
        with self._sf() as s:
            for row in s.query(Connector).filter(Connector.enabled.is_(True),
                                                 Connector.interval_minutes > 0):
                if row.last_sync_status == "running":
                    continue
                if (row.last_sync_at is None
                        or row.last_sync_at
                        + timedelta(minutes=row.interval_minutes) <= now):
                    due.append(row.id)
        return due


def reset_stale_running(sf) -> int:
    """启动恢复：进程崩溃/强杀会把 running 状态永久残留，该连接器从此
    抢不到锁再也不同步。启动时一律复位为 failed 并注明中断。"""
    with sf() as s:
        n = (s.query(Connector)
             .filter(Connector.last_sync_status == "running")
             .update({"last_sync_status": "failed",
                      "last_sync_error": "上次同步因服务重启中断"},
                     synchronize_session=False))
        s.commit()
        return n
