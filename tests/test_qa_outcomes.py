"""T12 问答归因：一次问答一行，四个互斥桶各一条用例；归因行与会话消息/反馈
的关联（message_id 回填、点踩同步）；三个读接口（清单/下钻/CSV 导出）。

判定条件刻意都是确定性的，不依赖 FakeEmbedder 的语义相似度：
- 空库（无任何文档）→ 检索必空 → empty_retrieval；
- 有文档但 min_score_dense=99（任何余弦都过不去）→ 检索到了但无可用依据
  → below_threshold（**必须与"检索为空"分开**，两者的运营动作完全不同）；
- 有文档 + min_score_dense=-1 → 必有可用依据 → answered；
- 受限 API Key 问白名单外的库 → 越权静默空集 → scope_denied（这条路径压根
  没走到检索，"空"不代表库里没资料）。

数据可回填性（写在 kbase/qa_outcomes.py 的模块 docstring 里，这里同样钉一条）：
历史问答当时没记录命中数/最高分，**无法回填**——所以本文件的用例只断言
"从这一轮开始记"，不代表上线即有存量数据。
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from kbase import qa_outcomes
from kbase.api.main import create_app
from tests.test_api import CFG, MD, FakeLLM

# 阈值两档：99=检索有候选但全不过阈值；-1=有候选就算可用
_CFG = """
data_dir: {data_dir}
chunker: {{name: structure, chunk_size: 200, chunk_overlap: 0}}
retrieval: {{min_score_dense: {min_score}}}
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
"""

_ADMIN_PW = "adminpass123"


def _client(tmp_path, fake_embedder, min_score=99.0):
    cfg = tmp_path / f"kbase-{min_score}.yaml"
    cfg.write_text(_CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/"),
                               min_score=min_score),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


@pytest.fixture
def client_on(tmp_path, fake_embedder, monkeypatch):
    """auth="on" 的客户端（已登录 admin=superadmin）。

    归因的两种可见性——受限 API Key 的库 scope、超管活动的审计分层——都挂在
    鉴权依赖上，auth="off" 的合成超管 actor 恒为 superadmin 且无 scope，测不到。
    用与 tests/test_acl_matrix.py 同一套办法（env 设口令 + 真实登录）。
    """
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", _ADMIN_PW)
    cfg = tmp_path / "kbase-on.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data-on").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="on")
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": "admin", "password": _ADMIN_PW})
    assert r.status_code == 200, r.text
    return c


def _drain(c, kb_id, question):
    """跑完一次问答的 SSE（事件本身不是本文件的被测物，读干净即可）。"""
    with c.stream("POST", f"/api/kb/{kb_id}/query",
                  json={"question": question, "top_k": 3}) as r:
        assert r.status_code == 200, r.text
        for _ in r.iter_lines():
            pass


def _rows(app):
    """当前全部归因行（新→旧，按 ts/id 与接口同序），直接读表做断言。
    走 sf 而不是走接口：被测物要包含"写进去的是什么"，不能只验读接口。"""
    from kbase.models import QaOutcome
    with app.state.svc.sf() as s:
        rows = (s.query(QaOutcome)
                .order_by(QaOutcome.ts.desc(), QaOutcome.id.desc()).all())
        return [{c: getattr(r, c) for c in
                 ("id", "channel", "kb_id", "conv_id", "message_id", "actor",
                  "bucket", "retrieved_count", "usable_count", "top_score",
                  "question", "feedback")} for r in rows]


@pytest.fixture
def client_low(tmp_path, fake_embedder):
    """阈值 -1：有候选就算可用（能答桶 + 会话/反馈关联都用它）。"""
    return _client(tmp_path, fake_embedder, min_score=-1.0)


def _doc_kb(c, name="有料库"):
    """建库 + 传一份文档（摄取是同步 bg task，响应返回时已完成）。"""
    kb = c.post("/api/kb", json={"name": name}).json()["id"]
    r = c.post(f"/api/kb/{kb}/documents",
               files=[("files", ("补贴办法.md", MD.encode("utf-8"),
                                 "text/markdown"))])
    assert r.status_code == 200, r.text
    return kb


# ---- 四个桶各一条（互斥，取值来自响应里的 buckets_known / 行本身） ----

def test_bucket_empty_retrieval(tmp_path, fake_embedder):
    """空库提问 → empty_retrieval：库里很可能压根没有这份资料（补文档）。"""
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "空库"}).json()["id"]
    _drain(c, kb, "量子计算机散热方案")

    rows = _rows(c.app)
    assert [r["bucket"] for r in rows] == [qa_outcomes.BUCKET_EMPTY_RETRIEVAL]
    assert rows[0]["retrieved_count"] == 0 and rows[0]["usable_count"] == 0
    # 检索为空时 top_score 是 None 而不是 0：0 会被读成"最高分就是 0"
    assert rows[0]["top_score"] is None
    assert rows[0]["channel"] == "web" and rows[0]["kb_id"] == kb
    assert rows[0]["question"] == "量子计算机散热方案"


def test_bucket_below_threshold(tmp_path, fake_embedder):
    """有文档但最高分过不了阈值 → below_threshold：资料在库里却捞不起来。

    与空检索必须分开——这**不能**记成 empty_retrieval（那会让人以为该补文档，
    实际该调提问口径/切块/阈值）。
    """
    c = _client(tmp_path, fake_embedder, min_score=99.0)
    kb = _doc_kb(c)
    _drain(c, kb, "住房补贴能申领吗")

    rows = _rows(c.app)
    assert len(rows) == 1
    row = rows[0]
    assert row["bucket"] == qa_outcomes.BUCKET_BELOW_THRESHOLD
    assert row["retrieved_count"] > 0          # 检索**到了**东西
    assert row["usable_count"] == 0            # 只是没有可用依据
    assert row["top_score"] is not None        # 有分数 → 与空检索可区分


def test_bucket_answered(tmp_path, fake_embedder):
    """阈值 -1 → 有依据 → answered（正常作答）。"""
    c = _client(tmp_path, fake_embedder, min_score=-1.0)
    kb = _doc_kb(c)
    _drain(c, kb, "住房补贴能申领吗")

    rows = _rows(c.app)
    assert [r["bucket"] for r in rows] == [qa_outcomes.BUCKET_ANSWERED]
    assert rows[0]["usable_count"] > 0
    assert rows[0]["retrieved_count"] >= rows[0]["usable_count"]


def _on_kb(c, name):
    """auth="on" 下建库 + 传文档（上传需要 editor 门槛，admin 已登录）。"""
    kb = c.post("/api/kb", json={"name": name}).json()["id"]
    r = c.post(f"/api/kb/{kb}/documents",
               files=[("files", ("补贴办法.md", MD.encode("utf-8"),
                                 "text/markdown"))])
    assert r.status_code == 200, r.text
    return kb


def _scoped_client(c, scope, *, role="viewer", name="scoped"):
    """受限 API Key 客户端。

    角色只能是 admin/editor/viewer（API Key 不允许 superadmin 角色，见
    ApiKeyCreate 的 Role 校验）——这是"审计分层"的必然结果：key 是集成方身份，
    不是系统 owner。越权问答用例要用 viewer 角色：kb_acl 对 admin 豁免 ACL，
    受限 admin key 会从"库里查得到"一侧放行，测不出 scope 那道闸门。
    """
    r = c.post("/api/settings/api-keys",
               json={"name": name, "role": role, "scope_kb_ids": scope})
    assert r.status_code == 200, r.text
    return TestClient(c.app, headers={"Authorization": f"Bearer {r.json()['key']}"})


def _viewer(c, username):
    """建一个 viewer 账号并登录返回客户端。

    归因行记下的是提问者身份——需要"非超管的提问者"时用它（超管会话发的行会被
    审计分层从普通 admin 的视图里滤掉，用途不同）。
    """
    r = c.post("/api/users", json={"username": username, "role": "viewer",
                                   "password": "v-pw"})
    assert r.status_code == 200, r.text
    cv = TestClient(c.app)
    assert cv.post("/api/auth/login",
                   json={"username": username, "password": "v-pw"}
                   ).status_code == 200
    return cv


def test_bucket_scope_denied_is_security_event(client_on):
    """受限 API Key 问白名单外的库 → scope_denied（用户想问的库根本不让他问）。

    对外仍是静默空集（事件序列与"检索无依据"一个字都不差，防探测），对内必须
    留痕：这条路径不落 query_refused（用户并没有"问了没答案"），归因行是它唯一
    的可见记录。
    """
    c = client_on
    allowed, other = _on_kb(c, "白名单库"), _on_kb(c, "白名单外")
    ck = _scoped_client(c, [allowed], name="scoped-q")

    # 越权问答：HTTP 200（静默空集语义，与能查但没依据同形状）
    _drain(ck, other, "越权提问")

    rows = _rows(c.app)
    assert [r["bucket"] for r in rows] == [qa_outcomes.BUCKET_SCOPE_DENIED]
    row = rows[0]
    assert row["kb_id"] == other
    assert row["retrieved_count"] == 0 and row["usable_count"] == 0
    # 越权是安全事件，不是知识缺口：审计侧不落 query_refused
    from kbase.models import AuditLog
    with c.app.state.svc.sf() as s:
        refused = (s.query(AuditLog)
                   .filter(AuditLog.action == "query_refused").count())
    assert refused == 0

    # 对照：白名单内的库照常问答（"守卫生效"≠"守卫写成全拒"）。桶本身不做
    # 硬编码断言——默认阈值下能否答取决于检索分，这里只钉"不是越权桶"。
    _drain(ck, allowed, "白名单内提问")
    rows = _rows(c.app)
    assert rows[0]["kb_id"] == allowed
    assert rows[0]["bucket"] == qa_outcomes.classify(
        rows[0]["retrieved_count"], rows[0]["usable_count"])
    assert rows[0]["bucket"] != qa_outcomes.BUCKET_SCOPE_DENIED


# ---- 归因行 ↔ 会话消息 / 反馈的关联 ----

def test_conversation_round_backfills_message_id(client_low):
    """会话轮次的归因行 message_id 被回填（append_round 落库后才有助手消息 id）。

    没有这一步，点踩同步（sync_feedback 按 message_id 找行）与下钻（按
    message_id 取答案原文）都连不上。
    """
    c = client_low
    kb = _doc_kb(c)
    conv = c.post("/api/conversations", json={"kb_id": kb}).json()["id"]
    with c.stream("POST", f"/api/conversations/{conv}/query",
                  json={"question": "住房补贴能申领吗", "top_k": 3}) as r:
        assert r.status_code == 200, r.text
        for _ in r.iter_lines():
            pass

    rows = _rows(c.app)
    assert len(rows) == 1
    row = rows[0]
    assert row["bucket"] == qa_outcomes.BUCKET_ANSWERED
    assert row["conv_id"] == conv
    assert row["message_id"], "助手消息 id 没有回填到归因行"

    # 回填的 id 必须真是本轮助手消息（不是随便一个 id）
    from kbase.models import Message
    with c.app.state.svc.sf() as s:
        msg = s.get(Message, row["message_id"])
        assert msg is not None and msg.role == "assistant"
        assert msg.conv_id == conv

    # 下钻接口能按它取出答案原文与 citations
    got = c.get(f"/api/stats/outcomes/{row['id']}").json()
    assert got["message_id"] == row["message_id"]
    assert got["answer"]
    assert isinstance(got["citations"], list) and got["citations"]


def test_downvote_syncs_feedback_onto_outcome(client_low):
    """点踩把 feedback=-1 叠加到原本的桶上（downvote 不是独立的桶）。

    单列成桶会把"答砸了"的问题从它的归因桶里抹掉，反而丢信息——所以这里断言
    bucket 保持 answered，feedback 变成 -1；改成赞同样同步。
    """
    c = client_low
    kb = _doc_kb(c)
    conv = c.post("/api/conversations", json={"kb_id": kb}).json()["id"]
    with c.stream("POST", f"/api/conversations/{conv}/query",
                  json={"question": "住房补贴能申领吗", "top_k": 3}) as r:
        for _ in r.iter_lines():
            pass
    outcome = _rows(c.app)[0]
    assert outcome["feedback"] is None          # 还没评

    r = c.post(f"/api/messages/{outcome['message_id']}/feedback",
               json={"rating": -1, "note": "答非所问"})
    assert r.status_code == 200, r.text
    after = _rows(c.app)[0]
    assert after["feedback"] == -1
    assert after["bucket"] == qa_outcomes.BUCKET_ANSWERED   # 桶不因点踩改变

    # 改主意点赞：覆盖，不留双记录
    c.post(f"/api/messages/{outcome['message_id']}/feedback", json={"rating": 1})
    assert _rows(c.app)[0]["feedback"] == 1


# ---- 读接口：清单 / 下钻 / CSV 导出 ----

def test_outcome_list_filters_and_distribution(tmp_path, fake_embedder):
    """清单：过滤（bucket/channel/kb_id/from/to/limit/offset）与按桶分布。

    分布**不叠加 bucket 过滤**（否则按桶点进去就只看得到自己那一格，等于没有
    分布）；同条件 total 与 items 必须对得上。
    """
    c = _client(tmp_path, fake_embedder, min_score=99.0)
    kb_a, kb_b = _doc_kb(c, "甲库"), c.post("/api/kb", json={"name": "乙库"}).json()["id"]
    _drain(c, kb_a, "甲库问题一")          # below_threshold
    _drain(c, kb_a, "甲库问题二")          # below_threshold
    _drain(c, kb_b, "乙库问题")            # empty_retrieval

    all_rows = c.get("/api/stats/outcomes").json()
    assert all_rows["total"] == 3
    assert len(all_rows["items"]) == 3
    assert all_rows["buckets"][qa_outcomes.BUCKET_BELOW_THRESHOLD] == 2
    assert all_rows["buckets"][qa_outcomes.BUCKET_EMPTY_RETRIEVAL] == 1
    assert all_rows["buckets"][qa_outcomes.BUCKET_ANSWERED] == 0
    assert all_rows["buckets_known"] == list(qa_outcomes.BUCKETS)
    # 新→旧（最近一次提问排在最前）
    assert all_rows["items"][0]["question"] == "乙库问题"

    # 按桶过滤：items 变少，分布不变
    only_below = c.get("/api/stats/outcomes"
                       f"?bucket={qa_outcomes.BUCKET_BELOW_THRESHOLD}").json()
    assert only_below["total"] == 2
    assert {i["bucket"] for i in only_below["items"]} == \
        {qa_outcomes.BUCKET_BELOW_THRESHOLD}
    assert only_below["buckets"] == all_rows["buckets"]

    # 按库 / 渠道过滤
    assert c.get(f"/api/stats/outcomes?kb_id={kb_b}").json()["total"] == 1
    assert c.get("/api/stats/outcomes?channel=web").json()["total"] == 3
    assert c.get("/api/stats/outcomes?channel=feishu").json()["total"] == 0

    # 分页：limit/offset 不重不漏
    page1 = c.get("/api/stats/outcomes?limit=2&offset=0").json()
    page2 = c.get("/api/stats/outcomes?limit=2&offset=2").json()
    assert len(page1["items"]) == 2 and len(page2["items"]) == 1
    assert page1["total"] == page2["total"] == 3
    assert {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]} == set()

    # 时间过滤：from=未来 → 空；to=很早 → 空；非法值 422（不静默当作没传）
    assert c.get("/api/stats/outcomes?from=2999-01-01").json()["total"] == 0
    assert c.get("/api/stats/outcomes?to=2000-01-01").json()["total"] == 0
    assert c.get("/api/stats/outcomes?from=not-a-time").status_code == 422
    # 未知桶 422（不是"这个桶没数据"）
    assert c.get("/api/stats/outcomes?bucket=downvoted").status_code == 422


def test_outcome_detail_and_csv_columns(tmp_path, fake_embedder):
    """下钻带问题原文/答案/citations；CSV 列完整（列顺序由 EXPORT_COLUMNS 定义）。

    CSV 表头与行都按同一份列清单生成——少一列等于导出废掉（question 与
    feedback 是运营真正要的）。
    """
    c = _client(tmp_path, fake_embedder, min_score=-1.0)
    kb = _doc_kb(c)
    _drain(c, kb, "住房补贴能申领吗")
    row = _rows(c.app)[0]

    detail = c.get(f"/api/stats/outcomes/{row['id']}").json()
    assert detail["question"] == "住房补贴能申领吗"
    assert detail["bucket"] == qa_outcomes.BUCKET_ANSWERED
    assert detail["top_score"] is not None
    # 直问路径没有会话消息 → answer/citations 为 None（不是空串：空串会被读成
    # "答了但答案是空的"）
    assert detail["answer"] is None and detail["citations"] is None

    r = c.get("/api/stats/outcomes/export.csv")
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers["content-type"]
    assert "qa-outcomes.csv" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")        # 带 BOM，Excel 不乱码
    assert r.content.startswith("\ufeff".encode())
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(qa_outcomes.EXPORT_COLUMNS)
    assert len(rows) == 2
    body = dict(zip(rows[0], rows[1]))
    # 每一列都有值（完整列 = 表头在、行也填得上）
    assert body["id"] == row["id"] and body["bucket"] == qa_outcomes.BUCKET_ANSWERED
    assert body["kb_id"] == kb and body["channel"] == "web"
    assert body["question"] == "住房补贴能申领吗"
    assert body["feedback"] == ""               # 没评 → 空串（不是字面 None）
    assert body["top_score"] and body["retrieved_count"]

    # 导出与清单同口径：过滤参数一样时条数一致
    assert len(list(csv.reader(io.StringIO(text)))) - 1 == \
        c.get("/api/stats/outcomes").json()["total"]
    assert len(list(csv.reader(io.StringIO(
        c.get(f"/api/stats/outcomes/export.csv?kb_id={kb}")
        .content.decode("utf-8-sig"))))) == 2        # 表头 + 1 行


def test_outcome_detail_unknown_id_404(tmp_path, fake_embedder):
    """未知 id 一律 404（不泄漏"存在但无权"）。"""
    c = _client(tmp_path, fake_embedder)
    assert c.get("/api/stats/outcomes/not-exist").status_code == 404


def test_outcome_visibility_follows_kb_acl(client_on):
    """可见性：受限 API Key 的白名单外归因行不进清单、下钻也 404。

    面板不指定库时最容易漏——"没选库"不等于"看全站"，否则一个受限 key 就能把
    白名单外库的知识缺口连同提问原文一起读走（归因行里存的是问题全文，比审计的
    100 字前缀更能泄漏内容）。

    admin 角色的 key 也要被 scope 挡住：kb_acl 对 admin 豁免 ACL，只判 ACL 的话
    受限 admin key 反而看得最多。

    本用例的问答由一个普通用户（不是超管）发出：API Key 的角色只能是
    admin/editor/viewer，而任何非超管查看者都会被审计分层滤掉超管的行，用超管
    会话发的行会一并滤没（那是另一条规则，见
    test_superadmin_activity_is_invisible_to_ordinary_admin）。
    """
    c = client_on
    kb_a, kb_b = _on_kb(c, "甲库"), _on_kb(c, "乙库")
    cu = _viewer(c, "归因查看者")
    _drain(cu, kb_a, "甲库问题")
    _drain(cu, kb_b, "乙库问题")
    row_b, row_a = _rows(c.app)          # 新→旧：先乙后甲
    assert row_b["kb_id"] == kb_b and row_a["kb_id"] == kb_a
    assert {row_a["actor"], row_b["actor"]} == {"归因查看者"}

    ck = _scoped_client(c, [kb_a], role="admin", name="scoped-list")

    visible = ck.get("/api/stats/outcomes").json()
    assert visible["total"] == 1
    assert [i["kb_id"] for i in visible["items"]] == [kb_a]
    assert visible["buckets"] == {b: (1 if b == row_a["bucket"] else 0)
                                  for b in qa_outcomes.BUCKETS}
    # 白名单内的库可以显式查；白名单外的库 404
    assert ck.get(f"/api/stats/outcomes?kb_id={kb_a}").json()["total"] == 1
    assert ck.get(f"/api/stats/outcomes?kb_id={kb_b}").status_code == 404
    # 下钻：白名单内的行能看，白名单外的 404（不是 403——不泄漏存在性）
    assert ck.get(f"/api/stats/outcomes/{row_a['id']}").status_code == 200
    assert ck.get(f"/api/stats/outcomes/{row_b['id']}").status_code == 404
    # CSV 同口径（导出不该比面板多几行）
    csv_text = ck.get("/api/stats/outcomes/export.csv").content.decode("utf-8-sig")
    assert kb_a in csv_text and kb_b not in csv_text
    # 对照：不限 scope 的 key 看得到全部（排除"库里压根没有行"的假绿）
    full = _scoped_client(c, None, role="admin", name="open-list")
    assert full.get("/api/stats/outcomes").json()["total"] == 2


def test_superadmin_activity_is_invisible_to_ordinary_admin(client_on):
    """审计分层：普通 admin 的归因清单里不出现超管的活动行（超管自己看全量）。

    归因行带 actor，不过滤会从侧面泄漏超管的活动痕迹——与 routes/admin.py 的
    _hidden_actors 同一条口径（判"是不是超管"+超管用户名集合）。
    """
    c = client_on
    kb = _on_kb(c, "共同库")
    _drain(c, kb, "超管的问题")            # 当前登录身份就是 superadmin
    super_rows = {r["question"]: r["id"] for r in _rows(c.app)}

    # 普通 admin（非超管）问同一个库
    r = c.post("/api/users", json={"username": "ops-admin", "role": "admin",
                                   "password": "ops-pw"})
    assert r.status_code == 200, r.text
    ca = TestClient(c.app)
    assert ca.post("/api/auth/login",
                   json={"username": "ops-admin", "password": "ops-pw"}
                   ).status_code == 200
    _drain(ca, kb, "普通管理员的问题")

    seen = ca.get("/api/stats/outcomes").json()
    questions = [i["question"] for i in seen["items"]]
    assert questions == ["普通管理员的问题"]
    assert seen["total"] == 1
    assert seen["buckets"] == {b: (1 if b == _rows(c.app)[0]["bucket"] else 0)
                               for b in qa_outcomes.BUCKETS}
    # 超管行不是"看不见但能按 id 捞出来"：下钻同样 404
    assert ca.get(f"/api/stats/outcomes/{super_rows['超管的问题']}").status_code == 404
    assert ca.get("/api/stats/outcomes/export.csv").text.count("超管的问题") == 0

    # 超管本人看全量（分层只在读取侧，落库照实）
    assert c.get("/api/stats/outcomes").json()["total"] == 2
    assert c.get(f"/api/stats/outcomes/{super_rows['超管的问题']}").status_code == 200


def test_outcome_records_only_go_forward(tmp_path, fake_embedder):
    """历史数据不可回填：接口只反映"开始记录之后"的问答。

    发布之前的问答在 qa_outcomes 里没有行（当时根本没记命中数/最高分，重跑
    历史问题拿到的是今天的检索结果，不是当时的事故现场），所以对一段没有记录
    的历史时间窗，接口如实返回 0 行——这正是预期的、也是唯一诚实的答案。
    """
    c = _client(tmp_path, fake_embedder, min_score=99.0)
    kb = _doc_kb(c)
    # 库早已存在（有文档），但"历史时间窗"里没有任何归因行
    old = c.get("/api/stats/outcomes?to=2000-01-01").json()
    assert old["total"] == 0 and old["items"] == []
    assert all(v == 0 for v in old["buckets"].values())

    _drain(c, kb, "今天才问的问题")
    assert c.get("/api/stats/outcomes").json()["total"] == 1
