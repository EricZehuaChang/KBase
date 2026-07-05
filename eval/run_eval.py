# eval/run_eval.py
"""检索命中率 + 答案关键词覆盖率评测，支持多 provider 对比。
用法：
  python eval/run_eval.py --kb <kb_id> --providers qwen-plus,qwen-max

档位对比模式（只做检索命中评测，不生成，快）：
  python eval/run_eval.py --tiers --kb <kb_id> --questions <questions.jsonl> --out <tiers.md>

输出：eval/report.md（生成产物，不入库）
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from kbase.config import load_config
from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.plugins.registry import registry
from kbase.rag.generator import Generator
from kbase.rag.retriever import Retriever


def _build_components(cfg):
    """组装检索所需的公共组件（session factory / embedder / store），
    三个档位共用同一份，避免重复加载 bge-m3。"""
    import kbase.plugins.embedders.bge_local      # noqa: F401
    import kbase.plugins.llm.openai_compat        # noqa: F401
    import kbase.plugins.vectorstores.chroma_store  # noqa: F401
    sf = make_session_factory(f"sqlite:///{cfg.data_dir}/kbase.sqlite")
    embedder = registry.create("embedder", cfg.embedder.name, model=cfg.embedder.model)
    store = registry.create("vectorstore", cfg.vectorstore.name,
                            persist_dir=str(cfg.data_dir / "chroma"))
    return sf, embedder, store


def build_retriever(cfg):
    sf, embedder, store = _build_components(cfg)
    return Retriever(sf, embedder, store)


def _build_tier_retrievers(cfg):
    """构造三个 Retriever 变体：纯向量 / 混合 / 混合+重排。
    重排模型加载失败时该档跳过（返回 None），不阻塞其余档位的评测。
    返回 dict[档位名, Retriever | None]（None 表示跳过，附带原因见 print）。"""
    sf, embedder, store = _build_components(cfg)
    tiers: dict[str, Retriever | None] = {}

    tiers["纯向量"] = Retriever(sf, embedder, store,
                              candidates=cfg.retrieval.candidates,
                              rrf_k=cfg.retrieval.rrf_k)

    kw = KeywordIndex(sf)
    tiers["混合"] = Retriever(sf, embedder, store, keyword_index=kw,
                            candidates=cfg.retrieval.candidates,
                            rrf_k=cfg.retrieval.rrf_k)

    try:
        import kbase.plugins.rerankers.bge_local  # noqa: F401
        reranker = registry.create("reranker", "bge-local",
                                   model=cfg.retrieval.rerank.model)
        tiers["混合+重排"] = Retriever(sf, embedder, store, keyword_index=kw,
                                    reranker=reranker,
                                    candidates=cfg.retrieval.candidates,
                                    rrf_k=cfg.retrieval.rrf_k)
    except Exception as e:  # noqa: BLE001 —— 重排模型加载失败时该档跳过，不阻塞其余档位
        print(f"提示：重排模型加载失败，跳过「混合+重排」档位：{e}", file=sys.stderr)
        tiers["混合+重排"] = None

    return tiers


def run_tiers(args):
    """档位对比模式：只做检索命中评测（retrieval-only），不调用 LLM 生成。"""
    if args.providers:
        print("提示：--tiers 模式忽略 --providers（档位对比只评测检索，不生成）",
              file=sys.stderr)
    cfg = load_config(args.config)
    questions = load_questions(args.questions)
    tiers = _build_tier_retrievers(cfg)

    lines = ["# KBase 检索档位对比", "",
             f"- 题目数：{len(questions)}，top_k={args.top_k}", "",
             "| 档位 | 命中率 |", "|---|---|"]
    detail_lines = ["", "## 逐题命中详情", ""]

    for tier_name, retriever in tiers.items():
        if retriever is None:
            lines.append(f"| {tier_name} | 跳过（模型加载失败） |")
            continue
        hit_count = 0
        detail_lines.append(f"### {tier_name}")
        for q in questions:
            blocks = retriever.retrieve(args.kb, q["question"], top_k=args.top_k)
            hit = any(q["expect_doc"] in b.doc_name for b in blocks)
            hit_count += hit
            mark = "✓" if hit else "✗"
            detail_lines.append(f"- {mark} {q['question']}")
        lines.append(f"| {tier_name} | {hit_count}/{len(questions)} |")
        detail_lines.append("")

    out = Path(args.out)
    out.write_text("\n".join(lines + detail_lines), encoding="utf-8")
    print(f"完成:{out}")


def load_questions(path: str) -> list[dict]:
    """逐行解析 JSONL；出错时报告文件名+行号（1-based），避免定位困难。
    encoding="utf-8-sig" 可同时兼容有/无 BOM 的文件。"""
    p = Path(path)
    questions = []
    for i, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            questions.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"错误：{p}: 第 {i} 行 JSON 解析失败：{e}", file=sys.stderr)
            sys.exit(1)
    return questions


async def run(args):
    cfg = load_config(args.config)
    retriever = build_retriever(cfg)
    questions = load_questions(args.questions)
    providers = args.providers.split(",")
    rows, hit_count = [], 0

    retrievals = {}
    for q in questions:
        blocks = retriever.retrieve(args.kb, q["question"], top_k=args.top_k)
        hit = any(q["expect_doc"] in b.doc_name for b in blocks)
        hit_count += hit
        retrievals[q["question"]] = (blocks, hit)

    # 与 main.py:276 生产逻辑对齐：按 retriever 是否启用重排选 min_score 档位，
    # 并传 min_include_score（收录底线）。build_retriever() 走的是"纯向量/混合"
    # 档（未传 reranker），故这里 rerank_active 恒为 False、恒取 min_score_dense；
    # 写成与生产相同的三目而非直接硬编码 min_score_dense，是为了在
    # build_retriever 将来也变成 rerank 感知时不必再改这行，且评测的拒答门
    # 阈值语义与线上永远一致，不会因为忘记同步而跑出虚高/虚低的评测分。
    gen_min_score = (cfg.retrieval.min_score_rerank if retriever.rerank_active
                     else cfg.retrieval.min_score_dense)

    for pname in providers:
        p = cfg.get_provider(pname)
        llm = registry.create("llm", "openai-compat", base_url=p.base_url,
                              api_key_env=p.api_key_env, model=p.model,
                              max_concurrency=p.max_concurrency, params=p.params)
        gen = Generator(llm, min_score=gen_min_score,
                        min_include_score=cfg.retrieval.min_include_score)
        for q in questions:
            blocks, hit = retrievals[q["question"]]
            usable = gen.usable_blocks(blocks)
            answer = "".join([t async for t in
                              gen.answer_stream(q["question"], usable)])
            covered = [k for k in q["expect_keywords"] if k in answer]
            rows.append({"provider": pname, "question": q["question"],
                         "retrieval_hit": hit,
                         "keyword_coverage": f"{len(covered)}/{len(q['expect_keywords'])}",
                         "answer": answer[:200]})

    lines = ["# KBase 评测报告", "",
             f"- 题目数：{len(questions)}，top_k={args.top_k}",
             f"- 检索命中率：{hit_count}/{len(questions)}", "",
             "| Provider | 问题 | 检索命中 | 关键词覆盖 | 答案(截断) |",
             "|---|---|---|---|---|"]
    for r in rows:
        ans = r["answer"].replace("\n", " ").replace("|", "\\|")
        q = r["question"].replace("|", "\\|")
        lines.append(f"| {r['provider']} | {q} | "
                     f"{'✓' if r['retrieval_hit'] else '✗'} | "
                     f"{r['keyword_coverage']} | {ans} |")
    out = Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"完成:{out}（{len(rows)} 行）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/kbase.yaml")
    ap.add_argument("--questions", default="eval/questions.jsonl")
    ap.add_argument("--kb", required=True)
    ap.add_argument("--providers", required=False)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--out", default="eval/report.md")
    ap.add_argument("--tiers", action="store_true",
                    help="档位对比模式：纯向量/混合/混合+重排检索命中率对比，忽略 --providers")
    args = ap.parse_args()
    if args.tiers:
        run_tiers(args)
    else:
        if not args.providers:
            ap.error("--providers 为必填（非 --tiers 模式）")
        asyncio.run(run(args))
