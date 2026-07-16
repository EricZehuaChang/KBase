"""飞书知识库连接器（一期）：凭据管理 + wiki 节点树遍历 + docx blocks →
Markdown 转换。

设计参照（调研结论）：树遍历照 longbridge/feishu-pages 的思路（wiki v2
nodes 分页递归），块转换照 Wsine/feishu2md 的映射表；API 用 httpx 直连
（一期只打 4 个端点，不引入官方 SDK 依赖）。

层级保留（用户核心诉求）：每个 wiki 节点导入为一个独立文档，节点在树中
的路径注入 Markdown 标题链首部——结构分块器按标题层级做父子分块后，
heading_path 即完整层级链（"空间路径 > 文档内章节"），检索命中直接呈现。

凭据存 AppSetting KV（页面维护，GET 脱敏），与 Provider/向量密钥同规矩。
网络调用集中在本模块顶层函数，测试打桩这些函数即可全程不出网。
"""
import logging
import time

import httpx

from kbase.models import AppSetting

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"
APP_ID_KEY = "feishu_app_id"
APP_SECRET_KEY = "feishu_app_secret"

# tenant_access_token 进程级缓存（有效期 ~2h，提前 5 分钟过期重取）
_token_cache: dict = {"token": None, "expire_at": 0.0}


# ---- 凭据 KV（页面维护） ----

def get_credentials(sf) -> tuple[str | None, str | None]:
    with sf() as s:
        aid = s.get(AppSetting, APP_ID_KEY)
        sec = s.get(AppSetting, APP_SECRET_KEY)
        return (aid.value if aid else None, sec.value if sec else None)


def set_credentials(sf, app_id: str, app_secret: str) -> None:
    with sf() as s:
        for key, value in ((APP_ID_KEY, app_id), (APP_SECRET_KEY, app_secret)):
            row = s.get(AppSetting, key)
            if row is None:
                s.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        s.commit()
    _token_cache["token"] = None          # 换凭据即作废旧 token


def delete_credentials(sf) -> bool:
    with sf() as s:
        rows = [s.get(AppSetting, k) for k in (APP_ID_KEY, APP_SECRET_KEY)]
        found = any(r is not None for r in rows)
        for r in rows:
            if r is not None:
                s.delete(r)
        s.commit()
    _token_cache["token"] = None
    return found


def credentials_status(sf) -> dict:
    """脱敏状态：app_id 明文（非密钥，公开标识）、secret 只回尾4位。"""
    app_id, secret = get_credentials(sf)
    return {"configured": bool(app_id and secret),
            "app_id": app_id,
            "secret_hint": (f"…{secret[-4:]}" if secret and len(secret) >= 4
                            else None)}


# ---- 飞书 API（网络层，测试打桩点） ----

def _get_token(app_id: str, app_secret: str) -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire_at"]:
        return _token_cache["token"]
    resp = httpx.post(f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": app_id, "app_secret": app_secret},
                      timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败: {data.get('msg')} (code={data.get('code')})")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire_at"] = now + data.get("expire", 3600) - 300
    return _token_cache["token"]


def _api_get(token: str, path: str, params: dict | None = None) -> dict:
    resp = httpx.get(f"{FEISHU_BASE}{path}", params=params or {},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书接口错误 {path}: {data.get('msg')} "
                           f"(code={data.get('code')})")
    return data.get("data", {})


def resolve_node(token: str, node_token: str) -> dict:
    """wiki 节点 URL 里的 token → 节点信息（含 space_id/obj_token/title）。"""
    data = _api_get(token, "/wiki/v2/spaces/get_node",
                    {"token": node_token, "obj_type": "wiki"})
    return data.get("node", {})


def list_children(token: str, space_id: str,
                  parent_node_token: str | None = None) -> list[dict]:
    """某节点的直接子节点（分页取全）。"""
    items: list[dict] = []
    page_token = None
    while True:
        params: dict = {"page_size": 50}
        if parent_node_token:
            params["parent_node_token"] = parent_node_token
        if page_token:
            params["page_token"] = page_token
        data = _api_get(token, f"/wiki/v2/spaces/{space_id}/nodes", params)
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            return items
        page_token = data.get("page_token")


def fetch_doc_blocks(token: str, document_id: str) -> list[dict]:
    """docx 全部块（分页取全，平铺列表，树关系在 parent_id/children）。"""
    items: list[dict] = []
    page_token = None
    while True:
        params: dict = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = _api_get(token, f"/docx/v1/documents/{document_id}/blocks", params)
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            return items
        page_token = data.get("page_token")


# ---- blocks → Markdown（映射表照 feishu2md 思路，一期覆盖核心块型） ----

def _text_of(block: dict, field: str) -> str:
    """块的文本内容：{field}.elements[].text_run.content 拼接。"""
    node = block.get(field) or {}
    return "".join(el.get("text_run", {}).get("content", "")
                   for el in node.get("elements", []))


_HEADING_TYPES = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 6, 10: 6, 11: 6}
_HEADING_FIELDS = {3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
                   7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8",
                   11: "heading9"}


def blocks_to_markdown(blocks: list[dict], base_heading_level: int = 0) -> str:
    """飞书 docx 块 → Markdown。base_heading_level：文档内标题整体下移的
    级数（wiki 路径占用了前几级标题时传入，保持层级单调）。
    一期覆盖：段落/标题1-9/无序/有序/代码/引用/分割线/表格；图片等
    其余块型输出占位注释（二期接 media 下载进插图体系）。"""
    by_id = {b.get("block_id"): b for b in blocks}
    page = next((b for b in blocks if b.get("block_type") == 1), None)
    order = (page or {}).get("children", []) or [
        b.get("block_id") for b in blocks if b.get("block_type") != 1]

    lines: list[str] = []
    ordered_counter = 0
    for bid in order:
        b = by_id.get(bid)
        if b is None:
            continue
        btype = b.get("block_type")
        if btype != 13:
            ordered_counter = 0
        if btype == 2:                                   # 段落
            text = _text_of(b, "text")
            if text.strip():
                lines.append(text)
                lines.append("")
        elif btype in _HEADING_TYPES:                    # 标题 1-9（7+ 归并 6）
            level = min(_HEADING_TYPES[btype] + base_heading_level, 6)
            lines.append("#" * level + " " + _text_of(b, _HEADING_FIELDS[btype]))
            lines.append("")
        elif btype == 12:                                # 无序列表
            lines.append("- " + _text_of(b, "bullet"))
        elif btype == 13:                                # 有序列表
            ordered_counter += 1
            lines.append(f"{ordered_counter}. " + _text_of(b, "ordered"))
        elif btype == 14:                                # 代码块
            lines.append("```")
            lines.append(_text_of(b, "code"))
            lines.append("```")
            lines.append("")
        elif btype == 15:                                # 引用
            lines.append("> " + _text_of(b, "quote"))
            lines.append("")
        elif btype == 22:                                # 分割线
            lines.append("---")
            lines.append("")
        elif btype == 31:                                # 表格（招牌能力对接）
            md_table = _table_to_markdown(b, by_id)
            if md_table:
                lines.append(md_table)
                lines.append("")
        elif btype == 27:                                # 图片：一期占位
            lines.append("<!-- 图片（飞书素材，二期接入插图体系） -->")
        # 其余块型（多维表格/画板/附件等）静默跳过——如实不猜内容
    return "\n".join(lines).strip() + "\n"


def _table_to_markdown(table_block: dict, by_id: dict) -> str:
    """表格块 → Markdown 管道表：cells 平铺在 children，按 column_size
    切行；cell 内容为其子块文本拼接。转出的管道表进摄取管道后自动吃到
    表格原子分块+行线性化全套待遇。"""
    prop = (table_block.get("table") or {}).get("property", {})
    cols = prop.get("column_size") or 0
    cell_ids = table_block.get("children", [])
    if not cols or not cell_ids:
        return ""

    def cell_text(cell_id: str) -> str:
        cell = by_id.get(cell_id) or {}
        parts = []
        for child_id in cell.get("children", []):
            child = by_id.get(child_id) or {}
            parts.append(_text_of(child, "text"))
        return " ".join(p for p in parts if p).replace("|", "\\|").strip()

    rows = [cell_ids[i:i + cols] for i in range(0, len(cell_ids), cols)]
    out = []
    for r, row in enumerate(rows):
        out.append("| " + " | ".join(cell_text(c) for c in row) + " |")
        if r == 0:
            out.append("|" + "---|" * cols)
    return "\n".join(out)


# ---- 导入编排 ----

def collect_wiki_docs(token: str, space_id: str,
                      root_node_token: str | None = None,
                      root_path: list[str] | None = None,
                      max_docs: int = 500) -> list[dict]:
    """遍历 wiki 子树，收集可导入的 docx 节点：
    [{obj_token, title, path: [祖先标题...]}]。max_docs 防御超大空间。"""
    results: list[dict] = []

    def walk(parent_token: str | None, path: list[str]) -> None:
        if len(results) >= max_docs:
            return
        for node in list_children(token, space_id, parent_token):
            title = node.get("title") or "未命名"
            if node.get("obj_type") == "docx" and node.get("obj_token"):
                results.append({"obj_token": node["obj_token"],
                                "title": title, "path": path})
            if node.get("has_child"):
                walk(node.get("node_token"), path + [title])

    walk(root_node_token, root_path or [])
    return results


def doc_to_markdown(token: str, doc: dict) -> str:
    """单个 wiki 文档 → 带层级前缀的 Markdown：wiki 路径占标题前几级，
    文档标题接续其后，正文标题整体下移，保证 heading_path 完整呈现
    "空间路径 > 文档 > 文内章节"。"""
    blocks = fetch_doc_blocks(token, doc["obj_token"])
    prefix_lines = []
    level = 1
    for seg in doc["path"]:
        prefix_lines.append("#" * min(level, 5) + " " + seg)
        level += 1
    prefix_lines.append("#" * min(level, 6) + " " + doc["title"])
    body = blocks_to_markdown(blocks, base_heading_level=min(level, 5))
    return "\n\n".join(["\n".join(prefix_lines), body])
