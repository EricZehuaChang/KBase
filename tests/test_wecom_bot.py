"""企业微信智能机器人·长连接（T20）：订阅帧、心跳、重连退避、单连接互斥、
去重、提问与身份提取、回复/流式帧形状、aeskey 媒体解密，以及**最要紧的一条**
——同一句提问由两个不同企微 userid 发出，结果随各自的 channel_identities
绑定而不同（与 tests/test_feishu_bot.py 的飞书用例同一个语义）。

**全程不连任何真实企微租户**：WebSocket 是注入的假对象（FakeSocket），
帧的构造与解析是纯函数，问答走测试进程内的假 LLM + 假向量器。
"""
import asyncio
import base64
import json
import os
import random

import pytest
from fastapi.testclient import TestClient

import kbase.channels.wecom as wecom
from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


# ---- 假 WebSocket（无网络） ----

class ConnectionClosed(Exception):
    """假 socket 的"连接被关闭"。刻意不用 websockets 的异常类型：
    本文件要能证明**收包循环对任何连接级异常都走重连**，而不是只认某一种。"""


class FakeSocket:
    """注入用假 socket。

    - `queued`：服务端会发来的帧，收完就挂着等（不立刻断，"刚连上就被踢"
      会把重连用例真正要观察的退避序列冲掉）；
    - `drop_when(sock)`：每次收帧前判断，为真就抛 ConnectionClosed 模拟断开
      （对面关连接/被新连接踢掉/网络中断）。用**条件**而不是"让出几次事件循环"
      来驱动断开：后者依赖事件循环调度次数，脆得没法读。
    """

    def __init__(self, queued=None, *, drop_when=None):
        self.queued = list(queued or [])
        self.sent: list[str] = []
        self.closed = False
        self._drop_when = drop_when

    async def send(self, text):
        # 只按 closed 判断，**不**看 drop_when：drop_when 表达的是"这条连接不再
        # 收帧了"（recv 侧），不是"发不出去"。混用会让"收到消息 → 回执"这条
        # 路径在回执那一刻莫名炸掉（发送发生在 recv 之后，条件早就成立了）。
        if self.closed:
            raise ConnectionClosed("socket 已关闭")
        self.sent.append(text)

    async def recv(self):
        if self.closed:
            raise ConnectionClosed("socket 已关闭")
        while not self.queued:
            if self._drop_when and self._drop_when(self):
                raise ConnectionClosed("模拟连接断开")
            await asyncio.sleep(0)
        return self.queued.pop(0)

    async def close(self):
        self.closed = True

    def frames(self) -> list[dict]:
        return [json.loads(t) for t in self.sent]

    def cmds(self) -> list[str]:
        return [f.get("cmd") for f in self.frames()]

    def replies(self) -> list[dict]:
        """我们发出的**回复帧**（订阅/心跳帧不参与回复形状断言）。"""
        return [f for f in self.frames() if f["cmd"] == wecom.CMD_RESPOND]


class SockFactory:
    """按序发 socket 的工厂；用尽后重复最后一个（避免测试里 IndexError
    掩盖真正要断言的失败原因）。"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls: list[tuple] = []

    async def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.seq[min(len(self.calls), len(self.seq)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def ack(req_id: str = "srv-1") -> str:
    """订阅成功应答。"""
    return wecom.build_subscribe_ack(req_id)


async def _tick(n: int = 60):
    """让出 n 次事件循环，给后台任务推进的机会。"""
    for _ in range(n):
        await asyncio.sleep(0)


async def run_until_dropped(coro):
    """跑一个"最终会被断开"的循环，断言它确实断在 ConnectionClosed 上。"""
    with pytest.raises(ConnectionClosed):
        await coro


# ---- 回调帧构造 ----

def cb(text: str, *, req_id: str = "REQ-1", msg_id: str = "MSG-1",
       userid: str | None = "zhangsan", msgtype: str = "text") -> str:
    """一条用户消息回调帧（明文，无需加解密——长连接模式的前提）。"""
    body: dict = {"msgtype": msgtype}
    if msgtype == "text":
        body["text"] = {"content": text}
    elif msgtype == "mixed":
        body["mixed"] = {"items": [{"type": "text", "text": text},
                                   {"type": "mention", "userid": "bot"}]}
    elif msgtype == "richtext":
        body["richtext"] = {"content": f"<p>{text}</p>"}
    else:
        body[msgtype] = {"url": "https://example.invalid/media", "aeskey": "K"}
    if userid:
        body["from"] = {"userid": userid}
    body["msgid"] = msg_id
    return json.dumps({"cmd": "aibot_msg_callback",
                       "headers": {"req_id": req_id}, "body": body},
                      ensure_ascii=False)


@pytest.fixture(autouse=True)
def _clean_process_state():
    """单连接槽与去重 LRU 都是**进程级**状态（生产里一个进程就该只跑一个 Bot，
    所以它们故意不是实例属性）。测试里同一进程跑几十个用例，必须显式清干净：
    留着上一个用例的锁会让下一个用例报"已有一条长连接"，而那个失败完全看不出
    跟用例本身有什么关系。"""
    wecom.release_single_connection()
    wecom._seen_messages.clear()
    yield
    wecom.release_single_connection()
    wecom._seen_messages.clear()


CFG_BOT = wecom.WeComConfig(enabled=True, bot_id="BOTID",
                            secret_env="KBASE_WECOM_BOT_SECRET",
                            heartbeat_seconds=30.0, streaming=False)


class Clock:
    """假时钟：只保证单调，不参与断言（存活时长只用来记账）。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        self.t += 0.5
        return self.t


def make_bot(sock, *, answer=None, sleeps=None, cfg=None, **kw):
    """组装一个用假 socket 的 Bot。

    sleep 被换成记账 + 让出控制权的假 sleep：**不真睡**（真睡 30s 心跳会让测试
    挂死），同时后台任务仍能推进。
    """
    recorded: list[tuple] = []

    async def _answer(kb_id, question, external_user_id):
        recorded.append((kb_id, question, external_user_id))
        return f"答案：{question}", [{"doc_name": "报销.md"}]

    sleeps = sleeps if sleeps is not None else []

    async def _sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    bot = wecom.WeComBot(
        cfg or CFG_BOT, secret="SECRET-1", sock_connect=SockFactory([sock]),
        answer_fn=answer or _answer, kb_id="kb-1",
        sleep=_sleep, clock=Clock(),
        # 抖动固定 1.0：退避序列可逐项断言（抖动本身另有专门用例）
        jitter_uniform=lambda a, b: 1.0, **kw)
    return bot, recorded, sleeps


# ---- 报文构造（对照官方文档样例逐字断言） ----

def test_subscribe_frame_matches_official_shape():
    frame = json.loads(wecom.build_subscribe_frame("BOTID", "SECRET", "REQUEST_ID"))
    assert frame == {"cmd": "aibot_subscribe",
                     "headers": {"req_id": "REQUEST_ID"},
                     "body": {"bot_id": "BOTID", "secret": "SECRET"}}
    # 帧里没有 Token/EncodingAESKey/encrypt 之类字段：长连接鉴权只有这两项，
    # 照抄飞书那套加解密在这里没有依据（证据文档 §①）
    assert set(frame["body"]) == {"bot_id", "secret"}


def test_ping_frame_is_application_level_ping():
    """心跳不是 websockets 的协议级 ping/pong 控制帧，而是企微的应用层命令。
    两者混为一谈会导致"连接看着健康、服务端却判定不活跃"。"""
    frame = json.loads(wecom.build_ping_frame("BOTID", "R-1"))
    assert frame["cmd"] == "aibot_ping"
    assert frame["headers"] == {"req_id": "R-1"}
    assert frame["body"] == {"bot_id": "BOTID"}


def test_reply_frame_shape_finish_and_streaming():
    done = json.loads(wecom.build_reply_frame("REQ-1", "最终答案", finish=True))
    assert done == {"cmd": wecom.CMD_RESPOND,
                    "headers": {"req_id": "REQ-1"},
                    "body": {"msgtype": "stream",
                             "stream": {"status": "finish",
                                        "content": "最终答案"}}}
    mid = json.loads(wecom.build_stream_frame("REQ-1", "半截", finish=False))
    assert mid["body"]["stream"] == {"status": "streaming", "content": "半截"}
    assert mid["headers"]["req_id"] == "REQ-1"


def test_subscribe_ack_handling_ok_and_rejected():
    ok = json.loads(wecom.build_subscribe_ack("r"))
    assert wecom.is_subscribe_ack(ok) and wecom.subscribe_ok(ok)
    # 裸回执（只有 errcode）：也要认出来，否则订阅成功那一刻会被当成用户消息
    bare = {"headers": {"req_id": "r"}, "body": {"errcode": 0}}
    assert wecom.is_subscribe_ack(bare) and wecom.subscribe_ok(bare)
    bad = {"cmd": "aibot_subscribe", "body": {"errcode": 40001,
                                              "errmsg": "invalid secret"}}
    assert not wecom.subscribe_ok(bad)
    assert "invalid secret" in wecom.subscribe_error(bad)
    assert not wecom.is_subscribe_ack(json.loads(cb("你好")))


def test_stream_deltas_are_cumulative_and_end_with_full_text():
    """流式更新在企微侧是**替换**语义，必须推累积全文；推增量会让卡片只剩尾巴。"""
    answer = "A" * 50
    pieces = wecom.stream_deltas(answer, min_delta=24)
    assert pieces[-1] == answer                       # 末项恒为全文
    assert pieces[0] == "A" * 24
    assert all(len(p) >= 24 for p in pieces[:-1])
    assert pieces == sorted(pieces, key=len)          # 单调增长（累积而非增量）
    assert wecom.stream_deltas("") == []


# ---- 提问与身份提取 ----

def test_extract_question_text_mixed_richtext():
    assert wecom.extract_question(json.loads(cb("住宿上限是多少"))) == "住宿上限是多少"
    # mixed：只取 text 项，mention 项不混进问题（否则昵称污染检索）
    assert wecom.extract_question(
        json.loads(cb("住宿上限是多少", msgtype="mixed"))) == "住宿上限是多少"
    # richtext：HTML 取纯文本
    assert wecom.extract_question(
        json.loads(cb("住宿上限是多少", msgtype="richtext"))) == "住宿上限是多少"
    # 富文本带标签、换行与转义实体
    rich = {"cmd": "aibot_msg_callback", "headers": {"req_id": "r"},
            "body": {"msgtype": "richtext", "msgid": "m",
                     "richtext": {"content": "<p>第一行</p><p>第二行 &amp; 补充</p>"}}}
    assert wecom.extract_question(rich) == "第一行\n第二行 & 补充"
    # 心跳/订阅回执/空文本都不是问题
    assert wecom.extract_question(json.loads(wecom.build_ping_frame("B", "r"))) is None
    assert wecom.extract_question(json.loads(ack())) is None
    assert wecom.extract_question(json.loads(cb("   "))) is None
    # 图片消息没有可当问题的文本（走下载那条路）
    assert wecom.extract_question(json.loads(cb("", msgtype="image"))) is None


def test_extract_sender_id_and_download_target():
    assert wecom.extract_sender_id(json.loads(cb("x", userid="zhangsan"))) == "zhangsan"
    # userid 缺失（企微未带 sender）→ None，由渠道层走未映射默认策略
    assert wecom.extract_sender_id(json.loads(cb("x", userid=None))) is None
    # 加密 userid 原样当外部 id 用：不解密、不猜格式
    enc = "ENC#abc123=="
    assert wecom.extract_sender_id(json.loads(cb("x", userid=enc))) == enc
    # 多媒体只在这三种类型上出现
    assert wecom.download_target(json.loads(cb("", msgtype="image"))) == \
        ("https://example.invalid/media", "K")
    assert wecom.download_target(json.loads(cb("", msgtype="file"))) is not None
    assert wecom.download_target(json.loads(cb("", msgtype="video"))) is not None
    assert wecom.download_target(json.loads(cb("问题"))) is None


# ---- 去重 ----

def test_duplicate_delivery_is_answered_once():
    """企微在网络抖动/未及时应答时会重投同一条消息：不去重就对同一个人答两遍，
    并落两行归因（把归因数据也搞脏）。"""
    sock = FakeSocket([ack(), cb("住宿上限是多少", msg_id="M-1"),
                       cb("住宿上限是多少", msg_id="M-1")],   # 同一 msgid 重投
                      drop_when=lambda s: len(s.queued) == 0)
    bot, recorded, _ = make_bot(sock)

    async def _main():
        await bot.connect_once()
        await run_until_dropped(bot.recv_loop())

    asyncio.run(_main())
    assert len(recorded) == 1
    assert sock.cmds() == ["aibot_subscribe", wecom.CMD_RESPOND]


def test_media_message_gets_a_receipt_and_bypasses_dedup():
    """暂不支持的媒体类型：回一句"暂不支持"，且**根本不进去重**——把"我们没处理
    的消息"记进 LRU，会让它和"重投的消息"混成一类，语义是错的。

    用 spy 断言"没调用过"，而不是断言进程级 LRU 为空：那是跨用例共享状态，
    拿它做断言会把用例顺序变成隐性依赖（这正是本文件 autouse fixture 存在的
    理由）——也顺带说明为什么这条断言不能等消息处理完再发：回复帧与 recv 循环
    推进是并发的。"""
    seen_keys: list[str] = []
    sock = FakeSocket([ack(), cb("", msgtype="image", msg_id="M-2")],
                      drop_when=lambda s: len(s.replies()) >= 1)
    bot, recorded, _ = make_bot(
        sock, dedup=lambda key: (seen_keys.append(key), False)[1])

    async def _main():
        await bot.connect_once()
        await run_until_dropped(bot.recv_loop())

    asyncio.run(_main())
    assert recorded == []                 # 媒体未进问答链路
    assert seen_keys == []                # 也没被记成"已处理"
    assert len(sock.replies()) == 1
    assert "暂不支持" in sock.replies()[0]["body"]["stream"]["content"]


def test_seen_cache_is_bounded():
    for i in range(wecom._SEEN_MAX + 50):
        assert wecom.is_duplicate_message(f"m-{i}") is False
    assert len(wecom._seen_messages) == wecom._SEEN_MAX
    # 重复的仍然认得出
    assert wecom.is_duplicate_message(f"m-{wecom._SEEN_MAX + 49}") is True
    # 空 id 不去重（拿不到 msgid 时宁可可能答两遍，也不能把不同消息当同一条）
    assert wecom.is_duplicate_message("") is False


# ---- 心跳 ----

def test_heartbeat_sends_ping_at_configured_interval():
    """心跳按配置间隔发**应用层** ping；且心跳里**不带订阅**
    （官方明确订阅有频率保护、成功后不可反复请求）。"""
    sock = FakeSocket([ack()], drop_when=lambda s: len(s.queued) == 0)
    bot, _, sleeps = make_bot(sock)

    async def _main() -> list[float]:
        await bot.connect_once()
        hb = asyncio.create_task(bot.heartbeat_loop())
        for _ in range(500):
            await asyncio.sleep(0)
            if len([f for f in sock.frames()
                    if f["cmd"] == wecom.CMD_PING]) >= 3:
                break
        intervals = list(sleeps)
        hb.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hb
        return intervals

    intervals = asyncio.run(_main())
    # 序列按"恰好 3 个 ping"截断时，记账可能已经记到第 4 次等待（记账发生在
    # 发送之前），所以只看前 3 项：间隔**恒为配置值**，不是别的数、也不是递减/递增
    assert intervals[:3] == [30.0, 30.0, 30.0]
    assert set(intervals) == {30.0}
    pings = [f for f in sock.frames() if f["cmd"] == wecom.CMD_PING]
    assert len(pings) == 3
    assert all(f["body"] == {"bot_id": "BOTID"} for f in pings)
    assert sock.cmds().count("aibot_subscribe") == 1


def test_heartbeat_uses_native_ping_disabled():
    """建连时显式关掉 websockets 的协议级 ping：企微要的是应用层 ping，
    协议级 ping 是另一套东西（开着只会让服务端看到无用流量）。"""
    sock = FakeSocket([ack()])
    bot, _, _ = make_bot(sock)

    async def _main():
        await bot.connect_once()

    asyncio.run(_main())
    url, kwargs = bot._sock_connect.calls[0]
    assert url == "wss://openws.work.weixin.qq.com"
    assert kwargs["ping_interval"] is None
    bot._forget_socket()


# ---- 单连接互斥 ----

def test_single_connection_mutex_rejects_second_connection():
    """企微规定每个机器人同一时间只有 1 条有效长连接，新连接踢旧连接。
    "我们踢我们"表现为消息随机丢一半且两边日志都健康——所以同进程第二条连接
    直接报错（编程错误，宁可当场炸）。"""
    sock_a = FakeSocket([ack()])
    sock_b = FakeSocket([ack()])
    bot_a, _, _ = make_bot(sock_a)
    bot_b, _, _ = make_bot(sock_b)
    asyncio.run(bot_a.connect_once())
    assert wecom.single_connection_held() is True
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(bot_b.connect_once())
    assert "只允许 1 条有效长连接" in str(ei.value)
    # 被拒的那次没有留下半开连接
    assert bot_b.connected is False and sock_b.sent == []
    # 第一条仍持有（互斥不是"谁都能关掉别人"）
    assert bot_a.connected is True
    bot_a._forget_socket()
    assert wecom.single_connection_held() is False
    # 释放后才能再建（进程重启/前一条已断开的情形）
    asyncio.run(bot_b.connect_once())
    assert bot_b.connected is True
    bot_b._forget_socket()


def test_connect_failure_releases_slot():
    """建连失败（网络不通/订阅被拒）也必须放掉槽位：不放的话后续重连永远撞自己的
    锁，表现为"第一次连不上就再也连不上"。"""
    bot, _, _ = make_bot(FakeSocket([]))

    async def _boom(url, **kwargs):
        raise ConnectionClosed("网络不通")

    bot._sock_connect = _boom
    with pytest.raises(ConnectionClosed):
        asyncio.run(bot.connect_once())
    assert wecom.single_connection_held() is False and bot.connected is False

    # 订阅被拒同样放槽
    bad = FakeSocket([json.dumps({"cmd": "aibot_subscribe",
                                  "body": {"errcode": 40001,
                                           "errmsg": "invalid secret"}})])
    bot_b, _, _ = make_bot(bad)
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(bot_b.connect_once())
    assert "invalid secret" in str(ei.value)
    assert wecom.single_connection_held() is False


def test_reconnect_can_reacquire_after_release():
    """_forget_socket 必须能**重复**释放且不影响再次占用：重连路径会先丢弃旧
    socket 再建新的，若第一次释放后就把自己锁在"已释放"状态，第二次连接会
    直接撞锁（表现为一次重连之后进程再也连不上）。"""
    # 两次连接各给一个 socket：第二条 socket 的应答帧是"第二次连接真的重新
    # 订阅了"的证据（复用同一个已排空的 socket 会挂在等应答上，那是测试写错，
    # 不是被测代码的问题）。
    first, second = FakeSocket([ack()]), FakeSocket([ack()])
    bot, _, _ = make_bot(first)
    bot._sock_connect = SockFactory([first, second])

    async def _main():
        await bot.connect_once()
        bot._forget_socket()
        bot._forget_socket()          # 幂等：重连路径与 finally 都会调
        await bot.connect_once()
        assert bot.connected is True

    asyncio.run(_main())
    assert second.cmds() == ["aibot_subscribe"]   # 第二次连接真的重新订阅了
    assert wecom.single_connection_held() is True
    bot._forget_socket()


# ---- 重连与退避 ----

@pytest.mark.asyncio
async def test_reconnect_backoff_on_dropped_connection():
    """连接断掉 → 按指数退避重连（上限 reconnect_max_seconds），且**每次重连都
    重新订阅**（订阅状态随旧连接消失）。"""
    first = FakeSocket([ack()], drop_when=lambda s: True)      # 连上立刻断
    second = FakeSocket([ack()], drop_when=lambda s: True)     # 再连上又断
    third = FakeSocket([ack()], drop_when=lambda s: False)  # 第三次连上后保持
    bot, _, sleeps = make_bot(first, cfg=wecom.WeComConfig(
        enabled=True, bot_id="BOTID", heartbeat_seconds=30.0,
        reconnect_min_seconds=1.0, reconnect_max_seconds=3.0))
    factory = SockFactory([first, second, third])
    bot._sock_connect = factory

    task = asyncio.create_task(bot.run_forever())
    for _ in range(400):
        await _tick(1)
        # 判据取"第三条连接已经发了心跳"，而不是 bot.connected：后者在
        # connect_once 刚走到一半时就为真，会让取消落在连接建立过程中
        # （那时 _reconnects 还没归零，断言会误判成"退避没重置"）。
        if len(factory.calls) >= 3 and [f["cmd"] for f in third.frames()
                                        if f["cmd"] == wecom.CMD_PING]:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(factory.calls) == 3
    # 注：这里**不断言 _reconnects 的累计值**。它记的是"连续失败次数"，而
    # connect_once 一成功就归零（订阅成功=连接可用）——本用例的两条连接都是
    # 订阅成功后立刻被踢，每次失败时 _reconnects 恰好都是 1，所以退避序列是
    # 2.0s、2.0s 而不是递增。这不是 bug（真实场景里"连不稳"就该一直按最短档
    # 重试），指数增长与封顶由下一个用例单独钉住。
    backoff = [s for s in sleeps if s < 30.0]
    assert backoff == [2.0, 2.0]
    assert all(s <= 3.0 for s in backoff)
    # 三次连接各自发了订阅（不是只在第一次发）
    # 三次连接各自发了订阅（不是只在第一次发）；第三条还活着，所以它上面
    # 会有心跳，只断言"订阅恰好一次 + 是第一帧"
    for sock in (first, second, third):
        assert sock.cmds().count("aibot_subscribe") == 1
        assert sock.cmds()[0] == "aibot_subscribe"
    bot._forget_socket()
    assert bot._reconnects == 0                     # 连接成功后计数归零（退避重置）


def test_backoff_is_capped_and_jittered():
    """退避封顶 + 抖动：抖动不是装饰——主备同时掉线时固定间隔会让它们同时重连
    然后互相踢，退避错不开就白退。"""
    bot, _, _ = make_bot(FakeSocket([]), cfg=wecom.WeComConfig(
        enabled=True, bot_id="B", reconnect_min_seconds=1.0,
        reconnect_max_seconds=8.0))
    bot._jitter = lambda a, b: 1.0
    seq = []
    for _ in range(8):
        seq.append(bot.next_backoff())
        bot._reconnects += 1
    assert seq == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0, 8.0]

    # 抖动**不得**把间隔压到低于下界：_reconnects=0 时 base 恰好是下界，
    # 0.5 倍抖动若不夹住会变成 0.5s 疯狂重连，等于没有退避。
    bot._reconnects = 0
    bot._jitter = lambda a, b: 0.5
    assert bot.next_backoff() == 1.0
    # 退避真的错开：同样 _reconnects 下每次取值不同（主备不会撞在一起）
    bot._reconnects = 3                      # base = 8.0（未到上限）
    bot._jitter = random.uniform
    got = [bot.next_backoff() for _ in range(50)]
    assert all(4.0 <= v <= 8.0 for v in got)
    assert len(set(got)) > 1, "抖动没生效（主备会同时重连互踢）"


@pytest.mark.asyncio
async def test_backoff_grows_with_consecutive_failures_and_caps():
    """连续失败（每次都连不上）时退避指数增长，并在上限截住。

    刻意让它**一直**连不上：只有"连续失败"时 _reconnects 才会累加（订阅成功即
    归零，见上一个用例的注释），所以这条才是"指数退避 + 封顶"的真实路径。
    """
    dead = FakeSocket([])

    async def _refuse(url, **kwargs):
        raise ConnectionClosed("网络不通")

    bot, _, sleeps = make_bot(dead, cfg=wecom.WeComConfig(
        enabled=True, bot_id="B", reconnect_min_seconds=1.0,
        reconnect_max_seconds=8.0))
    bot._sock_connect = _refuse
    task = asyncio.create_task(bot.run_forever())
    for _ in range(300):
        await _tick(1)
        if len(sleeps) >= 5:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sleeps[:5] == [2.0, 4.0, 8.0, 8.0, 8.0]
    assert all(s <= 8.0 for s in sleeps)


@pytest.mark.asyncio
async def test_run_forever_retries_when_subscribe_rejected():
    """订阅被拒（常见原因：同一 BotID 在别处已经连着）按退避重试，等对面让位，
    而不是退出进程。"""
    rejected = FakeSocket([json.dumps({"cmd": "aibot_subscribe",
                                       "body": {"errcode": 40001,
                                                "errmsg": "already connected"}})])
    good = FakeSocket([ack()], drop_when=lambda s: False)
    bot, _, sleeps = make_bot(rejected, cfg=wecom.WeComConfig(
        enabled=True, bot_id="B", reconnect_min_seconds=1.0,
        reconnect_max_seconds=10.0))
    factory = SockFactory([rejected, good])
    bot._sock_connect = factory

    task = asyncio.create_task(bot.run_forever())
    for _ in range(200):
        await _tick(1)
        # 等第二条连接**稳定订阅完成**（发过心跳），而不是等 connected 为真
        if len(factory.calls) >= 2 and [f["cmd"] for f in good.frames()
                                        if f["cmd"] == wecom.CMD_PING]:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(factory.calls) == 2
    # 第 1 次失败时 _reconnects 已是 1 → 1s*2^1 = 2s（与断连那条同一套退避）
    assert 2.0 in sleeps
    bot._forget_socket()
    assert bot._reconnects == 0                     # 第二次订阅成功后归零


@pytest.mark.asyncio
async def test_run_forever_is_cancellable_during_subscribe():
    """取消必须能真的停下来（回归用例）。

    实测踩到的坑：取消恰好落在"等订阅回执"的那次 `asyncio.wait_for` 上时，
    CPython 3.11 会**吞掉这次取消**（任务停在 cancelling 状态却不再抛
    CancelledError），随后 `run_forever` 的 `except Exception` 又把被
    `raise ... from e` 变成 RuntimeError 的取消当成"连接失败"继续退避重连——
    结果是 **Ctrl-C / systemd stop 停不下来，进程一直在重连**。
    这里钉的就是"停得下来"：订阅直接被拒（最容易走进那条路径的状态）时，
    取消必须让 run_forever 抛 CancelledError 退出，而不是继续重试。
    """
    rejected = FakeSocket([json.dumps({"cmd": "aibot_subscribe",
                                       "body": {"errcode": 40001,
                                                "errmsg": "already connected"}})])
    bot, _, _ = make_bot(rejected, cfg=wecom.WeComConfig(
        enabled=True, bot_id="B", reconnect_min_seconds=1.0,
        reconnect_max_seconds=10.0))
    task = asyncio.create_task(bot.run_forever())
    await _tick(1)                     # 让它进到 connect_once 中途
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)     # 5s 内必须停：停不下来就是回归
    assert wecom.single_connection_held() is False


@pytest.mark.asyncio
async def test_cancelled_task_actually_stops_reconnecting():
    """更直接地钉住"退避期间被取消也要停"：连续失败进入退避睡眠后取消。"""
    bad = FakeSocket([json.dumps({"cmd": "aibot_subscribe",
                                 "body": {"errcode": 40001, "errmsg": "nope"}}),
                      json.dumps({"cmd": "aibot_subscribe",
                                  "body": {"errcode": 40001, "errmsg": "nope"}})])
    bot, _, sleeps = make_bot(bad, cfg=wecom.WeComConfig(
        enabled=True, bot_id="B", reconnect_min_seconds=1.0,
        reconnect_max_seconds=10.0))
    task = asyncio.create_task(bot.run_forever())
    for _ in range(200):
        await _tick(1)
        if sleeps:                     # 已经退避过一次 → 处于"重试中"
            break
    assert sleeps, "没进入重连退避，用例前提不成立"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


# ---- 回复：流式开关 off / on ----

def test_reply_single_finish_frame_when_streaming_off():
    """默认（streaming=False）：**只推一帧** finish=true 的完整答案。
    与飞书卡片同一取舍——IM 场景没有逐字刷新的位置，也避开企微的频率保护。"""
    sock = FakeSocket([ack()])
    bot, _, _ = make_bot(sock)

    async def _main():
        await bot.connect_once()
        await bot.reply("REQ-9", "答案正文")

    asyncio.run(_main())
    frames = sock.replies()
    assert len(frames) == 1
    assert frames[0]["body"]["stream"] == {"status": "finish",
                                           "content": "答案正文"}
    assert frames[0]["headers"] == {"req_id": "REQ-9"}
    bot._forget_socket()


def test_reply_pushes_streaming_frames_when_flag_on():
    """streaming=True：主动推中间帧直到 finish=true（长连接模式没有流式刷新回调，
    官方要求开发者侧主动推送）；中间帧是累积全文，终帧是完整答案。"""
    sock = FakeSocket([ack()])
    cfg = wecom.WeComConfig(enabled=True, bot_id="B", streaming=True)
    bot, _, _ = make_bot(sock, cfg=cfg)
    answer = "第一句。" * 40        # 长度足够切出多个中间帧（min_delta=24 字符）

    async def _main():
        await bot.connect_once()
        await bot.reply("REQ-10", answer)

    asyncio.run(_main())
    frames = sock.replies()
    assert len(frames) > 2                       # 至少一个中间帧 + 终帧
    assert [f["body"]["stream"]["status"] for f in frames[:-1]] == \
        ["streaming"] * (len(frames) - 1)
    assert frames[-1]["body"]["stream"]["status"] == "finish"
    assert frames[-1]["body"]["stream"]["content"] == answer
    contents = [f["body"]["stream"]["content"] for f in frames]
    # 累积（不是增量片段）：每一项都是答案的前缀，且长度递增
    assert all(answer.startswith(c) for c in contents)
    assert [len(c) for c in contents] == sorted(len(c) for c in contents)
    assert {f["headers"]["req_id"] for f in frames} == {"REQ-10"}
    bot._forget_socket()


# ---- 端到端（假 socket） ----

def test_full_message_flow_reply_and_audit():
    """订阅 → 收提问 → 问答 → 回复帧 → 审计，并在连接断开时把槽位放掉。"""
    audits: list[tuple] = []
    sock = FakeSocket([ack(), cb("住宿上限是多少", req_id="REQ-7", msg_id="M-7",
                                 userid="lisi")],
                      drop_when=lambda s: len(s.queued) == 0)
    bot, recorded, _ = make_bot(sock,
                                audit_fn=lambda kb, q: audits.append((kb, q)))

    async def _main():
        await bot.connect_once()
        await run_until_dropped(bot.recv_loop())

    asyncio.run(_main())
    assert recorded == [("kb-1", "住宿上限是多少", "lisi")]
    replies = sock.replies()
    assert len(replies) == 1
    assert replies[0]["headers"] == {"req_id": "REQ-7"}
    assert replies[0]["body"]["stream"]["content"] == "答案：住宿上限是多少"
    assert audits == [("kb-1", "住宿上限是多少")]
    bot._forget_socket()
    assert wecom.single_connection_held() is False


def test_unbound_kb_does_not_answer():
    """没绑库的机器人静默忽略（管理端会提示），而不是回落 llm.active 乱答。"""
    sock = FakeSocket([ack(), cb("住宿上限是多少")],
                      drop_when=lambda s: len(s.queued) == 0)
    recorded: list[tuple] = []

    async def _answer(kb_id, question, external_user_id):
        recorded.append(question)
        return "不应该被调用", []

    bot = wecom.WeComBot(CFG_BOT, secret="S", sock_connect=SockFactory([sock]),
                         answer_fn=_answer, kb_id="", sleep=_noop_sleep,
                         clock=Clock())

    async def _main():
        await bot.connect_once()
        await run_until_dropped(bot.recv_loop())

    asyncio.run(_main())
    assert recorded == []
    assert sock.replies() == []
    bot._forget_socket()


def test_answer_failure_does_not_kill_the_connection():
    """一条消息答不出来不能把长连接带走：断了之后重连期间的消息全丢。"""
    sock = FakeSocket([ack(),
                       cb("会炸的问题", msg_id="M-bad"),
                       cb("正常问题", msg_id="M-ok")],
                      drop_when=lambda s: len(s.queued) == 0)
    seen: list[str] = []

    async def _answer(kb_id, question, external_user_id):
        seen.append(question)
        if "炸" in question:
            raise RuntimeError("检索服务挂了")
        return "正常答案", []

    bot = wecom.WeComBot(CFG_BOT, secret="S", sock_connect=SockFactory([sock]),
                         answer_fn=_answer, kb_id="kb-1", sleep=_noop_sleep,
                         clock=Clock())

    async def _main():
        await bot.connect_once()
        await run_until_dropped(bot.recv_loop())

    asyncio.run(_main())
    assert seen == ["会炸的问题", "正常问题"]      # 第二条照样被处理
    assert [r["headers"]["req_id"] for r in sock.replies()] == ["REQ-1"]
    bot._forget_socket()


async def _noop_sleep(_seconds):
    await asyncio.sleep(0)


# ---- aeskey 媒体解密（协议里唯一需要解密的一处） ----

def _encrypt_media(aeskey: str, payload: bytes) -> bytes:
    """测试向量：与企微同算法（AES-256-CBC，key=aeskey 原文，IV 前置，PKCS7）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = aeskey.encode("utf-8")[:32].ljust(32, b"\0")
    pad = 16 - len(payload) % 16
    padded = payload + bytes([pad]) * pad
    iv = os.urandom(16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + enc.update(padded) + enc.finalize()


def test_decrypt_media_with_raw_aeskey():
    aeskey = "0123456789abcdef0123456789abcdef"      # 32 字符 → 原文作密钥
    blob = _encrypt_media(aeskey, b"\x89PNG\r\n fake image bytes")
    assert wecom.decrypt_media(aeskey, blob) == b"\x89PNG\r\n fake image bytes"


def test_decrypt_media_with_base64_aeskey():
    """官方口径是 base64 编码的 32 字节密钥；两种口径都要跑通——猜错密钥口径的
    表现是乱码或异常，现场极难判断是密钥问题还是响应体问题。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = bytes(range(32))
    payload = b"file-content"
    pad = 16 - len(payload) % 16
    iv = b"\x01" * 16
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    blob = iv + enc.update(payload + bytes([pad]) * pad) + enc.finalize()
    assert wecom.decrypt_media(base64.b64encode(key).decode(), blob) == payload


def test_decrypt_media_rejects_bad_input():
    """解不出来必须报错，**不能**把密文当结果返回：把密文当图片交给下游
    比明确报错危险得多。"""
    with pytest.raises(ValueError):
        wecom.decrypt_media("", b"\x00" * 32)
    aeskey = "k" * 32
    with pytest.raises(ValueError):
        wecom.decrypt_media(aeskey, b"")                  # 空密文
    with pytest.raises(ValueError):
        wecom.decrypt_media(aeskey, b"\x00" * 20)         # 长度不是 16 的倍数
    with pytest.raises(ValueError):
        # 用另一把密钥加密 → 填充校验必须挡住（不是静默返回乱码）
        wecom.decrypt_media(aeskey, _encrypt_media("w" * 32, b"x" * 16))


def test_fetch_and_decrypt_uses_injected_get():
    """下载+解密两步合一，get 注入所以离线可测（现场只有"拿到可用字节"才算成功）。"""
    aeskey = "a" * 32
    blob = _encrypt_media(aeskey, b"binary-media")

    class Resp:
        content = blob

        def raise_for_status(self):
            return None

    calls: list[tuple] = []

    def _get(url, timeout=None):
        calls.append((url, timeout))
        return Resp()

    assert wecom.fetch_and_decrypt("https://example.invalid/x", aeskey,
                                   get=_get) == b"binary-media"
    assert calls[0][0] == "https://example.invalid/x"


# ---- 配置与密钥 ----

def test_secret_comes_from_env_and_missing_env_raises(monkeypatch):
    """密钥只走环境变量（与 ProviderConfig.api_key_env 同一规矩）；变量缺失
    **必须报错**——静默退化成空串的表现是订阅被拒，日志里看不出是"没注入密钥"
    还是"密钥错了"。"""
    monkeypatch.delenv("KBASE_WECOM_BOT_SECRET", raising=False)
    with pytest.raises(RuntimeError) as ei:
        wecom.load_env_secret("KBASE_WECOM_BOT_SECRET")
    assert "KBASE_WECOM_BOT_SECRET" in str(ei.value)
    monkeypatch.setenv("KBASE_WECOM_BOT_SECRET", "s3cret")
    assert wecom.load_env_secret("KBASE_WECOM_BOT_SECRET") == "s3cret"


def test_wecom_config_defaults_are_off():
    """默认关、且密钥字段是**环境变量名**不是密钥值（配置文件可入库）。"""
    from pathlib import Path

    from kbase.config import WeComConfig, load_config
    w = WeComConfig()
    assert w.enabled is False and w.streaming is False
    assert w.heartbeat_seconds == 30.0                  # 官方建议 30 秒
    assert w.secret_env == "KBASE_WECOM_BOT_SECRET"
    assert "secret" not in w.model_dump()               # 没有明文密钥字段

    cfg = load_config(Path("config/kbase.yaml"))
    assert cfg.wecom.enabled is False
    assert cfg.wecom.url == "wss://openws.work.weixin.qq.com"
    assert cfg.wecom.reconnect_min_seconds <= cfg.wecom.reconnect_max_seconds
    # 配置文件里不许出现密钥值
    text = Path("config/kbase.yaml").read_text(encoding="utf-8")
    assert "secret: " not in text and "secret_env" in text

    cfg2 = load_config(Path("config/kbase.standard.yaml"))
    assert cfg2.wecom.enabled is False and cfg2.wecom.bot_id == ""


def test_channel_registered_in_registry():
    """管理端下拉/绑定接口由注册表驱动：注册了 wecom 才能绑定企微身份。"""
    from kbase.channels import core as channels
    assert channels.CHANNELS["wecom"] == "企业微信"
    assert channels.CHANNELS["feishu"] == "飞书"


def test_from_app_config_maps_fields():
    from kbase.config import WeComConfig as CfgModel, load_config
    cfg = load_config("config/kbase.yaml")
    cfg.wecom = CfgModel(enabled=True, bot_id="B1", heartbeat_seconds=15,
                         streaming=True, provider="glm-5-turbo")
    w = wecom.WeComConfig.from_app_config(cfg)
    assert (w.enabled, w.bot_id, w.heartbeat_seconds, w.streaming,
            w.provider) == (True, "B1", 15, True, "glm-5-turbo")


# ---- T19 渠道身份映射：同一句提问，不同企微 userid 拿到不同结果 ----

@pytest.fixture
def client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def _setup_kb(client) -> str:
    kb = client.post("/api/kb", json={"name": "企微库"}).json()["id"]
    r = client.post(f"/api/kb/{kb}/documents",
                    files=[("files", ("报销.md",
                                      "# 报销制度\n住宿上限每晚500元。".encode(),
                                      "text/markdown"))])
    assert r.status_code == 200, r.text
    return kb


def _new_user(client, username: str, role: str = "viewer") -> str:
    r = client.post("/api/users", json={"username": username, "role": role,
                                        "password": "pw-123456"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _kb_id(client) -> str:
    from kbase.models import KnowledgeBase
    with client.app.state.svc.sf() as s:
        return s.query(KnowledgeBase).first().id


def _production_answer_fn(client):
    """**生产接线**（与 wecom.build_bot 内部逐字一致）：外部 userid →
    channels.resolve_actor → answer_for_channel。这里不打任何桩，证明企微渠道
    真的落在 T19 适配层上，共享同一套「检索 → 可用依据 → 引用 → 拒答」语义。"""
    from kbase.channels import core as channels
    svc = client.app.state.svc
    sf = svc.sf

    async def _answer(kb_id, question, external_user_id):
        actor = channels.resolve_actor(sf, channel="wecom",
                                       external_user_id=external_user_id)
        return await channels.answer_for_channel(svc, kb_id=kb_id,
                                                 question=question, actor=actor)
    return _answer


def _run_one_message(client, *, userid: str | None, msg_id: str, question: str,
                     reply_to: list) -> None:
    """一条企微回调帧走完整链路：订阅 → 收消息 → 问答 → 回复。"""
    sock = FakeSocket([ack(), cb(question, msg_id=msg_id, userid=userid)],
                      drop_when=lambda s: len(s.queued) == 0)
    bot = wecom.WeComBot(
        wecom.WeComConfig(enabled=True, bot_id="BOTID"),
        secret="S", sock_connect=SockFactory([sock]),
        answer_fn=_production_answer_fn(client), kb_id=_kb_id(client),
        sleep=_noop_sleep, clock=Clock())

    async def _main():
        await bot.connect_once()
        await run_until_dropped(bot.recv_loop())

    asyncio.run(_main())
    bot._forget_socket()
    for r in sock.replies():
        reply_to.append(r["body"]["stream"]["content"])


def _outcomes(client):
    from kbase.models import QaOutcome
    with client.app.state.svc.sf() as s:
        rows = (s.query(QaOutcome).filter(QaOutcome.channel == "wecom")
                .order_by(QaOutcome.ts.desc(), QaOutcome.id.desc()).all())
        return [{"actor": r.actor, "bucket": r.bucket, "question": r.question,
                 "retrieved_count": r.retrieved_count} for r in rows]


def test_identity_mapping_decides_who_gets_an_answer(client):
    """同一句提问、两个不同企微 userid：映射到被授权用户的查得到，映射到无权
    用户的被拒答——这是 T19 渠道身份映射存在的全部理由，企微侧逐个钉住。

    同时钉住：无权者的回复里**没有引用**（静默拒答，不提示"你无权"），
    归因行 actor 是两个真实用户名、bucket 一个 answered 一个 scope_denied。
    """
    kb = _setup_kb(client)
    authorized = _new_user(client, "wecom-ok")
    stranger = _new_user(client, "wecom-no")
    # 收紧该库：授权集合只有 authorized（公开库变受限库）
    assert client.put(f"/api/kb/{kb}/grants",
                      json={"user_ids": [authorized]}).status_code == 200
    # 绑定两个企微 userid（企微侧可能是明文也可能是加密串，这里就是普通字符串）
    for userid, uid in (("zhangsan", authorized), ("lisi", stranger)):
        rb = client.post("/api/channels/identities",
                         json={"channel": "wecom", "external_user_id": userid,
                               "user_id": uid})
        assert rb.status_code == 200, rb.text
        assert rb.json()["channel"] == "wecom"

    replies: list[str] = []
    _run_one_message(client, userid="zhangsan", msg_id="M-ok",
                     question="住宿上限是多少", reply_to=replies)
    _run_one_message(client, userid="lisi", msg_id="M-no",
                     question="住宿上限是多少", reply_to=replies)
    assert len(replies) == 2
    ok_reply, no_reply = replies[0], replies[1]
    # 有权者：拿到答案（假 LLM 的固定输出）
    assert "满两年" in ok_reply
    # 无权者：与网页端逐字相同的拒答文案（静默拒答）
    assert "未找到依据" in no_reply
    assert ok_reply != no_reply

    rows = _outcomes(client)
    assert len(rows) == 2
    assert rows[0]["actor"] == "wecom-no" and rows[0]["bucket"] == "scope_denied"
    assert rows[0]["retrieved_count"] == 0        # 越权那次压根没触达检索器
    assert rows[1]["actor"] == "wecom-ok" and rows[1]["bucket"] == "answered"
    assert rows[1]["retrieved_count"] > 0
    # 两次问的是同一句话——结果不同只可能来自身份
    assert rows[0]["question"] == rows[1]["question"] == "住宿上限是多少"


def test_unmapped_wecom_user_follows_default_policy(client):
    """未映射（或回调里没有 userid）走默认策略：**不是"全放行"**。
    公开库照常能问，收紧过的库立刻问不到；绑定后立刻能问，解绑后立刻回落。"""
    kb = _setup_kb(client)
    replies: list[str] = []
    # 公开库（无 grant 行）
    _run_one_message(client, userid="wangwu", msg_id="M-1",
                     question="住宿上限是多少", reply_to=replies)
    assert "满两年" in replies[-1]
    assert _outcomes(client)[0]["actor"] == "wecom:wangwu"

    # 收紧库到某个用户 → 未映射的人立刻问不到，且是 scope_denied 安全事件
    owner = _new_user(client, "wecom-owner")
    assert client.put(f"/api/kb/{kb}/grants",
                      json={"user_ids": [owner]}).status_code == 200
    _run_one_message(client, userid="wangwu", msg_id="M-2",
                     question="住宿上限是多少", reply_to=replies)
    assert "未找到依据" in replies[-1]
    assert _outcomes(client)[0]["bucket"] == "scope_denied"

    # 绑定后立刻能问（resolve_actor 不缓存，管理端改完就生效）
    rb = client.post("/api/channels/identities",
                     json={"channel": "wecom", "external_user_id": "wangwu",
                           "user_id": owner}).json()
    _run_one_message(client, userid="wangwu", msg_id="M-3",
                     question="住宿上限是多少", reply_to=replies)
    assert "满两年" in replies[-1]
    assert _outcomes(client)[0]["actor"] == "wecom-owner"
    assert _outcomes(client)[0]["bucket"] == "answered"

    # 解绑后立刻回落默认策略
    assert client.delete(
        f"/api/channels/identities/{rb['id']}").status_code == 200
    _run_one_message(client, userid="wangwu", msg_id="M-4",
                     question="住宿上限是多少", reply_to=replies)
    assert "未找到依据" in replies[-1]
    assert _outcomes(client)[0]["actor"] == "wecom:wangwu"


def test_missing_userid_is_unmapped(client):
    """回调里没有 from.userid（企微少数形态）：按未映射处理，actor 名退化成
    wecom:unknown——宁可名字难看，也不要和"没有身份"混为一谈。"""
    _setup_kb(client)
    replies: list[str] = []
    _run_one_message(client, userid=None, msg_id="M-anon",
                     question="住宿上限是多少", reply_to=replies)
    assert replies and "满两年" in replies[-1]        # 公开库照常作答
    assert _outcomes(client)[0]["actor"] == "wecom:unknown"


def test_channel_binding_api_accepts_wecom(client):
    """绑定接口按注册表校验渠道：注册后 wecom 可绑定（此前会 422）。"""
    _setup_kb(client)
    uid = _new_user(client, "wecom-bind")
    r = client.post("/api/channels/identities",
                    json={"channel": "wecom", "external_user_id": "ENC#abc==",
                          "user_id": uid})
    assert r.status_code == 200, r.text
    items = client.get("/api/channels/identities?channel=wecom").json()["items"]
    assert len(items) == 1 and items[0]["username"] == "wecom-bind"
    assert items[0]["external_user_id"] == "ENC#abc=="   # 加密串原样保存
