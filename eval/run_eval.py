# eval/run_eval.py
"""检索命中率 + 答案关键词覆盖率评测，支持多 provider 对比。
用法：
  python eval/run_eval.py --kb <kb_id> --providers qwen-plus,qwen-max
输出：eval/report.md（生成产物，不入库）
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from kbase.config import load_config
from kbase.db import make_session_factory
from kbase.plugins.registry import registry
from kbase.rag.generator import Generator
from kbase.rag.retriever import Retriever


def build_retriever(cfg):
    import kbase.plugins.embedders.bge_local      # noqa: F401
    import kbase.plugins.llm.openai_compat        # noqa: F401
    import kbase.plugins.vectorstores.chroma_store  # noqa: F401
    sf = make_session_factory(f"sqlite:///{cfg.data_dir}/kbase.sqlite")
    embedder = registry.create("embedder", cfg.embedder.name, model=cfg.embedder.model)
    store = registry.create("vectorstore", cfg.vectorstore.name,
                            persist_dir=str(cfg.data_dir / "chroma"))
    return Retriever(sf, embedder, store)


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

    for pname in providers:
        p = cfg.get_provider(pname)
        llm = registry.create("llm", "openai-compat", base_url=p.base_url,
                              api_key_env=p.api_key_env, model=p.model,
                              max_concurrency=p.max_concurrency, params=p.params)
        gen = Generator(llm)
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
    ap.add_argument("--providers", required=True)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--out", default="eval/report.md")
    asyncio.run(run(ap.parse_args()))
