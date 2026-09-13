"""库级访问守卫（T02/G09）：把「ACL + API Key scope 两道闸门」收敛到一处。

09-14 代码走查发现：只有 `list_kb` 做了 ACL + scope 过滤，kb.py 里所有
**以 doc_id / chunk_id 为参数**的端点只校验角色（或什么都不校验）——受限
API Key 或无授权 viewer 只要拿到 id 就能读写他库文档；evals / jobs /
connectors / share 的守卫各自只查了 ACL 或什么都不查。id 是 uuid4 不可枚举，
但 id 会出现在引用、分享、审计与日志里，不能当作权限。

两条闸门必须**都**过（顺序无所谓，都是 404）：
- `kb_acl.can_access`：M6-3 库级 ACL（admin 豁免、owner 豁免、无 grant=公开）；
- `kb_acl.scope_allows`：API Key 白名单。**独立于角色**——受限的 admin key
  在 can_access 那里会被豁免，只有 scope 能挡住它。

失败一律 404（不泄漏"存在但无权"），这也是本文件与检索/问答路径的区别：
那条路对 scope 越权用「静默空集」语义（tests/test_apikey_scope.py 钉着），
这里没有那个既有契约，读类端点对外统一 404。

用法（路由模块在 register 里建一次）：
    guard = KbGuard(sf)
    guard.kb(kb_id, request)          # /api/kb/{kb_id}/... 这类端点
    guard.doc(doc_id, request)        # 返回 doc 对象（已确认有权）
    guard.chunk(chunk_id, request)    # 返回 chunk 对象
"""
from kbase import kb_acl
from kbase.errors import AppError
from kbase.models import Chunk, Document


class KbGuard:
    def __init__(self, sf):
        self.sf = sf

    # ---- 内部：actor 读写 ----

    @staticmethod
    def _actor(request) -> dict:
        """actor 取不到时按 admin 兜底：与既有 `_guard_kb` / list_kb 的写法
        一致（auth="off" 下路由级依赖已写入合成超管 actor，这里只是防御性
        兜底，不会真的把匿名请求放行——鉴权依赖在路由上先执行）。"""
        return (getattr(request.state, "actor", None)
                if request is not None else None) or {"role": "admin"}

    @staticmethod
    def _kb_not_found(kb_id: str) -> AppError:
        return AppError("error.kb_not_found", "知识库不存在: {id}",
                        status=404, id=kb_id)

    @staticmethod
    def _doc_not_found(doc_id: str) -> AppError:
        return AppError("error.doc_not_found", "文档不存在: {id}",
                        status=404, id=doc_id)

    @staticmethod
    def _chunk_not_found(chunk_id: str) -> AppError:
        return AppError("error.chunk_not_found", "分块不存在: {id}",
                        status=404, id=chunk_id)

    # ---- 判定 ----

    def allows(self, kb_id: str, request) -> bool:
        """该请求的 actor 能否访问 kb_id（ACL 且 scope）。"""
        actor = self._actor(request)
        return (kb_acl.can_access(self.sf, kb_id, actor)
                and kb_acl.scope_allows(actor, kb_id))

    def kb(self, kb_id: str, request) -> None:
        """kb 级守卫：不存在 / 无权 / 超出 key scope 一律 404。"""
        if not self.allows(kb_id, request):
            raise self._kb_not_found(kb_id)

    # ---- 取实体 + 判定（返回已确认有权的行）----

    def doc(self, doc_id: str, request) -> Document:
        """按 doc_id 定位文档→查它的 kb_id→走 kb 守卫。查不到文档或守卫
        不过都 404 doc_not_found（对外与"文档不存在"不可区分）。"""
        with self.sf() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                raise self._doc_not_found(doc_id)
            kb_id = doc.kb_id
            s.expunge(doc)
        if not self.allows(kb_id, request):
            raise self._doc_not_found(doc_id)
        return doc

    def chunk(self, chunk_id: str, request) -> Chunk:
        """按 chunk_id 定位分块→查它的 kb_id→走 kb 守卫。"""
        with self.sf() as s:
            chunk = s.get(Chunk, chunk_id)
            if chunk is None:
                raise self._chunk_not_found(chunk_id)
            kb_id = chunk.kb_id
            s.expunge(chunk)
        if not self.allows(kb_id, request):
            raise self._chunk_not_found(chunk_id)
        return chunk
