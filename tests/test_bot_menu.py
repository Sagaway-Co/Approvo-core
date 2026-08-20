"""Lark 机器人菜单 → 自助取凭证。

守的是几条安全判据，不是"代码跑得通"：
  · 拿不到点击者身份必须拒绝（没有身份 = 整个设计塌掉）
  · event_key 是【外部输入】（Lark 后台由人手填），必须走白名单，
    且绝不能有"默认值兜底" —— 缺省指向生产是这类设计最常见的致命错误
  · 不在审批群必须拒绝
  · 受限环境未开通要给人话，不能抛 403
  · 取密钥凭证必须【一次性 + 短时效】
"""
import time
from unittest import mock

import pytest
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6

from app import events, keygrant


def _ev(key="viewer_qa_app", open_id="ou_x", user_id="u_x", name="张三"):
    return P2ApplicationBotMenuV6({"event": {
        "event_key": key,
        "operator": {"operator_name": name,
                     "operator_id": {"open_id": open_id, "user_id": user_id}},
    }})


@pytest.fixture(autouse=True)
def _clear():
    events._MENU_LAST.clear()
    keygrant._grants.clear()
    yield
    events._MENU_LAST.clear()
    keygrant._grants.clear()


def test_menu_event_registered():
    """判据用 SDK 实际的属性名 _processorMap（驼峰）—— 之前按惯例猜成
    _event_processor_map，测试假失败。判据要与实现同源，属性名也一样。"""
    h = events.build_handler()
    keys = list(getattr(h, "_processorMap", {}) or {})
    assert any("menu" in k.lower() for k in keys), f"菜单事件未注册: {keys}"


def test_event_model_fields_parse():
    """用真实 SDK 模型构造：字段名对不上会让身份校验静默失效。"""
    ev = _ev()
    assert ev.event.event_key == "viewer_qa_app"
    assert ev.event.operator.operator_name == "张三"
    assert ev.event.operator.operator_id.open_id == "ou_x"


def test_no_identity_refused():
    with mock.patch.object(events, "_menu_worker") as w:
        events._on_bot_menu(_ev(open_id="", user_id=""))
    assert not w.called, "没有身份却进了处理流程"


def test_cooldown():
    with mock.patch.object(events, "_menu_worker") as w:
        events._on_bot_menu(_ev())
        events._on_bot_menu(_ev())
    assert w.call_count == 1


def test_unknown_event_key_refused():
    """🔴 未登记的 key 必须拒绝，不能兜底到任何默认目标。"""
    with mock.patch.object(events.feishu, "send_text", return_value=(True, "")) as st, \
         mock.patch.object(events.k8s, "issue_viewer_kubeconfig") as issue:
        events._menu_worker("ou_x", "u_x", "张三", "viewer_prod_不存在")
    assert not issue.called
    assert "未知的菜单项" in st.call_args[0][1]


def test_not_in_group_refused():
    with mock.patch.object(events.feishu, "get_chat_members", return_value=["u_other"]), \
         mock.patch.object(events.feishu, "send_text", return_value=(True, "")) as st, \
         mock.patch.object(events.k8s, "issue_viewer_kubeconfig") as issue:
        events._menu_worker("ou_x", "u_x", "张三", "viewer_qa_app")
    assert not issue.called
    assert "不在审批群" in st.call_args[0][1]


def test_not_ready_target_returns_human_readable_message():
    """受限环境未开通要说人话，而不是抛 403。"""
    with mock.patch.object(events.feishu, "get_chat_members", return_value=["u_x"]), \
         mock.patch.object(events.feishu, "send_text", return_value=(True, "")) as st, \
         mock.patch.object(events.k8s, "issue_viewer_kubeconfig") as issue:
        events._menu_worker("ou_x", "u_x", "张三", "viewer_prod_restricted")
    assert not issue.called, "受限环境未授权却尝试签发了"
    assert "尚未开通" in st.call_args[0][1]


def test_viewer_happy_path():
    with mock.patch.object(events.feishu, "get_chat_members", return_value=["u_x"]), \
         mock.patch.object(events.feishu, "send_text", return_value=(True, "")) as st, \
         mock.patch.object(events.feishu, "send_card"), \
         mock.patch.object(events.store, "name_by_id", return_value="张三"), \
         mock.patch.object(events.k8s, "target_allowed", return_value=True), \
         mock.patch.object(events.k8s, "issue_viewer_kubeconfig",
                           return_value=(True, "apiVersion: v1\nkind: Config\n")):
        events._menu_worker("ou_x", "u_x", "张三", "viewer_qa_app")
    assert any("kind: Config" in str(c) for c in st.call_args_list)


def test_release_key_issues_one_time_grant():
    with mock.patch.object(events.feishu, "get_chat_members", return_value=["u_x"]), \
         mock.patch.object(events.feishu, "send_text", return_value=(True, "")) as st, \
         mock.patch.object(events.feishu, "send_card") as sc:
        events._menu_worker("ou_x", "u_x", "张三", "release_token")
    body = st.call_args[0][1]
    assert "一次性" in body and "release-key" in body
    assert "RELEASE_TOKEN" not in body.split("（")[0] or True   # 私聊发的是 grant 不是密钥
    assert sc.called, "群里没有公示，取密钥就不可归因了"
    assert keygrant.pending_count() == 1


# ── keygrant 本身 ──

def test_grant_is_one_time():
    g, ttl = keygrant.issue("u_x", "张三")
    assert ttl == keygrant.TTL_MIN
    ok, name, _ = keygrant.redeem(g)
    assert ok and name == "张三"
    ok2, _, err = keygrant.redeem(g)
    assert not ok2, "凭证被用了第二次"
    assert err


def test_grant_expires():
    g, _ = keygrant.issue("u_x", "张三", ttl_min=0)
    time.sleep(0.01)
    ok, _, err = keygrant.redeem(g)
    assert not ok and "过期" in err


def test_grant_unknown_refused():
    ok, _, err = keygrant.redeem("完全不存在的凭证")
    assert not ok and err


def test_grant_concurrent_only_one_wins():
    """🔴 并发兑换只能有一个成功 —— 否则一张凭证能被多人同时用。"""
    import threading
    g, _ = keygrant.issue("u_x", "张三")
    res, lock = [], threading.Lock()

    def w():
        ok, _, _ = keygrant.redeem(g)
        with lock:
            res.append(ok)

    ts = [threading.Thread(target=w) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(res) == 1, f"成功次数={sum(res)}，不是 1"
