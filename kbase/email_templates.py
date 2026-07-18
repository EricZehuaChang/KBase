"""KBase 品牌邮件模板：账号开通 / 密码重置 / 发件箱测试。

每个模板函数返回 (subject, text, html) 三元组——text 是纯文本兜底（老客户
端/纯文本偏好），html 是品牌版式。HTML 只用 table 布局 + 内联样式：邮件
客户端（Outlook/网易/QQ 等）对 flex/外链 CSS 支持参差，这是唯一可移植写法。
品牌色与前端 tokens.css 对齐（主紫 #534AB7）。所有动态值经 html.escape。
"""
import html as _html

# 品牌色（与 web-app/src/styles/tokens.css 亮色主题对齐）
ACCENT = "#534AB7"
ACCENT_DARK = "#3C3489"
ACCENT_WEAK = "#EEEDF9"
TEXT = "#17171C"
TEXT_2 = "#4C4C55"
TEXT_3 = "#8A8A94"
PAGE_BG = "#F1F1F4"
CARD_BG = "#FFFFFF"
FIELD_BG = "#F6F6F9"

_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
         "'Hiragino Sans GB','Microsoft YaHei',sans-serif")
_MONO = "'SF Mono',Consolas,'Courier New',monospace"


def _esc(v: str) -> str:
    return _html.escape(str(v), quote=True)


def _button(label: str, url: str) -> str:
    """防弹按钮：a 标签块级+内联样式（不依赖 VML，Outlook 降级为可点色块）。"""
    return f"""
      <tr><td align="center" style="padding:8px 0 24px;">
        <a href="{_esc(url)}" target="_blank"
           style="display:inline-block;background:{ACCENT};color:#FFFFFF;
                  font-family:{_FONT};font-size:15px;font-weight:600;
                  text-decoration:none;padding:12px 36px;border-radius:8px;">
          {_esc(label)}</a>
      </td></tr>"""


def _fields(rows: list[tuple[str, str]]) -> str:
    """凭据/信息卡片：浅灰圆角块，label 小字灰、value 等宽突出。"""
    cells = "".join(f"""
        <tr>
          <td style="padding:7px 0;font-family:{_FONT};font-size:12px;
                     color:{TEXT_3};white-space:nowrap;vertical-align:top;
                     padding-right:20px;">{_esc(label)}</td>
          <td style="padding:7px 0;font-family:{_MONO};font-size:14px;
                     color:{TEXT};word-break:break-all;">{_esc(value)}</td>
        </tr>""" for label, value in rows)
    return f"""
      <tr><td style="padding:0 0 20px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="background:{FIELD_BG};border-radius:10px;">
          <tr><td style="padding:14px 20px;">
            <table role="presentation" cellpadding="0" cellspacing="0">{cells}
            </table>
          </td></tr>
        </table>
      </td></tr>"""


def _note(text: str) -> str:
    """提示条：浅紫底 + 左侧品牌色竖线。"""
    return f"""
      <tr><td style="padding:0 0 20px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td width="3" style="background:{ACCENT};border-radius:2px;"></td>
            <td style="background:{ACCENT_WEAK};padding:10px 14px;
                       font-family:{_FONT};font-size:13px;line-height:1.6;
                       color:{ACCENT_DARK};border-radius:0 8px 8px 0;">
              {_esc(text)}</td>
          </tr>
        </table>
      </td></tr>"""


def _link_fallback(url: str) -> str:
    """按钮点不了时的裸链接兜底（部分客户端禁用按钮样式）。"""
    return f"""
      <tr><td style="padding:0 0 20px;font-family:{_FONT};font-size:12px;
                     line-height:1.6;color:{TEXT_3};">
        按钮无法点击？复制以下链接到浏览器打开：<br>
        <a href="{_esc(url)}" target="_blank"
           style="color:{ACCENT};word-break:break-all;">{_esc(url)}</a>
      </td></tr>"""


def _render(*, title: str, paragraphs: list[str], blocks: str = "") -> str:
    """基础版式：品牌头带（KBase 字标）+ 白卡内容 + 页脚。blocks 为拼好的
    按钮/卡片/提示 HTML 片段，插在段落之后。"""
    paras = "".join(f"""
      <tr><td style="padding:0 0 16px;font-family:{_FONT};font-size:14px;
                     line-height:1.75;color:{TEXT_2};">{p}</td></tr>"""
                    for p in paragraphs)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{PAGE_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:{PAGE_BG};">
  <tr><td align="center" style="padding:32px 16px;">
    <table role="presentation" width="560" cellpadding="0" cellspacing="0"
           style="max-width:560px;width:100%;">
      <!-- 品牌头带 -->
      <tr><td style="background:{ACCENT};border-radius:12px 12px 0 0;
                     padding:20px 32px;">
        <table role="presentation" cellpadding="0" cellspacing="0">
          <tr>
            <td style="background:#FFFFFF;border-radius:8px;width:34px;
                       height:34px;text-align:center;vertical-align:middle;
                       font-family:{_FONT};font-size:18px;font-weight:800;
                       color:{ACCENT};">K</td>
            <td style="padding-left:12px;font-family:{_FONT};font-size:19px;
                       font-weight:700;color:#FFFFFF;letter-spacing:.3px;">
              KBase<span style="font-size:12px;font-weight:400;
                     color:#CECBF6;padding-left:10px;">企业知识库</span></td>
          </tr>
        </table>
      </td></tr>
      <!-- 内容卡 -->
      <tr><td style="background:{CARD_BG};border-radius:0 0 12px 12px;
                     padding:32px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="padding:0 0 18px;font-family:{_FONT};font-size:20px;
                         font-weight:700;color:{TEXT};">{_esc(title)}</td></tr>
          {paras}
          {blocks}
        </table>
      </td></tr>
      <!-- 页脚 -->
      <tr><td align="center" style="padding:20px 12px 0;font-family:{_FONT};
                     font-size:12px;line-height:1.7;color:{TEXT_3};">
        此邮件由 KBase 系统自动发送，请勿直接回复<br>
        <span style="color:#B9B9C2;">KBase · 私有化部署企业知识库</span>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def account_created(username: str, password: str, login_url: str
                    ) -> tuple[str, str, str]:
    """账号开通通知：凭据卡片 + 登录按钮 + 首登改密提示。"""
    subject = "KBase 账号已开通"
    text = (f"您的 KBase 知识库账号已创建：\n\n"
            f"登录地址：{login_url}\n用户名：{username}\n"
            f"初始密码：{password}\n\n"
            f"首次登录后请点击顶栏钥匙图标修改密码。")
    html = _render(
        title="账号已开通",
        paragraphs=[f"你好，<b>{_esc(username)}</b>。管理员为你开通了 "
                    f"KBase 知识库账号，以下是初始登录凭据："],
        blocks=(_fields([("用户名", username), ("初始密码", password)])
                + _button("登录 KBase", login_url)
                + _note("为了账号安全，请在首次登录后点击顶栏钥匙图标修改密码，"
                        "并按提示绑定邮箱（用于忘记密码时找回）。")
                + _link_fallback(login_url)))
    return subject, text, html


def password_reset(username: str, reset_url: str) -> tuple[str, str, str]:
    """密码重置：重置按钮 + 30 分钟一次性提示 + 非本人操作免责。"""
    subject = "KBase 密码重置"
    text = (f"你（或他人）请求重置 KBase 账号 {username} 的密码。\n\n"
            f"请在 30 分钟内打开以下链接设置新密码：\n{reset_url}\n\n"
            f"如果这不是你本人的操作，请忽略本邮件，你的密码不会有任何变化。")
    html = _render(
        title="重置你的密码",
        paragraphs=[f"我们收到了重置账号 <b>{_esc(username)}</b> "
                    f"密码的请求。点击下方按钮设置新密码："],
        blocks=(_button("设置新密码", reset_url)
                + _note("链接 30 分钟内有效，且仅可使用一次。")
                + _link_fallback(reset_url)
                + f"""
      <tr><td style="padding:0;font-family:{_FONT};font-size:13px;
                     line-height:1.7;color:{TEXT_3};">
        如果这不是你本人的操作，请忽略本邮件，你的密码不会有任何变化。
      </td></tr>"""))
    return subject, text, html


def smtp_test() -> tuple[str, str, str]:
    """发件箱连通测试：收到即配置正确。"""
    subject = "KBase 发件箱测试"
    text = "这是一封来自 KBase 的测试邮件。收到即说明发件箱配置正确。"
    html = _render(
        title="发件箱配置成功 🎉",
        paragraphs=["这是一封来自 KBase 的测试邮件。你能看到这封邮件，"
                    "说明发件箱（SMTP）配置正确，账号通知与密码重置等系统邮件"
                    "均可正常送达。"],
        blocks=_note("本邮件仅用于验证发件配置，无需任何操作。"))
    return subject, text, html
