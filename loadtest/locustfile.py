"""KBase standard 栈压测脚本（M4-2 Task H6，spec docs/superpowers/specs/2026-07-06-kbase-m4-2-design.md §4）。

压测口径：只打 POST /api/kb/{kb_id}/search（向量化 TEI-embed + Qdrant 双路
检索 + PG 关键词 + RRF 融合 + TEI-rerank），不含 LLM 生成（那是单独的 SSE
/api/kb/{kb_id}/query 路径，走 spec §11 的另一个验收口径）。

用法（在 VM 上本机跑，取纯服务端口径，避免 SSH 隧道引入的网络延迟）：

    source ~/loadtest-venv/bin/activate
    export KBASE_HOST=http://localhost:8100
    export KBASE_API_KEY=kbase_ak_xxx
    export KBASE_KB_ID=<loadtest-kb 的 id>
    locust -f loadtest/locustfile.py --headless -u 100 -r 20 -t 90s \
        --csv=loadtest/results/run_100 --host "$KBASE_HOST"

环境变量：
- KBASE_API_KEY（必需）：压测客户端走 Bearer API Key 鉴权（与真实客户端
  一致的鉴权路径，不绕过 auth 层）。
- KBASE_KB_ID（必需）：目标知识库 id（本次用专门种入几百个 chunk 的
  loadtest-kb，避免打空库拿到失真的低延迟数字）。
- KBASE_TOP_K（可选，默认 5）。
"""
import os
import random

from locust import HttpUser, task, between

API_KEY = os.environ["KBASE_API_KEY"]
KB_ID = os.environ["KBASE_KB_ID"]
TOP_K = int(os.environ.get("KBASE_TOP_K", "5"))

# 小池子里轮换的真实中文查询——覆盖精确术语、自然语言问句、长尾模糊问法，
# 都是 loadtest-kb 语料（差旅/住房/绩效/培训/合同/社保等兵团政策类文档）
# 真实会出现的问法，避免压测查询在语义上"太容易/太难"而失真。
QUERIES = [
    "住房补贴的申领条件是什么",
    "差旅费报销标准",
    "年度带薪休假天数如何计算",
    "劳动合同解除的赔偿标准",
    "绩效考核的等级如何划分",
    "培训费用可以报销多少",
    "招聘录用的基本流程",
    "社会保险和公积金的缴纳比例",
    "办公设备采购需要哪些审批",
    "信息安全管理的责任部门是谁",
    "财务报销需要哪些凭证",
    "档案保存年限是多久",
    "安全生产责任制的主要内容",
    "车辆管理办法适用范围",
    "对外合作合同审批权限",
    "科研经费使用的监督机制",
    "知识产权归属如何约定",
    "职工福利包含哪些项目",
    "干部选拔任用的基本条件",
    "内部审计监督的重点内容",
]


class LoadTestUser(HttpUser):
    # 无思考时间的纯并发吞吐测试：between(0, 0) 让 locust 尽快重复发请求，
    # 用户数（-u）本身就是并发控制旋钮，符合 spec §4"梯度 10/50/100 并发"
    # 的口径（并发用户数，不是叠加 think-time 后的稀释并发）。
    wait_time = between(0, 0)

    @task
    def search(self):
        query = random.choice(QUERIES)
        self.client.post(
            f"/api/kb/{KB_ID}/search",
            json={"query": query, "top_k": TOP_K, "debug": False},
            headers={"Authorization": f"Bearer {API_KEY}"},
            name="/api/kb/[id]/search",
        )
