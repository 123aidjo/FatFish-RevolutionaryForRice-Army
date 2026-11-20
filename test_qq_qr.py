import asyncio

import pytest

from live_qq import LiveQQ, require_online
from napcat_login import NapCatLogin
from store import Store


@pytest.mark.parametrize("status,stamp", [
    ({"loginPhase": "qrcode_scanned"}, 1),
    ({"loginPhase": "waiting_qrcode", "qrLoginAccepted": True}, 1),
    ({"loginPhase": "waiting_qrcode", "isLogin": True, "coreReady": True}, 1),
    ({"loginPhase": "waiting_qrcode"}, 995),
])
def test_qr_leaves_active_login_and_fresh_codes_alone(monkeypatch, tmp_path, status, stamp):
    monkeypatch.setattr("napcat_login.time.time", lambda: 1000)
    login = NapCatLogin(tmp_path / "webui.json")
    monkeypatch.setattr(login, "login_state", lambda: pytest.fail("不应再次查询"))
    assert login.refresh_expired(status, stamp) == (login.phase(status), False)


def test_qr_renews_expired_code_with_retry_spacing(monkeypatch, tmp_path):
    monkeypatch.setattr("napcat_login.time.time", lambda: 1000)
    monkeypatch.setattr("napcat_login.time.monotonic", lambda: 100)
    login = NapCatLogin(tmp_path / "webui.json")
    status = {"loginPhase": "waiting_qrcode", "loginError": "二维码已过期", "qrcodeurl": "old"}
    requests = []
    monkeypatch.setattr(login, "login_state", lambda: status)
    monkeypatch.setattr(login, "post", lambda route, payload: requests.append(route))
    assert login.refresh_expired(status, 999) == ("refreshing", True)
    assert login.refresh_expired(status, 999) == ("refreshing", True)
    assert requests == ["QQLogin/RefreshQRcode"]


def test_qr_checks_for_concurrent_scan_or_new_code(monkeypatch, tmp_path):
    login = NapCatLogin(tmp_path / "webui.json")
    previous = {"loginPhase": "waiting_qrcode", "qrcodeurl": "old"}
    monkeypatch.setattr(login, "post", lambda *args: pytest.fail("不应替换二维码"))
    monkeypatch.setattr(login, "login_state", lambda: {"loginPhase": "qrcode_scanned"})
    assert login.refresh_expired(previous, 1) == ("qrcode_scanned", True)
    monkeypatch.setattr(login, "login_state", lambda: {"loginPhase": "waiting_qrcode", "qrcodeurl": "new"})
    assert login.refresh_expired(previous, 1) == ("waiting_qrcode", True)


@pytest.mark.parametrize("state,hint", [
    ({"loginPhase": "waiting_qrcode", "loginError": "你的用户身份已失效"}, "QQ 登录身份已失效"),
    ({"loginPhase": "qrcode_scanned"}, "请在手机上确认登录"),
    ({"loginPhase": "qrcode_scanned", "loginError": "账号存在安全风险，请恢复账号使用"}, "QQ 限制了本次登录"),
    ({"qrLoginAccepted": True, "loginError": "账号存在安全风险，请恢复账号使用"}, "正在初始化 OneBot"),
    ({"isLogin": True, "coreReady": True}, "正在重连 OneBot"),
])
def test_connection_error_describes_login_stage(tmp_path, monkeypatch, state, hint):
    runtime = LiveQQ(Store(tmp_path / "fishbot.db"))
    runtime.napcat = True
    monkeypatch.setattr(runtime.napcat_login, "login_state", lambda: state)
    assert hint in asyncio.run(runtime._connection_error(ConnectionError("网络异常")))


def test_onebot_online_check_retries_timeout_but_stops_on_explicit_offline():
    class OneBot:
        def __init__(self, *replies):
            self.replies = iter(replies)

        async def call(self, action, params):
            assert action == "get_status"
            reply = next(self.replies)
            if isinstance(reply, Exception):
                raise reply
            return {"retcode": 0, "data": {"online": reply}}

    asyncio.run(require_online(OneBot(TimeoutError(), True)))
    with pytest.raises(ConnectionError, match="QQ 账号未在线"):
        asyncio.run(require_online(OneBot(False)))
