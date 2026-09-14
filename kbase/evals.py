"""检索评测回归（B）：评测集 CRUD + 一键回归（hit@k / MRR）+ 历史对比。

设计取舍：
- 只评检索不评生成——检索指标确定、免 LLM 费用、秒级出结果；生成质量
  依赖模型且难自动判分，留给人工抽查（运营看板已有拒答清单）。
- 用例判中规则：expect_doc（命中块所属文档名相等）或 expect_text
  （期望文本是命中块正文的子串），二者有其一即可、都给则任一命中算中。
- hit@k = 命中用例数/总数；MRR = 平均(1/首个命中块的名次)，未命中记 0。
- 每次回归落一行 eval_runs 快照（含逐用例明细），调参前后各跑一次对比
  hit/mrr 即回答"这次改配置是变好还是变坏"。
"""
import json
import uuid

from kbase.models import EvalRun, EvalSet


def create_set(sf, kb_id: str, name: str, cases: list[dict]) -> dict:
    """建评测集。cases 每条至少要有 question 和 expect_doc/expect_text 之一
    （schema 层已校验，这里直接存）。"""
    row = EvalSet(id=str(uuid.uuid4()), kb_id=kb_id, name=name,
                  cases=json.dumps(cases, ensure_ascii=False))
    with sf() as s:
        s.add(row)
        s.commit()
        return {"id": row.id, "kb_id": kb_id, "name": name,
                "case_count": len(cases)}


def list_sets(sf, kb_id: str) -> list[dict]:
    with sf() as s:
        rows = (s.query(EvalSet).filter(EvalSet.kb_id == kb_id)
                .order_by(EvalSet.created_at.desc()).all())
        return [{"id": r.id, "name": r.name,
                 "case_count": len(json.loads(r.cases)),
                 "created_at": r.created_at.isoformat()} for r in rows]


def get_set(sf, set_id: str) -> EvalSet | None:
    with sf() as s:
        return s.get(EvalSet, set_id)


def delete_set(sf, set_id: str) -> bool:
    with sf() as s:
        row = s.get(EvalSet, set_id)
        if row is None:
            return False
        s.query(EvalRun).filter(EvalRun.set_id == set_id).delete()
        s.delete(row)
        s.commit()
        return True


def _case_hit_rank(case: dict, blocks) -> int | None:
    """返回该用例首个命中块的名次（1 起），未命中返回 None。"""
    expect_doc = case.get("expect_doc")
    expect_text = case.get("expect_text")
    for rank, b in enumerate(blocks, start=1):
        if expect_doc and b.doc_name == expect_doc:
            return rank
        if expect_text and expect_text in b.text:
            return rank
    return None


def _retrieval_judgeable(case: dict) -> bool:
    """该用例能否被**检索**判分（即给了 expect_doc / expect_text）。

    T13：标问回灌的用例还带 expected_answer（参考答案，供答案级判分 T15），
    它不是检索期望——只带它的用例检索侧无从判中，不能算进 hit@k/MRR 的
    分母，否则"回灌一条答案级用例"就会凭空空拉低 hit 率、把回归曲线带偏。
    """
    return bool(case.get("expect_doc") or case.get("expect_text"))


def run_eval(sf, retriever, set_id: str, *, top_k: int = 5,
             strategy=None) -> dict | None:
    """对评测集跑一遍检索回归，落 eval_runs 快照并返回结果。
    strategy 传 None=用调用方解析好的 KB 策略跑（与线上问答同路径）。

    逐用例都检索（答案级判分 T15 要用同一批命中块），但 **hit@k/MRR 只统计
    有检索期望的用例**；`total` = 参与检索判分的用例数，未被检索判分的用例如
    数另给 `skipped`——两者都报出来，口径变化在结果里可见。
    """
    with sf() as s:
        row = s.get(EvalSet, set_id)
        if row is None:
            return None
        kb_id, cases = row.kb_id, json.loads(row.cases)

    details, hits, rr_sum, total, skipped = [], 0, 0.0, 0, 0
    for case in cases:
        blocks = retriever.retrieve(kb_id, case["question"], top_k,
                                    False, strategy)
        judgeable = _retrieval_judgeable(case)
        if not judgeable:
            skipped += 1
            details.append({"question": case["question"], "rank": None,
                            "hit": None, "top_doc": (blocks[0].doc_name if blocks else None),
                            "expected_answer": case.get("expected_answer"),
                            "retrieval_judged": False})
            continue
        rank = _case_hit_rank(case, blocks)
        total += 1
        if rank is not None:
            hits += 1
            rr_sum += 1.0 / rank
        details.append({"question": case["question"], "rank": rank,
                        "hit": rank is not None,
                        "top_doc": (blocks[0].doc_name if blocks else None),
                        "expected_answer": case.get("expected_answer"),
                        "retrieval_judged": True})

    hit_rate = (hits / total) if total else 0.0
    mrr = (rr_sum / total) if total else 0.0
    run = EvalRun(id=str(uuid.uuid4()), set_id=set_id, top_k=top_k,
                  hit_rate=hit_rate, mrr=mrr, total=total,
                  detail=json.dumps(details, ensure_ascii=False))
    with sf() as s:
        s.add(run)
        s.commit()
        return {"id": run.id, "set_id": set_id, "top_k": top_k,
                "hit_rate": round(hit_rate, 4), "mrr": round(mrr, 4),
                "total": total, "hits": hits, "skipped": skipped,
                "case_count": len(cases), "details": details,
                "created_at": run.created_at.isoformat()}


def list_runs(sf, set_id: str, limit: int = 20) -> list[dict]:
    """历史回归清单（新→旧），前端并排画 hit/mrr 折线做趋势对比。"""
    with sf() as s:
        rows = (s.query(EvalRun).filter(EvalRun.set_id == set_id)
                .order_by(EvalRun.created_at.desc()).limit(limit).all())
        return [{"id": r.id, "top_k": r.top_k,
                 "hit_rate": round(r.hit_rate, 4), "mrr": round(r.mrr, 4),
                 "total": r.total, "created_at": r.created_at.isoformat()}
                for r in rows]


def get_run(sf, run_id: str) -> dict | None:
    """单次回归的逐用例明细（排查哪些问题掉出了 top-k）。"""
    with sf() as s:
        r = s.get(EvalRun, run_id)
        if r is None:
            return None
        return {"id": r.id, "set_id": r.set_id, "top_k": r.top_k,
                "hit_rate": round(r.hit_rate, 4), "mrr": round(r.mrr, 4),
                "total": r.total, "details": json.loads(r.detail),
                "created_at": r.created_at.isoformat()}
