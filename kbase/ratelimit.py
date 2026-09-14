"""API Key 限流与用量（T09）：进程内滑动窗口 + 逐日计数 + 来源 IP 白名单
匹配 + 逐日用量落库（api_key_usage_daily）。

**限流口径（务必如实对外表述）**：本模块的状态全在进程内存里
（dict + threading.Lock，不引 Redis/slowapi 等外部依赖）。因此：
- **lite 档（单进程 uvicorn）**：窗口与当日计数是**精确值**；
- **standard 档（多 worker / 多副本）**：每个进程各持一份计数，实际放行量
  约为配置值 × 进程数——这是**每进程近似**，不是硬限流。客户侧配额只承诺
  "单进程可精确"，多进程下的精确控制需外部网关（Nginx limit_req 等）。

对外出口：路由级依赖 make_rate_limit_dependency 挂在 /api 与 /v1 两个
router 上（kbase/api/main.py、kbase/api/routes/openai_compat.py），超限回
429 + Retry-After。只有 API Key 身份受限（会话 Cookie / auth="off" 合成
actor 没有 key_id，直接放行——行为与引入限流前一致）。

用量只经管理端鉴权接口（GET /api/settings/api-keys/{id}/usage）读出，
**绝不进无鉴权的 /metrics**。
"""
import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request
from sqlalchemy import text

from kbase.models import ApiKeyUsageDaily

# 滑动窗口长度（秒）：rpm 的语义就是"最近 60 秒内的请求数"。
WINDOW_SECONDS = 60.0

# 同一 Key 的 last_used_at 写库最小间隔（秒）：鉴权在每请求关键路径上，
# 不能每个请求都写库（见 auth/deps.py 的调用点）。
LAST_USED_WRITE_INTERVAL = 60.0

# 单条 UPSERT：requests（+1）与 token 两项分开写，避免互相覆盖
# （见 record_requests / record_tokens 的注释）。
_REQUESTS_UPSERT_SQL = text(
    "INSERT INTO api_key_usage_daily "
    "(key_id, day, requests, prompt_tokens, completion_tokens, tokens_estimated) "
    "VALUES (:k, :d, :r, 0, 0, :e) "
    "ON CONFLICT (key_id, day) DO UPDATE SET "
    "requests = api_key_usage_daily.requests + :r"
)

_TOKENS_UPSERT_SQL = text(
    "INSERT INTO api_key_usage_daily "
    "(key_id, day, requests, prompt_tokens, completion_tokens, tokens_estimated) "
    "VALUES (:k, :d, 0, :p, :c, :e) "
    "ON CONFLICT (key_id, day) DO UPDATE SET "
    "prompt_tokens = api_key_usage_daily.prompt_tokens + :p, "
    "completion_tokens = api_key_usage_daily.completion_tokens + :c, "
    "tokens_estimated = :e"
)


def utc_day(epoch: float) -> str:
    """纪元秒 → UTC 日期串（YYYY-MM-DD）。逐日计数/用量的"日"一律按 UTC，
    避免多时区部署下同一天被切成两段（管理端展示也照 UTC 标注）。"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def seconds_to_next_day(epoch: float) -> float:
    """距下一个 UTC 零点的秒数——每日配额超限时的 Retry-After 依据。"""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    tomorrow = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow.timestamp() + 86400) - epoch


def estimate_tokens(text: str) -> int:
    """按字符数粗估 token 数：CJK 字≈1 token/字，其余≈4 字符/token。

    **兜底口径，不是首选**：/v1 的 usage 优先用上游真实值（provider 流式带
    stream_options.include_usage，末块回传 usage，见
    kbase/plugins/llm/openai_compat.py）；只有端点不认该参数（provider 内部
    已降级重试）或末块没带 usage 时才走本估算，并在响应里如实标注
    （非流式 x-kbase-usage-estimated 头 + usage_estimated 字段，流式见末块
    usage_estimated），落库记 tokens_estimated=true。估算只用于用量看板与
    配额参考，**不是计费依据**。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    return cjk + math.ceil((len(text) - cjk) / 4)


def ip_allowed(raw: str | None, ip: str | None) -> bool:
    """来源 IP 是否命中 Key 的白名单。

    raw 为空（NULL/空串）=不限（老库/未配置，与升级前行为一致）。
    命中规则：精确 IP 或 CIDR 网段（ip_network(entry, strict=False)，
    于是裸 IP "10.0.0.1" 等价于 "10.0.0.1/32"）。
    白名单里有解析不了的脏条目、或请求侧拿不到来源 IP → **拒绝**（fail
    closed）：写入侧已校验（api/schemas.py），脏数据只可能来自人工改库，
    此时宁可拒绝也不放行。
    """
    if not raw:
        return True
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(entries, list) or not entries:
        return False          # 脏数据（非数组/空数组）→ 收不到白名单 → 拒绝
    if ip is None:
        return False
    try:
        addr = ip_address(ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ip_network(str(entry), strict=False):
                return True
        except ValueError:
            continue          # 单条脏数据跳过，但不因此整体放行
    return False


class RateLimiter:
    """进程内限流状态：每 Key 的 60 秒滑窗 + 当日计数 + last_used 写节流。

    全部状态用一把 threading.Lock 串行化（uvicorn 的同步依赖跑在线程池里，
    会有真并发）。clock 可注入（测试用可控时钟），默认 time.time——纪元秒。
    """

    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}          # key_id -> 窗口内命中时间戳
        self._daily: dict[str, tuple[str, int]] = {}   # key_id -> (day, 计数)
        self._last_used: dict[str, float] = {}     # key_id -> 上次写库时间

    def check(self, key_id: str, *, rpm: int | None = None,
              daily_quota: int | None = None, day: str | None = None,
              now: float | None = None) -> int:
        """准入判定：返回 0=放行（并记一次命中）；>0=超限，值为 Retry-After 秒数。

        两道闸独立判定，任一超限都**不记命中**——否则被拒的请求会不断把
        窗口/当日后推，变成"越拒绝越出不来"。
        """
        now = self.clock() if now is None else now
        day = day or utc_day(now)
        with self._lock:
            if rpm:
                hits = self._hits.setdefault(key_id, deque())
                while hits and now - hits[0] >= WINDOW_SECONDS:
                    hits.popleft()
                if len(hits) >= rpm:
                    return max(1, math.ceil(hits[0] + WINDOW_SECONDS - now))
            if daily_quota and self._used(key_id, day) >= daily_quota:
                return max(1, math.ceil(seconds_to_next_day(now)))
            if rpm:
                self._hits[key_id].append(now)
            self._bump(key_id, day)
            return 0

    # 下面两个是**不加锁**的内部读取/自增：check() 已在锁内，threading.Lock
    # 不可重入，公开方法里再取一次锁就是死锁。
    def _used(self, key_id: str, day: str) -> int:
        d, n = self._daily.get(key_id, ("", 0))
        return n if d == day else 0

    def _bump(self, key_id: str, day: str) -> None:
        d, n = self._daily.get(key_id, (day, 0))
        self._daily[key_id] = (day, n + 1 if d == day else 1)

    def daily_used(self, key_id: str, day: str) -> int:
        """进程内当日计数；跨日（day 变了）自动视为 0=新一天从头计。"""
        with self._lock:
            return self._used(key_id, day)

    def seed_daily(self, key_id: str, day: str, used: int) -> None:
        """用库里的当日累计值播种进程内计数（仅在该 Key 当日尚无进程内计数时）。

        意义：standard 档滚动重启/多副本时不会因为内存清零而白送一整轮配额；
        lite 档重启同理。库里的当日行由 record_requests 持续写入，故播种值
        与真实累计一致（多 worker 下每进程只播种一次，仍是近似）。
        """
        with self._lock:
            d, _ = self._daily.get(key_id, ("", 0))
            if d != day:
                self._daily[key_id] = (day, used)

    def allow_last_used_write(self, key_id: str,
                              now: float | None = None) -> bool:
        """last_used_at 是否该写库：同一 Key 每分钟至多一次（高频 Key 下
        把"每请求一次写"降为"每分钟一次写"）。命中即记账，调用方只在拿到
        True 时写库。"""
        now = self.clock() if now is None else now
        with self._lock:
            last = self._last_used.get(key_id)
            if last is not None and now - last < LAST_USED_WRITE_INTERVAL:
                return False
            self._last_used[key_id] = now
            return True

    def reset(self) -> None:
        """清空全部进程内状态（测试用：模块级单例跨用例共享）。"""
        with self._lock:
            self._hits.clear()
            self._daily.clear()
            self._last_used.clear()


# 模块级单例：路由级依赖与鉴权通道共用同一份状态。
limiter = RateLimiter()


def _requests_on_day(sf, key_id: str, day: str) -> int:
    with sf() as s:
        row = s.get(ApiKeyUsageDaily, (key_id, day))
        return int(row.requests) if row is not None else 0


def record_requests(sf, key_id: str, *, day: str | None = None) -> None:
    """记一次已放行的 API Key 请求（逐日 requests +1，UPSERT 原子自增）。"""
    day = day or utc_day(limiter.clock())
    with sf() as s:
        s.execute(_REQUESTS_UPSERT_SQL,
                  {"k": key_id, "d": day, "r": 1, "e": False})
        s.commit()


def record_tokens(sf, key_id: str, *, prompt_tokens: int,
                  completion_tokens: int, tokens_estimated: bool = True,
                  day: str | None = None) -> None:
    """记一次生成的 token 用量。tokens_estimated 取本次计量的口径：上游回传了
    真实 usage 传 False，回退字符估算才传 True（默认值保守取 True）——行级
    标记即"最近一次 token 写入是否为估算"，管理端据此提示口径。"""
    if not prompt_tokens and not completion_tokens:
        return
    day = day or utc_day(limiter.clock())
    with sf() as s:
        s.execute(_TOKENS_UPSERT_SQL,
                  {"k": key_id, "d": day, "p": int(prompt_tokens),
                   "c": int(completion_tokens), "e": bool(tokens_estimated)})
        s.commit()


def usage_days(sf, key_id: str, *, days: int = 30) -> list[dict]:
    """近 days 天（含今天）的逐日用量，按日期升序（管理端画趋势/列表）。
    只读接口，调用方必须已过 require_admin。"""
    since = utc_day(limiter.clock() - (days - 1) * 86400)
    with sf() as s:
        rows = (s.query(ApiKeyUsageDaily)
                .filter(ApiKeyUsageDaily.key_id == key_id,
                        ApiKeyUsageDaily.day >= since)
                .order_by(ApiKeyUsageDaily.day.asc()).all())
        return [{"day": r.day, "requests": int(r.requests or 0),
                 "prompt_tokens": int(r.prompt_tokens or 0),
                 "completion_tokens": int(r.completion_tokens or 0),
                 "tokens_estimated": bool(r.tokens_estimated)} for r in rows]


def make_rate_limit_dependency(sf):
    """路由级依赖工厂：按 actor 上的 Key 配额判定，超限 429+Retry-After。

    挂在 /api 与 /v1 两个 router 上（必须**排在 actor 依赖之后**——依赖按
    声明顺序解析，本依赖读 request.state.actor）。非 API Key 身份无 key_id
    → 直接返回，行为与引入限流前完全一致。
    """

    def _rate_limit(request: Request) -> None:
        actor = getattr(request.state, "actor", None) or {}
        key_id = actor.get("key_id")
        if not key_id:
            return
        rpm = actor.get("rpm")
        quota = actor.get("daily_quota")
        now = limiter.clock()
        day = utc_day(now)
        if quota and limiter.daily_used(key_id, day) == 0:
            # 进程内当日计数为空 → 用库里当日累计值播种（重启不白送配额）。
            limiter.seed_daily(key_id, day, _requests_on_day(sf, key_id, day))
        retry_after = limiter.check(key_id, rpm=rpm, daily_quota=quota,
                                    day=day, now=now)
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited",
                        "message": f"API Key 请求超出配额，请在 {retry_after} 秒后重试"},
                headers={"Retry-After": str(retry_after)})
        try:
            record_requests(sf, key_id, day=day)
        except Exception:      # noqa: BLE001 计量是旁路，绝不把请求打成 500
            import logging
            logging.getLogger(__name__).exception("API Key 用量落库失败: %s", key_id)

    return _rate_limit
