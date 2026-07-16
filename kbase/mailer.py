"""SMTP 发件箱：页面维护配置 + 发信（找回密码等系统邮件的底座）。

配置存 AppSetting KV（与飞书凭据同规矩）：密码只写不回显；GET 脱敏。
发送兼容两种主流姿势：465 端口走 SMTPS（隐式 TLS），587/25 走
STARTTLS（服务器支持才升级，不支持则明文——内网中继场景）。
网络调用集中在 send_mail，测试打桩它或 smtplib 即可不出网。
"""
import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from kbase.models import AppSetting

logger = logging.getLogger(__name__)

_KEYS = {
    "host": "smtp_host",
    "port": "smtp_port",
    "user": "smtp_user",
    "password": "smtp_password",
    "from_addr": "smtp_from_addr",
    "from_name": "smtp_from_name",
}


def get_settings(sf) -> dict:
    """内部用完整配置（含密码原文）。"""
    with sf() as s:
        out = {}
        for field, key in _KEYS.items():
            row = s.get(AppSetting, key)
            out[field] = row.value if row else None
    return out


def set_settings(sf, *, host: str, port: int, user: str,
                 password: str | None, from_addr: str, from_name: str) -> None:
    """password 为 None = 保留旧密码（编辑表单不回显密码的惯例）。"""
    values = {"host": host, "port": str(port), "user": user,
              "from_addr": from_addr, "from_name": from_name}
    if password is not None:
        values["password"] = password
    with sf() as s:
        for field, value in values.items():
            key = _KEYS[field]
            row = s.get(AppSetting, key)
            if row is None:
                s.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        s.commit()


def status(sf) -> dict:
    """管理页脱敏视图：密码只回配置与否。"""
    cfg = get_settings(sf)
    return {"configured": bool(cfg["host"] and cfg["user"] and cfg["password"]),
            "host": cfg["host"], "port": int(cfg["port"] or 465),
            "user": cfg["user"], "from_addr": cfg["from_addr"],
            "from_name": cfg["from_name"],
            "has_password": bool(cfg["password"])}


def send_mail(sf, to: str, subject: str, body: str) -> None:
    """发一封纯文本邮件。配置缺失/服务器拒绝直接抛异常（调用方转可读信息）。"""
    cfg = get_settings(sf)
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        raise RuntimeError("发件箱未配置（设置 → 系统 → 发件箱）")
    port = int(cfg["port"] or 465)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((cfg["from_name"] or "KBase",
                              cfg["from_addr"] or cfg["user"]))
    msg["To"] = to

    if port == 465:
        server = smtplib.SMTP_SSL(cfg["host"], port, timeout=15)
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=15)
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls()
            server.ehlo()
    try:
        server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["from_addr"] or cfg["user"], [to], msg.as_string())
    finally:
        server.quit()
