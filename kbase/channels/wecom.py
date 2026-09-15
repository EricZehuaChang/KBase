"""企业微信智能机器人（长连接模式，T20）：与飞书机器人**对等的渠道入口**，
只是传输方式完全不同——飞书靠公网回调（KBase 当服务端，谁都能打进来），
企微长连接是**出站长连接**（KBase 当客户端，主动连到
`wss://openws.work.weixin.qq.com`）。因此本模块没有验签/解密/握手应答这一层：
没有公网地址要求、没有回调 URL 可配、也就没有伪造事件能打进来——连接本身
（BotID + Secret 订阅）就是身份校验。

协议要点（一手证据：官方文档 101463，见
projects/project06-kbase/deliverables/2026-09-15-三份一手证据核实结果.md）：

1. **鉴权只有 BotID + Secret 两项**。Secret 是**长连接专用密钥**，官方原文明确
   与"接收消息回调地址"模式的 Token/EncodingAESKey 不是一个东西——所以这里
   **不存在**飞书那种 encrypt/verify token 体系，别照着 feishu_bot 抄加解密。
2. **通道消息无需加解密**。唯一例外：`image`/`file`/`video` 回调体里带下载 URL
   与 `aeskey`，**下载回来的多媒体资源**要用 aeskey 解密（见 decrypt_media）。
3. **每个机器人同一时间只允许 1 条有效长连接**，新连接会踢掉旧连接。高可用只能
   主备切换，不能多连——所以本模块有进程内的单连接互斥（`_CONNECT_LOCK`）把
   "自己踢自己"挡在代码层，运维层则必须保证每个 BotID 只跑一个进程。
4. **心跳靠主动发 ping，官方建议 30 秒**。注意：websockets 库自带的协议级
   ping/pong 是**另一套东西**（RFC 6455 控制帧），企微要的是**应用层** ping
   命令，所以建连时显式 `ping_interval=None`，心跳由 heartbeat_loop 发。
5. **长连接模式没有流式刷新回调**：流式回答只能由开发者**主动推送**更新帧直到
   `finish=true`（见 build_stream_frame）。默认关闭，见 WeComConfig.streaming。
6. `userid` 只在"机器人创建者是超级管理员"时是明文，否则是加密串。本模块**不做
   解密、不猜格式**——原样当外部 id 用（渠道身份映射按字符串精确匹配即可），
   要明文请走官方建议的自建应用对接转明文。
7. 模式互斥：长连接与回调地址二选一，在管理后台切换会让另一模式失效。
8. 微盘/企微文档 API **不在本卡范围内**（免费企业 1000 次/月配额，是另一个
   待决策项），本模块一行都不碰。

可测试性：WebSocket 对象是**注入**的（`sock_connect`），帧的构造与解析全是纯函数，
所以全套逻辑（订阅/心跳/重连退避/去重/提取/回复/流式/media 解密）能在**不连任何
真实企微租户**的前提下测完（见 tests/test_wecom_bot.py）。问答走的是 T19 的
`channels.answer_for_channel`（由本模块的调用方注入 `answer_fn` 接线），
本模块**不自己拼检索/生成/拒答**——渠道语义只有一份。

为什么是独立进程：长连接是"常驻 + 只允许一条"的资源，塞进 web 进程会带来两个
具体问题：多 worker（uvicorn --workers N）或重启滚动期会同时存在多条连接，
**互相踢**（企微按新连接踢旧连接），结果是谁都收不全消息；且 web 进程的
reload/部署节奏会随业务发布打断连接。所以本模块提供
`python -m kbase.channels.wecom` 独立入口，由 systemd/compose 单独拉起并
`Restart=always`。**跑两个进程不会更快，只会互相踢**——需要高可用请做主备
（备的不连、由编排决定谁持有连接），不要在同一个 BotID 上开两条。
"""
import argparse
import asyncio
import base64
import html
import json
import logging
import os
import random
import signal
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# 官方长连接地址（文档 101463）。不做成配置项：官方只有一个地址，多一个可配项
# 只会多一种"配错了连不上"的排查成本；真要私有化代理再说。
WECOM_WS_URL = "wss://openws.work.weixin.qq.com"

# 渠道名（归因行的 channel 取值来源；注册表见 channels/core.py 的 CHANNELS）。
# 与飞书路由里的 CHANNEL 同名同义：一个渠道一个明确取值，不用字面量散落。
CHANNEL = "wecom"

# 订阅命令名与"确认/心跳"命令名（协议固定取值）
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_PING = "aibot_ping"
CMD_PONG = "aibot_pong"

# ---- 回复帧的命令名与取值：**此处是全模块唯一没有官方正文逐字确认的部分** ----
# 官方文档 101463 明确写了订阅帧与心跳帧的报文形状，但回复侧未给出报文样例
# （只说"长连接模式下没有流式刷新回调，需开发者主动推送流式更新直到
# finish=true"）。下面四个取值来自官方 Python SDK（wecom-aibot-python-sdk）的
# 公开用法命名，**未在真实租户上验证过**。真机联调时如果企微回
# errcode 不认，只需改这四个常量（构造与解析全是纯函数，测试用的是同一份
# 常量，所以改一处即全量跟随），不要在业务逻辑里再散落字面量。
CMD_RESPOND = "aibot_respond_msg"
REPLY_MSGTYPE = "stream"
STREAM_STATUS_STREAMING = "streaming"
STREAM_STATUS_FINISH = "finish"

# 建连超时（秒）：websockets.connect 的 open_timeout。连不上（网络不通/DNS
# 解析不了/被中间设备拦）必须尽快报错走重连，而不是挂在那里看不出区别。
CONNECT_TIMEOUT_S = 10.0

# 订阅应答等待上限（秒）：连上但订阅不被受理时必须**超时**报错走重连，不能
# "连上了却永远收不到消息"地挂着——那种状态从监控上看是健康的，最难查。
#
# 注意这里不能只依赖 websockets 建连时的超时参数：建连成功之后的 recv 是**没有
# 超时**的（长连接本来就要一直挂着等消息，参数超时管不着收帧），所以必须由
# 我们自己在 wait_for 里卡住"等订阅应答"这一步。
SUBSCRIBE_TIMEOUT_S = 10.0

# 媒体类消息的即时回执文案：官方支持 image/file/video（后两者仅单聊），但本卡
# 的问答链路只吃文本（媒体要先下载 + aeskey 解密再交给 VLM/OCR，属于后续卡）。
# 不回一句的代价是提问者完全不知道机器人有没有收到——长连接模式下没有 HTTP
# 状态码可看，静默是最差的交互。
UNSUPPORTED_MSGTYPE_REPLY = "暂不支持该消息类型，请用文字描述你的问题。"

# 连接断开后等"在途问答"收尾的上限（秒）：给刚收到的那条消息一个答完的机会，
# 但绝不让它拖住重连（连接已断，那条回复本来也发不出去）。
DRAIN_TIMEOUT_S = 10.0

# 流式推送的节流（秒）：企微侧对同一消息的更新频率有限制，逐字推送等于
# 自己打自己（官方也提示订阅/调用有频率保护）。按累积文本长度推进，
# 不做定时器——定时器会让"答案生成完了还在推送旧片段"。
STREAM_MIN_DELTA_CHARS = 24


# ---- 报文构造（纯函数，可离线断言） ----

def new_req_id() -> str:
    """请求 id：官方样例给的是不透明字符串，用 uuid4 十六进制即可。
    不注入随机源——它只是关联同一条消息的 req）与回复，不需要可预测。"""
    return uuid.uuid4().hex


def build_subscribe_frame(bot_id: str, secret: str, req_id: str) -> str:
    """订阅帧（连上后第一件事就是发它完成身份校验）。

    形状**逐字**对齐官方文档样例：
    ```json
    {"cmd": "aibot_subscribe", "headers": {"req_id": "REQUEST_ID"},
     "body": {"bot_id": "BOTID", "secret": "SECRET"}}
    ```
    `ensure_ascii=False` 与本仓库其它出站 JSON 一致（secret 是 ASCII，但保持
    统一，避免以后带中文的字段被转义成 \\uXXXX 让对面不认）。
    """
    return json.dumps({"cmd": CMD_SUBSCRIBE,
                       "headers": {"req_id": req_id},
                       "body": {"bot_id": bot_id, "secret": secret}},
                      ensure_ascii=False)


def build_ping_frame(bot_id: str, req_id: str) -> str:
    """心跳帧。官方只写"定期发 ping、建议 30 秒"，未给报文样例，此形状按
    订阅帧的 cmd/headers/body 同构写出；body 带 bot_id 让服务端能认出是哪条
    连接的心跳（服务端已经知道，但显式带上不依赖会话状态，排查时抓包可读）。"""
    return json.dumps({"cmd": CMD_PING,
                       "headers": {"req_id": req_id},
                       "body": {"bot_id": bot_id}},
                      ensure_ascii=False)


def build_subscribe_ack(req_id: str = "") -> str:
    """订阅应答（解析用）。官方样例只给到请求侧，应答按同构形状写出：
    成功判定看 body.errcode 缺省/0，失败时回错误信息——见 is_subscribe_ack。"""
    return json.dumps({"cmd": CMD_SUBSCRIBE,
                       "headers": {"req_id": req_id},
                       "body": {"errcode": 0}}, ensure_ascii=False)


def build_reply_frame(req_id: str, text: str, *, finish: bool = True,
                      msg_id: str = "") -> str:
    """回复帧：`cmd + headers.req_id` 回原请求，body 带消息类型与流式状态。

    finish=True 即终态（非流式时这是**唯一**一帧，也是官方要求的
    "推送直到 finish=true"的最后一帧）。msg_id 留空由企微生成。
    """
    body: dict = {"msgtype": REPLY_MSGTYPE,
                  "stream": {"status": (STREAM_STATUS_FINISH if finish
                                        else STREAM_STATUS_STREAMING),
                             "content": text}}
    if msg_id:
        body["msg_id"] = msg_id
    return json.dumps({"cmd": CMD_RESPOND,
                       "headers": {"req_id": req_id}, "body": body},
                      ensure_ascii=False)


def build_stream_frame(req_id: str, text: str, *, finish: bool,
                       msg_id: str = "") -> str:
    """流式更新帧（build_reply_frame 的显式别名，意图在调用点可读：
    区分"流式过程中的中间帧"与"终态帧"）。"""
    return build_reply_frame(req_id, text, finish=finish, msg_id=msg_id)


def stream_deltas(answer: str, *, min_delta: int = STREAM_MIN_DELTA_CHARS) -> list[str]:
    """把答案切成流式推送的文本序列（末项恒为全文）。

    **必须逐项推"累积全文"而不是增量片段**：企微的流式更新语义是**替换**当前
    内容（不是追加），推增量会让卡片上只剩最后几个字——这是长连接模式最容易被
    写成"答案看着在跳但只剩尾巴"的坑。

    按 min_delta 攒够再推：既避免一个字一帧打爆频率保护，也不做时间节流
    （时间节流会在答案已生成完后还继续推旧片段）。
    """
    if not answer:
        return []
    out: list[str] = []
    next_at = min_delta
    for i in range(1, len(answer)):
        if i >= next_at:
            out.append(answer[:i])
            next_at = i + min_delta
    out.append(answer)          # 末项恒为全文（finish 帧另发，内容与之一致）
    return out


# ---- 报文解析（纯函数） ----

class _TextExtractor(HTMLParser):
    """富文本（richtext）内容取纯文本：去标签、保留换行、反转义实体。

    用标准库 HTMLParser 而不是正则去标签——企微 richtext 是 `<p>`/`<br>` 拼的
    HTML，正则遇到属性里的 `>` 或注释就会切错；这里只需要"文本 + 换行"，
    HTMLParser 是零依赖且不会抛错的选择。不引 markdownify 之类新依赖：
    本卡只加 websockets 一个依赖（见 pyproject.toml）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "div", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li", "tr"):
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln).strip()


def html_to_text(value: str) -> str:
    """富文本 HTML → 纯文本（解析失败按原样返回：宁可带标签也别丢问题）。"""
    try:
        parser = _TextExtractor()
        parser.feed(value)
        parser.close()
        return parser.text()
    except Exception:  # noqa: BLE001 —— 解析器不该让整条问答失败
        return html.unescape(value).strip()


def parse_frame(raw: str | bytes) -> dict | None:
    """收到的报文 → dict。非 JSON / 非对象一律 None（由调用方决定记日志还是忽略）。

    **不做加解密**：通道消息明文（证据文档 §①"WebSocket 模式无需加解密"），
    这是与飞书事件回调最大的实现差异。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return frame if isinstance(frame, dict) else None


def _frame_body(frame: dict) -> dict:
    body = frame.get("body")
    return body if isinstance(body, dict) else {}


def frame_cmd(frame: dict) -> str:
    return str(frame.get("cmd") or "")


def is_subscribe_ack(frame: dict) -> bool:
    """是不是订阅应答。

    兼容两种形状：`cmd=aibot_subscribe` 的回执，或只有 headers.req_id 回显、
    正文带 errcode 的裸回执（长连接实现里后者常见）。判定放宽不是偷懒——
    把应答当成普通消息会在订阅成功那一刻就去解析一个不存在的用户消息。
    """
    if frame_cmd(frame) == CMD_SUBSCRIBE:
        return True
    body = _frame_body(frame)
    return "errcode" in body and not body.get("msgtype")


def subscribe_ok(frame: dict) -> bool:
    """订阅是否被受理：errcode 缺省或 0 视为成功，非 0 一律失败。
    失败**必须**走重连（而不是继续 recv）：未订阅的连接收不到任何消息，
    挂着它就是"看起来连着、其实什么都没发生"。"""
    body = _frame_body(frame)
    code = body.get("errcode", 0)
    try:
        return int(code) == 0
    except (TypeError, ValueError):
        return False


def subscribe_error(frame: dict) -> str:
    body = _frame_body(frame)
    return str(body.get("errmsg") or body.get("errcode") or "")


def is_ping(frame: dict) -> bool:
    """服务端主动 ping：必须回 pong，否则被判定连接不活跃。
    这里只做识别，回帧在 WeComBot.heartbeat_loop 的同名分支里。"""
    return frame_cmd(frame) == CMD_PING


# ---- 回调内容提取（纯函数） ----

def extract_message(frame: dict) -> dict | None:
    """回调帧 → `{"req_id", "msg_id", "userid", "msgtype", "body"}`，非用户消息
    返回 None（心跳/订阅回执/我们自己的回声都从这里被过滤掉，而不是散落一堆
    if 在收包循环里）。

    `userid` 取 `from.userid`（企微单聊/群聊回调的发送者字段）：可能是明文也可能是
    加密串，本模块不区分——见模块 docstring 第 6 条。取不到就是 None，由渠道层走
    未映射默认策略（与飞书拿不到 open_id 时同一条路）。
    """
    if frame_cmd(frame) in (CMD_SUBSCRIBE, CMD_PING, CMD_PONG):
        return None
    body = _frame_body(frame)
    msgtype = body.get("msgtype")
    if not msgtype:
        return None
    headers = frame.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    sender = body.get("from")
    sender = sender if isinstance(sender, dict) else {}
    return {
        "req_id": str(headers.get("req_id") or body.get("req_id") or ""),
        "msg_id": str(body.get("msgid") or body.get("msg_id") or ""),
        "userid": sender.get("userid"),
        "msgtype": str(msgtype),
        "body": body,
    }


# 能直接取到问题文本的消息类型（官方：文本/图片/图文混排全支持。"图片"归到
# 多媒体那条路——它的回调体里只有 url+aeskey，没有可当问题的文本；语音/文件/视频
# 官方限单聊，同样走 download_target）。这里是**白名单**不是黑名单：白名单外的
# 类型不猜、不进问答链路（宁可明确回一句"暂不支持"，也不要拿附件名硬凑一句假问题
# 去检索、然后按这个假问题落一行归因）。
TEXT_MSGTYPES = ("text", "mixed", "richtext")


def question_from_message(msg: dict) -> str | None:
    """已解析的回调消息 dict → 提问纯文本；解析不出返回 None。

    支持三种形态：
    - `text`：与飞书同构的 `{"text": {"content": "..."}}`；
    - `mixed`：`items[]` 里有 text 项；img/mention/link 项跳过（它们是附件不是
      问题文本，mentions 的昵称混进问题会污染检索）；
    - `richtext`：`content` 是 HTML（如 `<p>问题</p>`），取纯文本。
    """
    msgtype = msg.get("msgtype")
    if msgtype not in TEXT_MSGTYPES:
        return None
    body = msg.get("body")
    body = body if isinstance(body, dict) else {}
    text = ""
    if msgtype == "text":
        block = body.get("text")
        block = block if isinstance(block, dict) else {}
        text = str(block.get("content") or "")
    elif msgtype == "mixed":
        block = body.get("mixed")
        block = block if isinstance(block, dict) else {}
        parts = [str(i.get("text") or "")
                 for i in (block.get("items") or [])
                 if isinstance(i, dict) and i.get("type") == "text"]
        text = "".join(parts)
    else:
        block = body.get("richtext")
        block = block if isinstance(block, dict) else {}
        text = html_to_text(str(block.get("content") or ""))
    text = text.strip()
    return text or None


def extract_question(frame: dict) -> str | None:
    """回调帧 → 提问纯文本（= extract_message + question_from_message 的组合，
    给只用帧的调用方与测试用；解析不出返回 None，静默忽略不回错误）。"""
    msg = extract_message(frame)
    if msg is None:
        return None
    return question_from_message(msg)


def extract_sender_id(frame: dict) -> str | None:
    """回调帧 → 提问者的外部 id（`from.userid`）。

    为什么用 userid 而不是别的：企微回调里能拿到的发送者标识就只有它（corpid
    是**企业**级、不是人），管理端绑定页要填的也是它，管理员在企微通讯录里能
    直接看到同一串。可能是加密串（机器人创建者非超管时）——那时管理员绑定的
    也是回调里看到的那串密文，一致即可，无需解密。
    """
    msg = extract_message(frame)
    if msg is None:
        return None
    userid = msg.get("userid")
    return str(userid) if userid else None


def download_target(frame: dict) -> tuple[str, str] | None:
    """`image`/`file`/`video` 回调 → (下载 url, aeskey)。

    这是**整个协议里唯一需要加解密的地方**：多媒体资源下载回来要用 aeskey 解密。
    取不到 url 返回 None（字段缺失/不是这三种类型）。
    """
    msg = extract_message(frame)
    if msg is None or msg["msgtype"] not in ("image", "file", "video"):
        return None
    block = msg["body"].get(msg["msgtype"])
    block = block if isinstance(block, dict) else {}
    url = block.get("url")
    if not url:
        return None
    return str(url), str(block.get("aeskey") or "")


def decrypt_media(aeskey: str, blob: bytes) -> bytes:
    """用回调里的 aeskey 解密下载到的多媒体资源（AES-256-CBC + PKCS7）。

    密钥口径：官方给的 aeskey 是 base64 编码的 32 字节；**两个都试**（解码成功
    用解码结果、否则用编码串补齐/截断的 utf-8 字节，即"未编码租户"的写法）。
    为什么不只按一种来：这是官网正文没逐字给出的细节，而"密钥口径猜错"的表现是
    解密出来一堆乱码或直接抛异常，现场极难判断是密钥问题还是响应体问题；
    两个都试能让两种租户都跑通，代价只是一次 try。

    解不出来**原地抛异常**（不返回原始字节）：把密文当图片交给 VLM 或当附件存下来，
    比明确报错危险得多。
    """
    if not aeskey:
        raise ValueError("缺少 aeskey，无法解密企微媒体资源")
    try:
        key = base64.b64decode(aeskey, validate=True)
    except (ValueError, TypeError):
        key = b""
    candidates = [key] if len(key) == 32 else []
    raw = aeskey.encode("utf-8")[:32].ljust(32, b"\0")
    if raw not in candidates:          # 两种口径偶尔会落到同一个 key 上，别重复试
        candidates.append(raw)
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            plain = _decrypt_cbc_pkcs7(candidate, blob)
        except Exception as e:  # noqa: BLE001 —— 换下一种密钥口径再试
            last_exc = e
            continue
        # CBC 无完整性校验，填充合法也可能是"解出来正好碰巧"；media 都是
        # 图片/文件这类二进制，用填充合法性 + 非空做最小闸门即可。
        if plain:
            return plain
    raise ValueError(f"企微媒体资源解密失败（aeskey 口径不符或密文损坏）: {last_exc}")


def _decrypt_cbc_pkcs7(key: bytes, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if not blob:
        raise ValueError("密文为空")
    iv, data = blob[:16], blob[16:]
    if not data or len(data) % 16:
        raise ValueError("密文长度不是 16 的整数倍")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(data) + dec.finalize()
    pad = padded[-1]
    if pad == 0 or pad > 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS7 填充不合法")
    return padded[:-pad]


def fetch_and_decrypt(media_url: str, aeskey: str,
                      *, get=None) -> bytes:
    """下载 + 解密（`get` 可注入 httpx.get 形状的函数，测试离线）。
    两步合一的原因：现场只有"拿到可用字节"才算成功，分开暴露会让调用方有可能
    顺手把密文当结果用掉（这正是 decrypt_media 拒绝返回原始字节的同一个理由）。"""
    if get is None:
        import httpx
        get = httpx.get
    resp = get(media_url, timeout=30.0)
    resp.raise_for_status()
    return decrypt_media(aeskey, resp.content)


# ---- 去重（与 feishu_bot.is_duplicate_event 同一手法） ----

_seen_messages: OrderedDict[str, None] = OrderedDict()
_SEEN_MAX = 1024


def is_duplicate_message(msg_id: str) -> bool:
    """进程内 LRU 去重：企微在网络抖动/未及时应答时会重投同一条消息，不去重
    就会对同一个人答两遍（且落两行归因，把归因数据也搞脏）。

    有界的 OrderedDict（容量 1024）而不是无界 set：常驻进程不能靠"消息本来就
    不多"赌内存。重启丢失可接受——重投窗口很短，且丢的是"重复"不是"消息"。

    去重键是 `msgid`（没有则退回 req_id）：req_id 是**我们**给这条回调起的关联
    id，理论上每次投递都不同，只能当兜底。
    """
    if not msg_id:
        return False
    if msg_id in _seen_messages:
        return True
    _seen_messages[msg_id] = None
    while len(_seen_messages) > _SEEN_MAX:
        _seen_messages.popitem(last=False)
    return False


# ---- 单连接互斥 ----

# 进程内单连接闸：企微规定每个机器人同一时间只有 1 条有效长连接，新连接会踢掉
# 旧连接。协议自己会兜底，但"我们踢我们"是最糟的一种：两边都在正常收发，谁被踢
# 取决于连接建立的先后，表现为消息随机丢一半，且日志两边都健康。
# 所以同一进程里第二个 Bot 实例直接报错（编程错误，宁可启动就炸）。
_CONNECT_LOCK = False


def acquire_single_connection() -> None:
    """占用进程内唯一连接槽；已被占用则抛 RuntimeError。"""
    global _CONNECT_LOCK
    if _CONNECT_LOCK:
        raise RuntimeError(
            "本进程已有一条企微长连接：每个机器人同一时间只允许 1 条有效长连接，"
            "第二条会踢掉第一条（表现为消息随机丢一半）。"
            "需要高可用请用主备切换，不要在同一 BotID 上开两条连接。")
    _CONNECT_LOCK = True


def release_single_connection() -> None:
    global _CONNECT_LOCK
    _CONNECT_LOCK = False


def single_connection_held() -> bool:
    """当前进程是否持有那条连接。给测试与排障脚本用
    （`python -c "from kbase.channels import wecom; print(wecom.single_connection_held())"`），
    本卡没有对外的状态接口。"""
    return _CONNECT_LOCK


# ---- 配置 ----

@dataclass
class WeComConfig:
    """运行一个小号长连接进程所需的全部参数。

    secret **不放进配置文件**：`secret_env` 是环境变量名（与 ProviderConfig 的
    api_key_env、sso.client_secret_env 同一规矩，见 kbase/config.py），
    值只从环境变量读（load_env_secret）。
    """
    enabled: bool = False
    bot_id: str = ""
    secret_env: str = "KBASE_WECOM_BOT_SECRET"
    url: str = WECOM_WS_URL
    heartbeat_seconds: float = 30.0    # 官方建议 30 秒
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    streaming: bool = False            # 默认关，见 kbase/config.py WeComConfig
    provider: str | None = None        # 回答用模型；None=llm.active（与飞书同义）

    @classmethod
    def from_app_config(cls, cfg) -> "WeComConfig":
        """从 AppConfig.wecom 装配（字段名一一对应，不做二次解释）。"""
        w = cfg.wecom
        return cls(enabled=w.enabled, bot_id=w.bot_id, secret_env=w.secret_env,
                   url=w.url, heartbeat_seconds=w.heartbeat_seconds,
                   reconnect_min_seconds=w.reconnect_min_seconds,
                   reconnect_max_seconds=w.reconnect_max_seconds,
                   streaming=w.streaming, provider=w.provider or None)


def load_env_secret(secret_env: str) -> str:
    """读密钥。缺失**必须报错**（与 resolve_db_url 对 db.password_env 同一条
    规矩）：静默退化成空串的表现是订阅被拒，而日志里看不出是"没注入密钥"还是
    "密钥错了"，现场排查成本极高。"""
    secret = os.environ.get(secret_env)
    if not secret:
        raise RuntimeError(
            f"企微机器人长连接密钥环境变量 {secret_env} 未设置（配置项 "
            f"wecom.secret_env 指定了它）；请在部署环境注入该变量")
    return secret


# ---- 长连接主体 ----

class WeComBot:
    """长连接机器人：订阅 → 心跳 → 收消息 → 问答 → 回复，断了自动重连。

    依赖全部注入，因此可在无网络、无企微租户的情况下测完整流程：
    - `sock_connect`：建连函数（生产=websockets.connect，测试=FakeSocket）；
    - `answer_fn`：`async (kb_id, question, external_user_id) -> (答案, 引用)`，
      生产=接到 `channels.answer_for_channel`（见 `__main__` 的接线），测试=记账；
    - `dedup`/`sleep`/`clock`/`uniform`：去重器、等待、时钟、抖动。
    """

    def __init__(self, cfg: WeComConfig, *, secret: str, sock_connect,
                 answer_fn, kb_id: str, provider: str | None = None,
                 dedup=is_duplicate_message, sleep=None,
                 clock=time.monotonic, jitter_uniform=None,
                 audit_fn=None) -> None:
        self.cfg = cfg
        self.secret = secret
        self._sock_connect = sock_connect
        self._answer_fn = answer_fn
        self.kb_id = kb_id
        self.provider = provider
        self._dedup = dedup
        # sleep/jitter 默认值**在运行期取**（不写成参数默认值）：参数默认值在
        # import 期就绑定，测试想替换 asyncio.sleep/random.uniform 就必须去改
        # 调用方传参，容易漏掉一处而让测试真的睡 30 秒。
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._clock = clock
        self._jitter = (jitter_uniform if jitter_uniform is not None
                        else random.uniform)
        # 审计（可选）：与 feishu_bot 同一笔（actor="wecom-bot"）。默认不接——
        # 没接线不该让问答失败。
        self._audit_fn = audit_fn
        self._ws = None
        self._held = False          # 本实例是否持有那个进程内唯一连接槽
        self._reconnects = 0        # 重连计数（运维看的信号：一直在涨=对面/网络有问题）

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # ---- 建连 / 订阅 ----

    async def connect_once(self) -> None:
        """建连并完成订阅。任何一步失败都抛异常，由 run_forever 退避重试。

        订阅**只在这里发一次**：官方明确"订阅请求有频率保护，订阅成功后不可
        反复请求"，所以心跳里没有订阅、重连时是新连接所以必须重新订阅。
        """
        acquire_single_connection()
        self._held = True
        try:
            ws = await self._sock_connect(
                self.cfg.url, ping_interval=None,   # 协议级 ping 不是企微要的
                open_timeout=CONNECT_TIMEOUT_S, close_timeout=5)
            self._ws = ws
            await self._send_raw(
                build_subscribe_frame(self.cfg.bot_id, self.secret, new_req_id()))
            # CancelledError 与 TimeoutError 必须分开处理，且**不能让取消被覆盖**：
            # 取消恰好落在 wait_for 内部时（运维停进程的瞬间正在等订阅回执），
            # `raise ... from e` 会把 CancelledError 变成 RuntimeError，run_forever
            # 于是把"要停进程"当成"连接失败"吞掉、退避重连——表现为
            # **Ctrl-C / systemd stop 停不下来**（实测复现）。
            try:
                frame = await asyncio.wait_for(self._recv_raw(),
                                               SUBSCRIBE_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"企微长连接订阅应答超时（{SUBSCRIBE_TIMEOUT_S:g}s 内没收到回执）："
                    f"bot_id 或 secret 不对、或这条连接已被别处的连接顶掉"
                ) from None
            # wait_for 完成与取消同时发生时（见 run_forever 顶部注释）取消会被吞掉：
            # 正常返回的路径也要自己确认一次，否则退出信号在这一轮里彻底丢失。
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise asyncio.CancelledError
            if not is_subscribe_ack(frame):
                raise RuntimeError(f"企微长连接订阅未收到应答: {frame}")
            if not subscribe_ok(frame):
                raise RuntimeError(f"企微长连接订阅被拒: {subscribe_error(frame)}")
            logger.info("企微长连接已订阅: bot_id=%s", self.cfg.bot_id)
        except Exception:
            self._forget_socket()
            raise

    async def _recv_raw(self) -> dict:
        """收一帧并解析。超时/非 JSON 抛异常：**不在这里吞**，由调用方决定
        （订阅应答超时→重连；业务消息解析失败→记日志继续收）。"""
        raw = await self._ws.recv()
        frame = parse_frame(raw)
        if frame is None:
            raise ValueError(f"企微长连接收到非法报文: {str(raw)[:200]}")
        return frame

    async def _send_raw(self, text: str) -> None:
        if self._ws is None:
            raise RuntimeError("企微长连接未建立，无法发送")
        await self._ws.send(text)

    def _forget_socket(self) -> None:
        """丢弃当前 socket 并释放单连接槽。**幂等、可重入**：重连路径与 finally
        都会调用，且人可能先手工调一次再走重连。

        释放条件看 `self._held` 而不是 `self._ws is not None`：重连时
        `_forget_socket()` 之后 `_ws` 已经是 None，若按 `_ws` 判断就永远不再释放，
        一次重连之后进程就再也建不了连接。
        """
        self._ws = None
        if self._held:
            release_single_connection()
            self._held = False

    # ---- 心跳 ----

    async def heartbeat_loop(self) -> None:
        """按配置间隔发应用层 ping。间隔之外不做节流/自适应：
        官方给的就是固定建议值，自己发明更聪明的算法只会让现场无法按文档排查。"""
        while self.connected:
            await self._sleep(self.cfg.heartbeat_seconds)
            if not self.connected:
                return
            await self._send_raw(build_ping_frame(self.cfg.bot_id, new_req_id()))

    # ---- 收包循环 ----

    async def recv_loop(self) -> None:
        """收包：每条消息一个独立任务处理。

        独立任务的理由：一条问答要跑检索+生成（秒级到十几秒），在收包循环里同步
        等它就会**停掉这个连接上的所有其它消息**（包括心跳依赖的 ping 应答处理），
        而且企微对未及时应答的消息会重投——于是重投风暴叠加越来越长的排队。
        任务集合由本循环在断开时处理，不裸留 Task 引用。
        """
        tasks: set[asyncio.Task] = set()
        try:
            while self.connected:
                # 每轮都让出一次控制权：连接断掉时 _recv_raw 立刻抛异常，若不等
                # 下一次 await 才让出，这个循环在"没有待收帧"的状态下会空转，把
                # 心跳任务和测试里的推进都饿死。
                await asyncio.sleep(0)
                frame = await self._recv_raw()
                if is_ping(frame):
                    # 服务端 ping 必须回 pong，否则被判定连接不活跃
                    await self._send_raw(json.dumps(
                        {"cmd": CMD_PONG,
                         "headers": {"req_id": new_req_id()}}, ensure_ascii=False))
                    continue
                if is_subscribe_ack(frame):
                    continue          # 重复回执：订阅已成功，不重复请求
                msg = extract_message(frame)
                if msg is None:
                    continue          # 非用户消息：确认但不处理
                task = asyncio.create_task(self._handle_message(msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            await self._drain(tasks)

    async def _drain(self, tasks: set[asyncio.Task]) -> None:
        """连接断开后收尾在途任务。

        为什么不是一律 cancel：刚收到的那条消息可能正在跑检索+生成，直接取消
        等于"连断的一瞬间收到的消息全丢"（企微不会因为我们断了就重投——它只按
        自己的重投策略来）。所以给在途任务一个收尾窗口，能答完就答完。

        为什么必须带超时：无条件 await 会让一个新连接被一条卡死的任务拖住
        ——连接已经断了，那条回复帧本来就发不出去，等它毫无意义；而重连越晚，
        丢的消息越多（企微不排队等我们）。超时的取消，由调用方（_handle_message）
        自己的 try/except 记日志。
        """
        pending = [t for t in tasks if not t.done()]
        if not pending:
            return
        try:
            await asyncio.wait(pending, timeout=DRAIN_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 收尾失败不影响重连
            logger.exception("企微在途消息收尾失败")
        for task in pending:
            if not task.done():
                task.cancel()
        # 让出一次：被 cancel 的子任务在自己的收尾路径上还要跑（它们的
        # CancelledError 要有人收），不留这一步就会在事件循环关闭时才炸出
        # "Task was destroyed but it is pending" 之类的噪声日志。
        await asyncio.sleep(0)

    async def _handle_message(self, msg: dict) -> None:
        """一条用户消息 → 问答 → 回复。**任何异常都在这里收口**：
        一条消息答不出来不能把整条长连接带走（长连接一断，重连期间所有消息都丢）。"""
        try:
            if not self.kb_id:
                logger.warning("企微机器人未绑定知识库，忽略消息（请在管理端配置）")
                return
            if msg["msgtype"] not in TEXT_MSGTYPES:
                # 媒体类（image/file/video）与语音先明确回一句，**不去重**：
                # 回一句总比让提问者以为机器人没收到好（长连接模式下我们收没收到
                # 只有我们自己知道，不像回调模式有 HTTP 状态码可看）。
                logger.info("企微消息类型暂不支持: %s", msg["msgtype"])
                await self._send_raw(build_reply_frame(
                    msg.get("req_id", ""), UNSUPPORTED_MSGTYPE_REPLY, finish=True))
                return
            key = msg.get("msg_id") or msg.get("req_id") or ""
            if self._dedup(key):
                logger.info("企微消息重投，已受理过: %s", key)
                return
            question = question_from_message(msg)
            if not question:
                return
            answer, citations = await self._answer_fn(
                self.kb_id, question, msg.get("userid"))
            await self.reply(msg.get("req_id", ""), answer or "（未能生成回答）")
            if self._audit_fn is not None:
                self._audit_fn(self.kb_id, question)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 单条消息失败不拖垮连接
            logger.exception("企微机器人回答失败: %s", str(msg.get("body"))[:80])

    # ---- 回复 ----

    async def reply(self, req_id: str, answer: str) -> None:
        """把答案推回去。流式开关决定**推几帧**，不决定答复内容：

        - streaming=False（默认）：只推一帧 `finish=true` 的完整答案——最省事、
          最不容易踩频率保护，且 IM 场景本来就没有逐字刷新的位置（与飞书卡片一致）。
        - streaming=True：先推中间帧（status=streaming，内容为**累积全文**），
          最后一帧 `finish=true`。长连接模式没有流式刷新回调，不主动推就永远是
          静默——这是官方明确要求的开发者侧动作。

        默认关的理由与 AnswerJudgeConfig 同类：新开关不该被一次升级动作
        悄悄打开（这里更实际的是频率保护）。
        """
        if not req_id:
            return
        if not self.cfg.streaming:
            await self._send_raw(build_reply_frame(req_id, answer, finish=True))
            return
        for piece in stream_deltas(answer)[:-1]:
            await self._send_raw(build_stream_frame(req_id, piece, finish=False))
        await self._send_raw(build_reply_frame(req_id, answer, finish=True))

    # ---- 主循环 ----

    def next_backoff(self) -> float:
        """下一次重连等待（指数退避 + 抖动，上限 cfg.reconnect_max_seconds）。

        抖动（0.5~1.0 倍）不是装饰：主备两个进程同时掉线时，固定间隔会让它们
        在同一毫秒一起重连，然后**互相踢**——退避里加抖动才能真的错开。
        """
        base = min(self.cfg.reconnect_min_seconds * (2 ** self._reconnects),
                   self.cfg.reconnect_max_seconds)
        return max(self.cfg.reconnect_min_seconds, base * self._jitter(0.5, 1.0))

    async def _attempt(self) -> float:
        """跑一次连接直到断开，返回本次存活时长（秒）。"""
        started = self._clock()
        await self.connect_once()
        self._reconnects = 0        # 连上了才算成功：重置退避
        hb = asyncio.create_task(self.heartbeat_loop())
        try:
            await self.recv_loop()
        finally:
            hb.cancel()
            self._forget_socket()
        return self._clock() - started

    async def run_forever(self) -> None:
        """常驻：连 → 收 → 断了退避重连，直到被取消。

        **每次重连都是新连接、都必须重新订阅**（旧连接已被企微关闭，订阅状态
        随连接消失）；订阅失败也走同一条重连路径（订阅被拒常见原因是同一个
        BotID 在别处已经连上了——那时按退避重试，等对面让位）。
        """
        while True:
            # 每轮开头显式让出一次并检查"是否已被要求取消"。
            #
            # 必要性（实测踩到，不是防御性代码）：`_attempt` 内部用了
            # `asyncio.wait_for` 等订阅回执，而 CPython 3.11 的实现在"等待对象
            # 恰好同时完成 + 此刻收到 task.cancel()"时**会吞掉这次取消**
            # （`_must_cancel` 被清掉却没抛 CancelledError，任务停在 cancelling
            # 状态且再也打断不了）。表现就是：**Ctrl-C / systemd stop 停不下来，
            # 进程继续重连**。在循环顶部让出一次能让挂起的取消在下一轮立刻兑现；
            # `cancelling()` 检查则覆盖"取消已经在返回前落过地"的情况。
            # 另：下面的 except Exception 分支本身也可能被取消打断（退避睡眠
            # 期间收到 SIGTERM），那种情况由 sleep 自己抛 CancelledError 兜住。
            await asyncio.sleep(0)
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                self._forget_socket()
                raise asyncio.CancelledError
            try:
                await self._attempt()
            except asyncio.CancelledError:
                # 取消是**唯一**不该重连的退出方式（进程要停了）。
                # 分支顺序不能反：CancelledError 不是 Exception，先写 Exception
                # 分支会把它漏给更外层，重连就再也停不下来。
                self._forget_socket()
                raise
            except Exception as e:  # noqa: BLE001 —— 任何连接级失败都重连
                self._forget_socket()
                self._reconnects += 1
                delay = self.next_backoff()
                logger.warning("企微长连接断开，%.1fs 后重连（第 %d 次）: %s",
                               delay, self._reconnects, e)
                await self._sleep(delay)


# ---- 独立进程入口 ----

def _make_audit_fn(sf):
    """审计闭包：与 feishu_bot 路由同一笔（actor="wecom-bot"，机器人是执行方）。
    提问者身份在 T12 归因行的 actor 里，两处不是一回事，不合并。"""
    def _audit(kb_id: str, question: str) -> None:
        from kbase.audit import write_audit
        write_audit(sf, actor="wecom-bot", action="wecom_bot_answer",
                    resource=f"kb_id={kb_id}", detail=question[:100])
    return _audit


def build_bot(svc, *, secret: str, sock_connect=None) -> WeComBot:
    """把 AppConfig + Services 接成一个 Bot（生产接线，测试不用它）。

    问答**只经 T19 适配层**：actor 由 channels.resolve_actor 按
    `(channel="wecom", userid)` 现查 channel_identities，再调
    `answer_for_channel`——所以企微渠道拿到的是与网页端逐字相同的
    「检索 → 可用依据 → 引用 → 拒答」语义，归因行的 channel 也是 "wecom"。
    本模块不出现任何检索/生成/拒答代码。
    """
    from kbase.channels import core as channels_core

    cfg = WeComConfig.from_app_config(svc.cfg)
    sf = svc.sf
    provider = svc.cfg.wecom.provider or None

    async def _answer(kb_id: str, question: str, external_user_id: str | None):
        actor = channels_core.resolve_actor(sf, channel=CHANNEL,
                                           external_user_id=external_user_id)
        # T12 归因行由 answer_for_channel 统一落（channel=wecom），越权也落
        # （bucket=scope_denied）——这里不重复记一笔。
        return await channels_core.answer_for_channel(
            svc, kb_id=kb_id, question=question, actor=actor, provider=provider)

    if sock_connect is None:
        import websockets
        sock_connect = websockets.connect
    kb_id = _bound_kb_id(sf)
    return WeComBot(cfg, secret=secret, sock_connect=sock_connect,
                    answer_fn=_answer, kb_id=kb_id, provider=provider,
                    audit_fn=_make_audit_fn(sf))


def _bound_kb_id(sf) -> str:
    """企微机器人绑定的知识库：沿用飞书机器人同一处 KV（AppSetting，
    `feishu_bot_kb_id` 是"机器人绑定库"的唯一事实源，管理页维护）。

    为什么不新开一个 KV：绑定库是**渠道无关**的运营判断（"这个机器人答哪个库的
    内容"），两个渠道各存一份必然漂移——运维改了飞书那一份、忘了企微这份，
    表现就是"同一句话在两个渠道答案不同"，而且从日志里完全看不出来。
    本卡不加 UI，复用既有配置键是这里唯一不引入新状态的做法；
    代价如实记在这里：**企微与飞书共用同一个绑定库**（要各自绑不同的库，
    得先在管理页拆开这个键，那是另一张卡）。
    """
    from kbase import feishu_bot
    return feishu_bot.get_settings(sf)["kb_id"] or ""


def main(argv: list[str] | None = None) -> int:
    """`python -m kbase.channels.wecom --config config/kbase.yaml`

    独立进程运行（理由见模块 docstring 末段）：**同一 BotID 只能有一条连接**，
    多 worker 部署 web 服务时若把长连接塞进 web 进程，滚动重启期间新旧进程会
    互相踢。运维侧请用 systemd/compose 单独拉起本进程并配置自动重启，
    且保证一个 BotID 只有一个进程。
    """
    parser = argparse.ArgumentParser(
        prog="python -m kbase.channels.wecom",
        description="企业微信智能机器人长连接进程（T20）")
    parser.add_argument("--config", default="config/kbase.yaml",
                        help="配置文件路径（默认 config/kbase.yaml）")
    parser.add_argument("--log-level", default="INFO",
                        help="日志级别（默认 INFO）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # 先只读配置判断开关，**再**决定要不要装配服务：build_services 会按配置加载
    # 向量化模型（lite 档要现场下 bge-m3、standard 档要连 TEI），一个只想确认
    # "开关是不是开着"的启动过程不该拉起这些重活。未启用时直接退出，零副作用。
    from kbase.config import load_config
    cfg = WeComConfig.from_app_config(load_config(args.config))
    if not cfg.enabled:
        print("企微机器人未启用（wecom.enabled=false），不建立长连接。"
              "确认要在管理后台开启「API 模式 → 长连接」后，把 wecom.enabled 置为 true。")
        return 0
    if not cfg.bot_id:
        print("企微机器人缺少 bot_id（管理后台「智能机器人」页可见），无法订阅。")
        return 2
    secret = load_env_secret(cfg.secret_env)      # 缺失即报错，不静默

    from kbase.api.services import build_services
    svc = build_services(args.config)
    bot = build_bot(svc, secret=secret)

    async def _run() -> None:
        task = asyncio.create_task(bot.run_forever())
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, ValueError):   # Windows/非主线程
                pass
        await stop.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    print(f"企微长连接启动：bot_id={cfg.bot_id} url={cfg.url} "
          f"心跳={cfg.heartbeat_seconds:g}s 流式={cfg.streaming}")
    asyncio.run(_run())
    return 0


if __name__ == "__main__":      # pragma: no cover —— 进程入口不参与单测
    raise SystemExit(main())
