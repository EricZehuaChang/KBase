"""轻量许可证校验：Ed25519 验签 license.json，四态 trial/valid/expired/invalid。

T16：从"纯展示"升级为**可开关的硬拦截**。产品负责人已拍板——"授权到期，
直接拦截；现阶段当前不拦截，留一个开关给我，没有交付客户"：
- 拦截代码路径真实存在且有测试覆盖（见 tests/test_license.py 的
  enforce=true 用例与离线续期全链路用例）；
- 但**配置默认 enforce=false**（config/kbase.standard.yaml 的 license 段），
  合并本改动不改变当前生产行为。这是有意为之的默认值，不是漏配。

license.json 路径：env KBASE_LICENSE_FILE 优先；否则默认仓库根目录下的
license.json（未提交到 git，客户私有化部署时按需放置，见
scripts/gen_license.py）。续期走 POST /api/license（见
kbase/api/routes/license.py），写回的就是这个路径。

两种格式，验签时自动识别（判据：文件里有 edition 字段就是 v2）：
- v1（历史格式，owner 自己的演示证就是这份）：{"org", "expires", "signature"}
- v2（T16 起）：{"org", "expires", "edition", "seats", "features"?, "signature"}
签名对象是除 signature 外的**全部字段**的 canonical JSON（sort_keys、
ensure_ascii=False、无多余空格、UTF-8），与 scripts/gen_license.py 的
_signable_payload 必须逐字节一致（字段增减要两边同改）。
v1 文件永远仍能验证通过——否则本改动一上线，所有已签发的证书当场作废。

缓存（性能红线）：拦截判定挂在每个请求上，但**绝不能每个请求都读文件+验签**
或重新解析配置。见 effective_config()/cached_license() 的注释：配置读一次
进内存，许可证文件按 "路径+mtime+大小" 做失效判据——生产上文件不变，后续
请求只做一次内存查找；enforce=false（当前默认）时连文件都不 stat。
"""
import base64
import json
import os
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Request
from fastapi.responses import JSONResponse

from kbase.errors import AppError

# 与私钥配对的公钥（scripts/gen_license.py 生成时打印）。私钥保存在仓库
# 之外（D:\Claude Code\kbase-license-private.pem，不提交到 git），只有
# 持有该私钥才能签出这里能验证通过的 license.json。
_PUBLIC_KEY_B64 = "IqOgil+VsdctRy7g6YjUnEtDQT8gbof5dn9MpjPTHfI="

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LICENSE_PATH = _REPO_ROOT / "license.json"
# 未指定配置路径时的兜底（与 create_app 的默认 config_path 一致）：直接
# python -c "from kbase.license import ..." 这类脱离应用的调用也能读到开关。
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "kbase.yaml"

# T16 拦截开关的默认值。
# enforce=False：**故意**保持"不拦截"——负责人要的是"开关在我手里"，交付前
# 不想让授权到期直接掐断服务（他名下也还没有交付客户）。
# grace_days=14：到期后仍宽限 14 天再拦，给现场留出上传新证的时间窗。
_DEFAULT_ENFORCE = False
_DEFAULT_GRACE_DAYS = 14

# 拦截时的 HTTP 语义：402（授权到期不是 401/403——凭据和角色都没问题，
# 是"这份授权已失效"，续期入口见响应 detail）。
LICENSE_EXPIRED_STATUS = 402

# 到期拦截的**作用域**：只拦后端 API（业务端点都在 /api 下，OpenAI 兼容层
# 在 /v1 下）。静态资源与前端路由（/、/kb、/admin、/assets/*）一律放行——
# 前端 shell 必须能打开，才能把"授权已到期、请上传新证书"这张页面画出来；
# 把静态资源也拦成 402 只会让现场看到一个无法自助的空窗口。
BLOCKED_PREFIXES = ("/api", "/v1")

# 作用域内的**豁免清单**（永远可达）。少了其中任何一条，离线续期就成了死结：
# 管理员必须先能登录（/api/auth/*）才拿得到上传权限，"现在的状态"必须随时
# 可查（/api/license）否则前端连提示都画不出。续期上传端点本身
# （POST /api/license）同样在清单里——它就是用来解开拦截的那把钥匙，不能
# 自己也被拦。/healthz、/metrics 是运维探活，被拦等于把监控打瞎。
ALLOWED_PREFIXES = ("/api/auth/",)
ALLOWED_EXACT = ("/api/license", "/healthz", "/metrics", "/docs", "/redoc",
                 "/openapi.json")

# 许可证里可被 require_feature 引用的功能位（T16）。
KNOWN_FEATURES = ("governance", "sso", "audit_export", "ops_attribution",
                  "answer_eval", "bundle_export")


def _license_path() -> Path:
    env_path = os.environ.get("KBASE_LICENSE_FILE")
    return Path(env_path) if env_path else _DEFAULT_LICENSE_PATH


# trial/invalid 态的公共字段（没有证书就没有 org/edition…，但 key 集合保持
# 一致，前端统一按字段渲染即可）。
_EMPTY_STATE = {"org": None, "expires": None, "edition": None, "seats": None,
                "features": None, "format": None, "grace_days_left": None}


# ---------------------------------------------------------------- 配置（开关）

@dataclass(frozen=True)
class LicensePolicy:
    """license 段的生效配置（启动期读一次，之后只在内存里）。"""
    enforce: bool = _DEFAULT_ENFORCE
    grace_days: int = _DEFAULT_GRACE_DAYS
    path: Path = _DEFAULT_LICENSE_PATH


_config_path: Path | None = None
_config_cache: tuple[Path, LicensePolicy] | None = None
_config_lock = threading.Lock()


def _parse_policy(raw: Any, path: Path) -> LicensePolicy:
    """从 YAML 的 license 段解析策略；段缺失/值非法一律回落默认值。

    解析失败**不抛异常**：许可证是旁路能力，配置段写坏不该让服务起不来。
    但 enforce 只认真正的 bool——"enforce: 'true'"（带引号被读成 str）
    这类手误一律当**不拦**处理，不做 "true".lower() 这种宽松猜测：猜错的
    方向会变成"以为拦着其实没拦"，比启动报错更危险。"""
    if not isinstance(raw, dict):
        return LicensePolicy(path=path)
    enforce = raw.get("enforce", _DEFAULT_ENFORCE)
    if not isinstance(enforce, bool):
        enforce = _DEFAULT_ENFORCE
    grace = raw.get("grace_days", _DEFAULT_GRACE_DAYS)
    if not isinstance(grace, int) or isinstance(grace, bool) or grace < 0:
        grace = _DEFAULT_GRACE_DAYS
    return LicensePolicy(enforce=enforce, grace_days=grace, path=path)


def configure(config_path: str | Path | None = None) -> None:
    """由 create_app 在启动期调用：记下配置路径并清空缓存。

    只记路径不读文件——真正的读取发生在第一次 effective_config()。"""
    global _config_path, _config_cache
    with _config_lock:
        _config_path = Path(config_path) if config_path else None
        _config_cache = None


def effective_config() -> LicensePolicy:
    """当前生效的 license 策略，进程内按"配置路径"缓存。

    生产路径上 create_app 启动时调一次 configure()，此后每个请求都命中
    缓存里的同一个 LicensePolicy 对象——没有文件 IO、没有 YAML 解析。
    只有测试用 configure() 换路径（或换 env KBASE_LICENSE_FILE）时才会
    重新读一次；注意策略里的 path 也要跟着 env 走，所以换 env 的测试用例
    必须调 configure() 显式刷新（tests/test_license.py 的 _patched_env）。"""
    global _config_cache
    config_path = _config_path or _DEFAULT_CONFIG_PATH
    license_path = _license_path()
    cached = _config_cache
    if cached is not None and cached[0] == config_path:
        policy = cached[1]
        if policy.path == license_path:
            return policy
        # 只有许可证路径变了（测试换 KBASE_LICENSE_FILE）：复用已解析的
        # 开关值，不重读配置文件，只换 path。
        policy = LicensePolicy(enforce=policy.enforce,
                               grace_days=policy.grace_days, path=license_path)
        _config_cache = (config_path, policy)
        return policy
    with _config_lock:
        cached = _config_cache
        if cached is not None and cached[0] == config_path:
            policy = cached[1]
            if policy.path != license_path:
                policy = LicensePolicy(enforce=policy.enforce,
                                       grace_days=policy.grace_days,
                                       path=license_path)
                _config_cache = (config_path, policy)
            return policy
        policy = _parse_policy(_read_config_section(config_path), license_path)
        _config_cache = (config_path, policy)
        return policy


def _read_config_section(path: Path) -> Any:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return raw.get("license") if isinstance(raw, dict) else None


# ------------------------------------------------------------ 许可证解析/验签

def _signable_payload(data: dict) -> bytes:
    """签名对象的 canonical 编码：**除 signature 外的全部字段**。

    v1（只有 org/expires）与 v2（多 edition/seats/features）共用这一条实现，
    所以 v1 的签名对象仍是 {"org","expires"} 两字段——历史证书的签名字节
    一个 bit 都没变，验签结果不变。"""
    return json.dumps({k: v for k, v in data.items() if k != "signature"},
                      sort_keys=True, ensure_ascii=False).encode("utf-8")


def _verify_signature(data: dict, signature_b64: str) -> bool:
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(_PUBLIC_KEY_B64))
    try:
        public_key.verify(base64.b64decode(signature_b64),
                          _signable_payload(data))
        return True
    except (InvalidSignature, ValueError):
        return False


def _optional_str(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _optional_int(data: dict, key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


def _optional_features(data: dict, key: str = "features") -> list[str] | None:
    """features 列成字符串清单；缺省/空清单 → None（=不限功能）。"""
    value = data.get(key)
    if not isinstance(value, list):
        return None
    items = [f for f in value if isinstance(f, str) and f]
    return items or None


def parse_license(raw: Any) -> dict | None:
    """把 license.json 的内容解析/验签成统一结果；不合法返回 None。

    返回形状（v1 文件里没有的字段为 None）：
    {"status", "org", "expires", "edition", "seats", "features", "format"}
    status: valid（未到期）| expired（已到期，无论是否在宽限期内——宽限期
    是**拦截判定**的事，不是证书状态的事，见 enforcement_state）。
    """
    if not isinstance(raw, dict):
        return None
    org, expires = raw.get("org"), raw.get("expires")
    signature = raw.get("signature")
    if not isinstance(org, str) or not org:
        return None
    if not isinstance(expires, str) or not isinstance(signature, str):
        return None

    # v2 判据：有 edition 字段。**必须在验签前判定**——签名对象因格式而异，
    # v2 文件若按 v1 验证，edition/seats 就成了"签名外可随便改"的字段
    # （篡改 edition 即可自助升级客户版本），这正是 T16 要堵的洞。
    edition = _optional_str(raw, "edition")
    is_v2 = edition is not None
    if is_v2 and _optional_int(raw, "seats") is None:
        # v2 的 seats 是必填位：签出来没座位数等于签了个说不清的授权
        return None

    if not _verify_signature(raw, signature):
        return None

    try:
        expires_date = date.fromisoformat(expires)
    except ValueError:
        return None

    status = "expired" if expires_date < date.today() else "valid"
    return {"status": status, "org": org, "expires": expires,
            "edition": edition, "seats": _optional_int(raw, "seats"),
            "features": _optional_features(raw),
            "format": "v2" if is_v2 else "v1"}


def _read_json(path: Path) -> tuple[bool, Any]:
    """返回 (文件是否存在, 内容)。读不到（权限/目录等）当"不存在"处理。"""
    try:
        return True, json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return True, None


def read_license_file(path: Path) -> dict | None:
    """从磁盘读并解析许可证；文件不存在/读不了/内容非法 → None。"""
    _exists, raw = _read_json(path)
    return parse_license(raw)


def check_license() -> dict:
    """返回当前许可证状态（**每次实读文件**，不做缓存）。

    调用方是状态卡这类低频接口，要的是"现在到底什么样"，走缓存反而会在
    续期上传后短暂显示旧状态。每个请求的拦截判定不走这里，走
    cached_license()。

    形状（三种状态**同一组 key**，前端不必按状态分支判字段有无）：
    - 无文件 → status="trial"；
    - 文件坏/验签不过 → status="invalid"；
    - 验签通过 → status="valid"|"expired"，并带 org/expires/edition/seats/
      features/format（v1 证书的 edition/seats 为 None，format="v1"）。
    grace_days_left 仅 expired 态有值（到期日为第 0 天），其余为 None。"""
    policy = effective_config()
    exists, raw = _read_json(policy.path)
    if not exists:
        return {**_EMPTY_STATE, "status": "trial"}
    state = parse_license(raw)
    if state is None:
        return {**_EMPTY_STATE, "status": "invalid"}
    state = dict(state)
    state["grace_days_left"] = grace_days_left(state, policy.grace_days)
    return state


# ------------------------------------------------------------- 拦截判定与缓存

# 许可证文件缓存：key 是路径，value 是 ((mtime_ns, size), 解析结果)。
# 失效判据用 stat 的 (mtime_ns, size) 而不是"每 N 秒重读"——续期上传会
# 原子替换文件（见 routes/license.py），mtime 必变、立即生效；文件不变时
# 永不重读。
_MISSING_STAMP = (-1, -1)
_license_cache: dict[str, tuple[tuple[int, int], dict | None]] = {}
_license_lock = threading.Lock()


def invalidate_license_cache() -> None:
    """丢弃缓存（续期上传成功后显式调用；正常路径靠 mtime 自动失效）。"""
    with _license_lock:
        _license_cache.clear()


def cached_license(path: Path | None = None) -> dict | None:
    """按文件 (mtime_ns, size) 缓存的许可证解析结果。

    生产上证书是"签发一次、几个月不动"的静态文件：每个请求只有一次
    os.stat（比对 mtime/size 是否变化），命中后直接返回内存里的 dict——
    没有读文件、没有 JSON 解析、没有 Ed25519 验签。文件不存在时的负结果
    同样被缓存（stamp 用 _MISSING_STAMP），所以"没放证书"的部署也不会
    每个请求去 stat 一个不存在的路径。
    """
    target = path or effective_config().path
    key = str(target)
    try:
        st = target.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = _MISSING_STAMP

    with _license_lock:
        hit = _license_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    state = read_license_file(target) if stamp != _MISSING_STAMP else None
    with _license_lock:
        _license_cache[key] = (stamp, state)
    return state


def grace_days_left(state: dict | None, grace_days: int) -> int | None:
    """宽限期剩余天数（到期日为第 0 天，之后每天递减）；未到期/无证 → None。

    返回 0 = 宽限期已用尽（拦截判定的触发条件）。"""
    if not state or state.get("status") != "expired" or not state.get("expires"):
        return None
    try:
        expires_date = date.fromisoformat(state["expires"])
    except ValueError:
        return None
    return max(grace_days - (date.today() - expires_date).days, 0)


def enforcement_state(path: Path | None = None) -> dict:
    """本次请求的拦截判定（拦截中间件与功能位依赖共用这一处）。

    blocked 的三个条件（缺一不可）：
    1. 开关打开（enforce=true）——关着时**任何**状态都不拦；
    2. 证书状态 expired（trial/invalid 不拦：这里只签"明确写了到期日且已
       过期"的证书；文件写坏了属于该修文件，不该把服务掐掉）；
    3. 超出宽限期（到期日 + grace_days < 今天，即 grace_days_left == 0）。
    """
    policy = effective_config()
    if not policy.enforce:
        return {"enforce": False, "blocked": False, "status": "off",
                "grace_days": policy.grace_days, "grace_days_left": None}
    state = cached_license(path or policy.path)
    left = grace_days_left(state, policy.grace_days)
    return {"enforce": True,
            "blocked": bool(state and state.get("status") == "expired"
                            and left == 0),
            "status": (state or {}).get("status", "trial"),
            "grace_days": policy.grace_days, "grace_days_left": left}


def in_blocked_scope(path: str) -> bool:
    """该路径是否要受到期拦截（纯字符串比较，无 IO）。

    True = 在作用域内且不在豁免清单里，到期就该 402；
    False = 不拦：静态资源/前端路由（不在 /api、/v1 下），或落在豁免清单里。
    注意 /api/auth/ 用带尾斜杠的前缀匹配：/api/authentic 这种"恰好打头"的
    路径不该被误当成登录端点放行。"""
    if not any(path == p or path.startswith(p + "/")
               for p in BLOCKED_PREFIXES):
        return False
    if path in ALLOWED_EXACT:
        return False
    return not any(path.startswith(p) for p in ALLOWED_PREFIXES)


# --------------------------------------------------------- 拦截与功能位依赖

def expired_error() -> AppError:
    """到期拦截的统一错误体（i18n code error.license_expired）。"""
    policy = effective_config()
    return AppError(
        "error.license_expired",
        "授权已到期（含 {grace} 天宽限期），请在设置 → 许可证状态上传新的 "
        "license.json 续期", status=LICENSE_EXPIRED_STATUS, grace=policy.grace_days)


def make_license_guard():
    """ASGI 中间件工厂：enforce=true 且超出宽限期时，业务端点返回 402。

    **作用域**：只拦 /api 与 /v1（后端 API），静态资源与前端路由一律放行
    ——前端 shell 能打开，才画得出"授权已到期、请上传新证书"这张页面。
    **豁免清单**（ALLOWED_EXACT / ALLOWED_PREFIXES）必须完整——管理员被拦
    在外面时还得能登录并上传新证书，这条离线续期链路靠的就是它：
    /api/auth/*（登录换 Cookie）+ POST /api/license（上传）+ GET /api/license
    （看当前状态）+ /healthz（探活）。见 tests/test_license.py 的
    test_offline_renewal_round_trip：到期被拦 → 登录 → 上传 → 放行。

    enforce=false（当前默认）时这里只做一次内存里的开关判断就放行，
    不 stat、不读文件、不验签。
    """
    async def _guard(request: Request, call_next):
        state = enforcement_state()
        if state["blocked"] and in_blocked_scope(request.url.path):
            err = expired_error()
            return JSONResponse(
                status_code=err.status,
                content={"detail": {"code": err.code, "params": err.params,
                                    "message": err.message}})
        return await call_next(request)
    return _guard


def require_feature(feature: str):
    """工厂：返回"功能位"依赖——证书里没有该 feature 时 402。

    语义（产品负责人拍板：拦截代码路径必须真实可用，但默认关着）：
    - license.enforce=false（当前默认）→ 直接放行，不读文件、不看证书；
    - 证书 features 缺省/为空 → 放行（老证书不限功能，升级不锁老客户）；
    - features 里没有该功能位 → 402，**不适用宽限期**（宽限期只针对"整份
      授权到期"，功能位是版本/套餐差异，到期与否都按套餐说话）。

    两种用法都支持：
    - 依赖注入：``dependencies=[Depends(require_feature("governance"))]``；
    - 路由内联（按请求体分支时才决定要不要拦，如 mode=answer 的评测）：
      ``require_feature("answer_eval")()``。
    """
    def _check_feature() -> None:
        policy = effective_config()
        if not policy.enforce:
            return
        features = (cached_license() or {}).get("features")
        if features is None:
            return
        if feature not in features:
            raise AppError("error.license_feature_denied",
                           "当前授权未包含该功能：{feature}",
                           status=LICENSE_EXPIRED_STATUS, feature=feature)
    return _check_feature


def feature_enabled(feature: str) -> bool:
    """只读查询：当前是否允许该功能位（诊断/展示用，无副作用）。"""
    policy = effective_config()
    if not policy.enforce:
        return True
    features = (cached_license() or {}).get("features")
    return features is None or feature in features


# --------------------------------------------------------------- 交互式续期

def install_license_text(text: str) -> dict:
    """校验并落盘一份新的 license.json（POST /api/license 的核心）。

    先验签再写：签不过/字段坏的文件不落盘，避免"上传了个坏证书把原本还能
    用的证书覆盖掉"——那是把可恢复的问题变成不可恢复。写入用"临时文件 +
    os.replace"原子替换：并发请求/其他进程读到的永远是完整文件（半截文件
    会被判 invalid）。
    """
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise AppError("error.license_bad_file",
                       "上传的文件不是合法 JSON：{msg}", status=422,
                       msg=str(e)[:200]) from e
    state = parse_license(raw)
    if state is None:
        raise AppError("error.license_bad_file",
                       "许可证校验失败（签名或字段不合法），文件未替换", status=422)
    path = effective_config().path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        raise AppError("error.license_write_failed", "许可证写入失败：{msg}",
                       status=500, msg=str(e)[:200]) from e
    invalidate_license_cache()
    return state
