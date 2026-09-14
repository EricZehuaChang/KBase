"""MCP 扩展域路由（T14）：MCP 工具需要、而既有路由没有暴露的两类只读端点。

- `GET /api/chunks/{chunk_id}`：单块详情（正文/标题链/页码/版式/所属文档）；
- `GET /api/documents/{doc_id}/outline`：文档章节树（父块按 heading_path 去重）。

守卫姿态与既有端点**有意不同**：两道闸门分开判、失败语义也分开
（见 kbase/api/guards.py 的说明）——
- 库级 ACL（无授权 / 资源不存在）→ 404 doc_not_found / chunk_not_found；
- API Key 库级 scope 越权 → **静默空集**（`{}` / `[]`），与 MCP 检索契约
  一致（tests/test_apikey_scope.py 钉着：外部无法区分"库不存在/无权/真没内容"）。
"""
import json
from types import SimpleNamespace

from fastapi import Request

from kbase import kb_acl
from kbase.api.guards import KbGuard
from kbase.api.routes import RouteDeps
from kbase.api.services import Services
from kbase.models import Chunk, Document


def _in_scope(request, kb_id: str) -> bool:
    """API Key 库级 scope（T14）：受限 key 的白名单判定统一走
    kb_acl.scope_allows，与 /search、/query、/v1 同一份实现（不另写一份
    判定，改一处漏一处就是越权口子）。actor 取不到时按"无 scope"兜底，
    与 KbGuard._actor 的姿态一致。"""
    actor = getattr(request.state, "actor", None) if request is not None else None
    return kb_acl.scope_allows(actor or {}, kb_id)


def _acl_view(request):
    """摘掉 actor 上 scope_kb_ids 的 request 替身（T14）。

    为什么需要它：KbGuard.allows 判的是「ACL **且** scope」，两条闸门被
    合并成同一个 404。本文件的两个端点要的是**两种不同的失败语义**——
    ACL 挡=404（不泄漏"存在但无权"），scope 挡=静默空集（与 /search、/query
    的越权语义一致，tests/test_apikey_scope.py 钉着）。所以先把 scope 维度
    摘掉交给 KbGuard 拿实体（缺失/ACL 无权 → 404，逻辑仍只有守卫这一份），
    再单独判 scope 决定是回空集还是实体。
    """
    actor = dict(getattr(request.state, "actor", None) or {"role": "admin"})
    actor.pop("scope_kb_ids", None)
    return SimpleNamespace(state=SimpleNamespace(actor=actor))


def _build_outline(rows: list[Chunk]) -> list[dict]:
    """父块 → 章节树（T14）。

    chunker 契约（plugins/chunkers/structure.py）：heading_path =
    "文件名 > h1 > h2 > ..."，首段恒是文件名，标题链从第二段起算；整篇没有任何
    标题时 heading_path 就等于文件名（此时以文件名为唯一的根节点）。

    两个"顺序"约定：
    - 同一 heading_path 的重复父块（同一标题在文中出现多次）只留**首个**；
    - 章节树按父块的**出现顺序**挂接，不按 heading_path 重排——那是字典序，
      中文标题（"第二章" vs "第一章"）一旦重排就与原文顺序不符。
    """
    roots: list[dict] = []
    nodes: dict[tuple, dict] = {}
    seen: set[str] = set()
    for c in rows:
        if c.heading_path in seen:
            continue
        seen.add(c.heading_path)
        parts = [p.strip() for p in c.heading_path.split(" > ") if p.strip()]
        if not parts:
            continue
        head, titles = parts[0], parts[1:] or parts[:1]
        level, prefix = roots, ()
        for title in titles:
            key = (*prefix, title)
            node = nodes.get(key)
            if node is None:
                # 祖先标题若没有自己的父块（纯标题、无正文的章节不落库），
                # 这里按需补一个空壳父节点，保证树不断层
                node = {"title": title,
                        "heading_path": " > ".join([head, *key]),
                        "children": []}
                nodes[key] = node
                level.append(node)
            level, prefix = node["children"], key
    return roots


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf
    # T02/G09：以 chunk_id / doc_id 为参数的端点先过 KbGuard 拿实体——不存在
    # 与 ACL 无权同一副面孔（404）；scope 维度由 _acl_view 摘掉后单独判（见
    # 该函数的注释：两条闸门的失败语义不同）。
    guard = KbGuard(sf)

    @router.get("/chunks/{chunk_id}", dependencies=[deps.require_viewer])
    def get_chunk(chunk_id: str, request: Request):
        """单块详情（MCP get_chunk 的数据源）：正文、标题链、页码、版式
        （表格块的解析后 JSON，非表格块 null）与所属文档。
        受限 key 越权 → `{}`（静默空集，不报错不提示）。"""
        chunk = guard.chunk(chunk_id, _acl_view(request))
        if not _in_scope(request, chunk.kb_id):
            return {}
        with sf() as s:
            doc = s.get(Document, chunk.doc_id)
            doc_name = doc.filename if doc is not None else None
        return {"doc_id": chunk.doc_id, "doc_name": doc_name,
                "heading_path": chunk.heading_path, "text": chunk.text,
                "page": chunk.page,
                "layout": json.loads(chunk.layout) if chunk.layout else None}

    @router.get("/documents/{doc_id}/outline", dependencies=[deps.require_viewer])
    def document_outline(doc_id: str, request: Request):
        """文档章节树（MCP get_document_outline 的数据源）：父块按
        heading_path 去重后按出现顺序挂成树，节点形状
        `{title, heading_path, children}`。
        受限 key 越权 → `[]`（静默空集，与 chunk 端点同一姿态）。"""
        doc = guard.doc(doc_id, _acl_view(request))
        if not _in_scope(request, doc.kb_id):
            return []
        with sf() as s:
            # 不写 ORDER BY：行的插入顺序就是文档顺序（摄取时按 chunker 输出
            # 顺序 s.add，chunks 行此后只原地更新不重排），而 heading_path 是
            # "文件名 + 标题链"的字符串，按它排序得到的是字典序、会把章节顺序
            # 打乱——"出现顺序"只能取物理行序（SQLite/PG 单表顺序扫描对只追加
            # 的表都返回插入序）。
            rows = (s.query(Chunk)
                    .filter_by(doc_id=doc_id, is_leaf=False).all())
            return _build_outline(rows)
