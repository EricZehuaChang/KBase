"""附图上限：步骤类章节几十张操作截图不能全灌进一条引用（产线 19 张反馈）。
直接驱动 attach_images，不过 API 层。"""
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kbase.doc_images import MAX_IMAGES_PER_CITATION, attach_images
from kbase.models import Base, DocumentImage


def test_attach_images_caps_per_citation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cap.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)

    doc_id = str(uuid.uuid4())
    with sf() as s:
        # 同一章节 8 张操作截图（heading 锚为标题链）
        for i in range(8):
            s.add(DocumentImage(id=str(uuid.uuid4()), doc_id=doc_id, page=0,
                                heading="用户管理 > 添加",
                                filename=f"fs{i + 1}.png", width=800, height=600))
        s.commit()

    citations = [{"doc_id": doc_id, "doc_name": "手册.md", "page": None,
                  "heading_path": "手册.md > 驾驶舱 > 用户管理 > 添加"}]
    attach_images(sf, citations)
    assert len(citations[0]["images"]) == MAX_IMAGES_PER_CITATION
    # 按文档内出现顺序取前 N 张
    assert citations[0]["images"][0]["name"] == "fs1.png"
