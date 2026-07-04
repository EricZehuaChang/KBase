"""结构分块：沿 Markdown 标题切父块（章节），父块内按长度切叶子块。
chunk_size 按字符计（中文场景下与 token 数近似 1:1）。"""
import uuid

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from kbase.plugins.base import ChunkData
from kbase.plugins.registry import registry

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]


@registry.register("chunker", "structure")
class StructureChunker:
    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        self._header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=_HEADERS, strip_headers=True
        )
        self._text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", "；", " ", ""],
        )

    def chunk(self, markdown: str, doc_name: str) -> list[ChunkData]:
        out: list[ChunkData] = []
        for section in self._header_splitter.split_text(markdown):
            titles = [section.metadata[key] for key in ("h1", "h2", "h3", "h4")
                      if key in section.metadata]
            heading_path = " > ".join([doc_name, *titles])
            parent = ChunkData(id=str(uuid.uuid4()), text=section.page_content,
                               heading_path=heading_path)
            out.append(parent)
            pieces = self._text_splitter.split_text(section.page_content)
            leaves = [ChunkData(id=str(uuid.uuid4()), text=p,
                                heading_path=heading_path, parent_id=parent.id)
                      for p in pieces]
            for i, leaf in enumerate(leaves):
                leaf.prev_id = leaves[i - 1].id if i > 0 else None
                leaf.next_id = leaves[i + 1].id if i < len(leaves) - 1 else None
            out.extend(leaves)
        return out
