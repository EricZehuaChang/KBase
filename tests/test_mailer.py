"""发件箱（SMTP）：配置 CRUD 密码脱敏、测试邮件端点、建号自动发账号
通知（填邮箱+已配发件箱才发；发信失败不阻塞建号）。smtplib 全打桩。"""
import pytest
from fastapi.testclient import TestClient

import kbase.mailer as mailer
from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


class FakeSMTP:
    """记录调用的假 SMTP 服务器（SSL/明文同一形状）。"""
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def ehlo(self):
        pass

    def has_extn(self, name):
        return True

    def starttls(self):
        pass

    def login(self, user, password):
        self.user = user

    def sendmail(self, from_addr, to_addrs, msg):
        FakeSMTP.sent.append({"from": from_addr, "to": to_addrs, "msg": msg})

    def quit(self):
        pass


@pytest.fixture
def client(tmp_path, fake_embedder, monkeypatch):
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    FakeSMTP.sent = []
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


SMTP_BODY = {"host": "smtp.163.com", "port": 465, "user": "notify@163.com",
             "password": "authcode123", "from_addr": "notify@163.com",
             "from_name": "RPA运营小助手"}


def test_smtp_settings_and_masking(client):
    assert client.get("/api/settings/smtp").json()["configured"] is False

    client.put("/api/settings/smtp", json=SMTP_BODY)
    st = client.get("/api/settings/smtp").json()
    assert st["configured"] is True and st["host"] == "smtp.163.com"
    assert st["has_password"] is True
    assert "authcode" not in str(st)               # 密码永不出站

    # 编辑不带密码=保留旧密码
    client.put("/api/settings/smtp", json={**SMTP_BODY, "password": None,
                                           "from_name": "通知中心"})
    st = client.get("/api/settings/smtp").json()
    assert st["from_name"] == "通知中心" and st["has_password"] is True


def test_smtp_test_endpoint(client):
    client.put("/api/settings/smtp", json=SMTP_BODY)
    r = client.post("/api/settings/smtp/test", json={"to": "me@corp.example"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert FakeSMTP.sent and FakeSMTP.sent[-1]["to"] == ["me@corp.example"]


def test_create_user_sends_notification(client):
    client.put("/api/settings/smtp", json=SMTP_BODY)
    r = client.post("/api/users", json={
        "username": "wang.wu", "role": "viewer", "password": "init123456",
        "email": "wang.wu@corp.example"})
    assert r.status_code == 200
    # TestClient 的 BackgroundTasks 同步执行完才返回
    assert FakeSMTP.sent, "建号应触发账号通知邮件"
    mail = FakeSMTP.sent[-1]
    assert mail["to"] == ["wang.wu@corp.example"]
    assert "wang.wu" in mail["msg"]

    # 未填邮箱不发
    before = len(FakeSMTP.sent)
    client.post("/api/users", json={"username": "no.mail", "role": "viewer",
                                    "password": "init123456"})
    assert len(FakeSMTP.sent) == before
