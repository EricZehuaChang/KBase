"""品牌邮件模板：三款模板的 (subject, text, html) 契约、动态值转义、
send_mail 的 multipart/alternative 组包（HTML 优先+纯文本兜底）。"""
from kbase import email_templates


def test_account_created_template():
    subject, text, html = email_templates.account_created(
        "wang.wu", "Init@123", "http://kb.example:5000")
    assert subject == "KBase 账号已开通"
    # 纯文本兜底：凭据与地址齐全（老客户端唯一能看到的版本）
    assert "wang.wu" in text and "Init@123" in text
    assert "http://kb.example:5000" in text
    # HTML：品牌色、字标、按钮链接、凭据卡片
    assert email_templates.ACCENT in html and "KBase" in html
    assert 'href="http://kb.example:5000"' in html
    assert "Init@123" in html and "wang.wu" in html


def test_password_reset_template_and_escaping():
    url = "http://kb.example:5000/?reset_token=abc-DEF_123"
    subject, text, html = email_templates.password_reset("<b>evil</b>", url)
    assert subject == "KBase 密码重置"
    assert url in text and f'href="{url}"' in html
    # 动态值必须转义——用户名可被注册者控制，不转义=邮件 HTML 注入
    assert "<b>evil</b>" not in html and "&lt;b&gt;evil&lt;/b&gt;" in html
    assert "30 分钟" in text and "30 分钟" in html


def test_smtp_test_template():
    subject, text, html = email_templates.smtp_test()
    assert subject == "KBase 发件箱测试"
    assert "测试邮件" in text and email_templates.ACCENT in html


def test_send_mail_multipart(tmp_path, monkeypatch):
    """html 给了 → multipart/alternative（plain 兜底在前 + html 在后）；
    不给 → 维持纯文本单部件。"""
    import smtplib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from kbase import mailer
    from kbase.models import Base
    from tests.test_mailer import FakeSMTP

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []

    engine = create_engine(f"sqlite:///{tmp_path / 'mail.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    mailer.set_settings(sf, host="smtp.test", port=465, user="n@test",
                        password="pw", from_addr="n@test", from_name="KBase")

    mailer.send_mail(sf, "a@b.c", "主题", "纯文本", html="<html>品牌版</html>")
    msg = FakeSMTP.sent[-1]["msg"]
    assert "multipart/alternative" in msg
    assert "text/plain" in msg and "text/html" in msg
    # alternative 语义：排后者优先渲染——html 必须在 plain 之后
    assert msg.index("text/plain") < msg.index("text/html")

    mailer.send_mail(sf, "a@b.c", "主题", "只有纯文本")
    assert "multipart" not in FakeSMTP.sent[-1]["msg"]
