"""Prometheus 指标文本（D 运维：/metrics 出口）。

手写 Prometheus 文本格式，零新依赖（不引 prometheus_client——私有化交付少
一个依赖就少一处 CVE 面与版本冲突）。指标来源：审计表累计计数（qa_stats）
+ 检索器进程级重排计数（retriever.rerank_stats）+ 健康态。

Prometheus 规范：每个指标先 # HELP/# TYPE 再样本行；counter 语义为单调
递增总量，速率由 Prometheus 侧 rate() 计算，本端点只吐当前累计值。
"""


def _line(name: str, value, help_text: str, mtype: str = "gauge") -> str:
    return (f"# HELP {name} {help_text}\n"
            f"# TYPE {name} {mtype}\n"
            f"{name} {value}\n")


def render(counters: dict, rerank_stats: dict, reranker_status: str) -> str:
    """组装 Prometheus 文本。counters=qa_stats.lifetime_counters()，
    rerank_stats=retriever.rerank_stats，reranker_status∈{on,off,degraded}。"""
    out = [
        _line("kbase_query_total", counters["query_total"],
              "累计问答请求数", "counter"),
        _line("kbase_query_refused_total", counters["refused_total"],
              "累计拒答（检索无依据）数", "counter"),
        _line("kbase_login_failed_total", counters["login_failed_total"],
              "累计登录失败数", "counter"),
        _line("kbase_rerank_total", rerank_stats.get("rerank_total", 0),
              "累计重排调用数", "counter"),
        _line("kbase_rerank_shed_load_total",
              rerank_stats.get("rerank_shed_load_total", 0),
              "累计重排过载主动降级数", "counter"),
        _line("kbase_rerank_error_total",
              rerank_stats.get("rerank_error_total", 0),
              "累计重排失败降级数", "counter"),
    ]
    # reranker 健康态映射为数值 gauge（Prometheus 告警不便用字符串）：
    # 1=on 正常，0=off 未启用，-1=degraded 降级（可据此告警）。
    status_val = {"on": 1, "off": 0, "degraded": -1}.get(reranker_status, 0)
    out.append(_line("kbase_reranker_status", status_val,
                     "重排状态 1=on 0=off -1=degraded"))
    return "".join(out)
