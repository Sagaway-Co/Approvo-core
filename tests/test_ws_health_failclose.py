"""长连接(WS)健康判据 + 审批链路 fail-close + 启动对账上限。

背景:用一个无效的 app_id 启动 → lark.ws.Client.start() 在连接阶段直接 raise
→ 未捕获异常把整个 WS 线程带走,且没有重试。而 uvicorn 在主线程毫发无伤:
容器 ready=true / restarts=0 / /healthz={"ok":true}。

于是形成静默失效:卡片能创建、人能点通过,但审批事件永远收不到 → 什么都不部署,
也没有任何告警;CI 侧 fail-close 只看 POST /release 是否 2xx,而那时 HTTP 是好的。

本文件守四条红线:
  ① healthy() 是 fail-close 的 —— 未知/刚启动/崩溃一律判不健康
  ② /healthz 即使链路挂了也必须 200(它是 liveness,红了会让实例被摘、无法诊断)
  ③ WS 不健康时 /release 必须 503 拒绝 —— 但【直通模式例外】(那条路不依赖 WS)
  ④ 启动对账有上限,且被跳过的条数必须显式告警(不许换成静默不对账)
"""
import time

import pytest
from fastapi import HTTPException

from app import events, server, settings, store, wshealth


@pytest.fixture(autouse=True)
def _reset_ws():
    """每个用例前把 WS 状态复位,避免用例间串味。"""
    wshealth._st.update({"state": "init", "since": 0.0, "attempts": 0,
                         "crashes": 0, "last_error": "", "last_error_at": 0.0})
    yield


def _make_running(age_sec: float):
    """把 WS 置为 running 且已持续 age_sec 秒。"""
    wshealth.mark_starting()
    wshealth.mark_running()
    wshealth._st["since"] = time.time() - age_sec


# ── ① healthy() 的 fail-close 语义 ────────────────────────────────────
def test_healthy_is_failclose_for_every_non_running_state():
    """init / starting / crashed 一律不健康。判据宁可保守,不许把"连不上"判成健康。"""
    assert wshealth.healthy() is False            # init:进程刚起,还没连
    wshealth.mark_starting()
    assert wshealth.healthy() is False            # starting:正在连,未确认
    wshealth.mark_crashed("RuntimeError: bad app_id")
    assert wshealth.healthy() is False            # crashed:明确坏了


def test_running_needs_grace_period():
    """🔴 running 必须【维持 GRACE_SEC】才算健康。

    无效凭证会在几百毫秒内 raise —— 若 running 一置上就算健康,那种情况会被
    判成正常,正是本次要修的 bug。
    """
    _make_running(age_sec=0)
    assert wshealth.healthy() is False, "刚进入 running 不能算健康"
    _make_running(age_sec=wshealth.GRACE_SEC - 1)
    assert wshealth.healthy() is False, "grace 未过不能算健康"
    _make_running(age_sec=wshealth.GRACE_SEC + 1)
    assert wshealth.healthy() is True


def test_snapshot_records_crash_and_leaks_no_credentials():
    """快照要能归因(次数/错误/年龄),且绝不含凭据。"""
    wshealth.mark_starting()
    wshealth.mark_crashed("AuthError: invalid app_id")
    s = wshealth.snapshot()
    assert s["state"] == "crashed" and s["healthy"] is False
    assert s["crashes"] == 1 and s["attempts"] == 1
    assert "invalid app_id" in s["last_error"]
    assert s["last_error_age_sec"] is not None
    # 快照会被 /healthz 公开返回 —— 只允许这些键,防止日后有人往里塞敏感字段
    assert set(s) == {"state", "healthy", "in_state_sec", "attempts",
                      "crashes", "last_error", "last_error_age_sec"}


def test_long_error_is_truncated():
    """错误文本可能极长(带 traceback/响应体),截断避免把日志和响应撑爆。"""
    wshealth.mark_crashed("E" * 5000)
    assert len(wshealth.snapshot()["last_error"]) <= 300


# ── ② /healthz 是 liveness,不许因链路挂而变红 ──────────────────────────
def test_healthz_stays_200_even_when_ws_dead():
    """🔴 /healthz red 会让编排层摘掉实例 → 连诊断都够不到,CI 只看到"连不上"。
    故它恒 200,但必须把真实状态如实带出来(不再是硬编码 {"ok":true})。
    """
    wshealth.mark_crashed("boom")
    out = server.healthz()
    assert out["ok"] is True                       # liveness 语义不变
    assert out["ws"]["state"] == "crashed"         # 但真相要可见
    assert out["ws"]["healthy"] is False


def test_readyz_503_when_ws_dead_and_no_bypass(monkeypatch):
    """能力探针:链路挂 + 无直通 → 503。"""
    monkeypatch.setattr(settings, "approval_bypass_on", lambda: False)
    wshealth.mark_crashed("boom")
    with pytest.raises(HTTPException) as e:
        server.readyz()
    assert e.value.status_code == 503


def test_readyz_ok_but_still_reports_degraded_under_bypass(monkeypatch):
    """直通模式下发版不依赖 WS → ok=True;但仍要如实报告链路是坏的,
    否则"开着 bypass 时 WS 挂了"会被永久掩盖。"""
    monkeypatch.setattr(settings, "approval_bypass_on", lambda: True)
    wshealth.mark_crashed("boom")
    out = server.readyz()
    assert out["ok"] is True
    assert out["ws"]["healthy"] is False           # 不粉饰
    assert "仍是坏的" in out["detail"]


# ── ③ /release 的审批链路 fail-close ──────────────────────────────────
_REQ = {"repo": "demo", "tag": "V9.9.9-pre", "stage": "pre"}


def _prep_release(monkeypatch, bypass: bool):
    """让 /release 能跑到 WS 检查那一步:放开 token、限流、防重。

    🔴 刻意【自带 RELEASES】而不依赖 config.example.yaml —— 那个文件的内容会变,
    依赖它会让本测试在示例配置调整时莫名 404。
    """
    monkeypatch.setattr(settings, "RELEASE_TOKEN", "")          # 不校验 token
    monkeypatch.setattr(settings, "approval_bypass_on", lambda: bypass)
    monkeypatch.setattr(settings, "RELEASES", {
        "demo": {"project": "T", "method": "kubectl", "image_repo": "registry.test/demo"}})
    monkeypatch.setattr(server, "_rate_ok", lambda *a, **k: True)
    monkeypatch.setattr(store, "status_history", lambda *a, **k: [])
    monkeypatch.setattr(store, "usermap_get_row", lambda *a, **k: None)
    monkeypatch.setattr(settings, "DEFAULT_INITIATOR", "u1")


def test_release_rejects_503_when_ws_dead(monkeypatch):
    """🔴 核心用例:链路挂时受理请求 = 静默积压。
    CI 拿到 2xx 以为进了流程、卡片也正常出现、人点完通过就走了 ——
    没有任何一环报错,直到有人发现某个版本压根没上线。必须当场 503。
    """
    _prep_release(monkeypatch, bypass=False)
    wshealth.mark_crashed("ConnectionError: ws connect failed")

    called = []
    monkeypatch.setattr(server.feishu, "get_chat_members",
                        lambda *a, **k: called.append("members") or ["u1"])
    monkeypatch.setattr(server.feishu, "create_instance",
                        lambda *a, **k: called.append("create") or "ins-1")

    with pytest.raises(HTTPException) as e:
        server.release(server.ReleaseReq(**_REQ), x_release_token="")

    assert e.value.status_code == 503
    assert "审批链路不可用" in e.value.detail
    assert called == [], "拒绝必须发生在【建审批实例之前】,不能留下半个实例"


class _Sentinel(Exception):
    """哨兵:证明执行【越过了】WS 检查,而不用把整条审批流程都跑完。"""


def test_release_proceeds_when_ws_healthy(monkeypatch):
    """链路正常 → 不因新增检查而误拒。

    ⚖️ 用哨兵异常而不是跑完整流程:走完 create_instance 会牵扯 form 构造、
    卡片渲染、approval_code 解析等一大串与本次改动无关的依赖,那样的测试
    很脆、坏了也说明不了问题。这里只断言"控制流到达了 WS 检查之后"。
    """
    _prep_release(monkeypatch, bypass=False)
    _make_running(age_sec=wshealth.GRACE_SEC + 1)
    monkeypatch.setattr(server.feishu, "get_chat_members",
                        lambda *a, **k: (_ for _ in ()).throw(_Sentinel()))

    with pytest.raises(_Sentinel):
        server.release(server.ReleaseReq(**_REQ), x_release_token="")


def test_bypass_path_is_not_blocked_by_dead_ws(monkeypatch):
    """⚖️ 直通模式不依赖长连接 —— 否则 IM 出问题时连应急通道也一起堵死。
    检查必须在 bypass 分支【之后】。
    """
    _prep_release(monkeypatch, bypass=True)
    wshealth.mark_crashed("ws dead")
    monkeypatch.setattr(server, "_bypass_release",
                        lambda code, spec, env: {"bypass": "qa-auto-approved"})

    out = server.release(server.ReleaseReq(**_REQ), x_release_token="")
    assert out["bypass"] == "qa-auto-approved", "WS 挂不该堵死应急通道"


# ── ④ 启动对账的上限与可见性 ──────────────────────────────────────────
def test_reconcile_caps_batch_and_warns_about_skipped(monkeypatch, capsys):
    """🔴 有上限,且被跳过的条数必须【显式告警】。

    原实现无上限:积压多少就一次性打出去多少(每条至少一次 IM 调用),
    一次重启就能突发上百次 —— 而月额度正是被高频调用吃光的。
    但只加上限而不喊出来,等于把"突发打爆额度"换成"静默不对账",
    是同一类错误(判据落在不反映真实情况的指标上)的又一次复发。
    """
    n = events.RECONCILE_MAX + 7
    monkeypatch.setattr(store, "list_pending", lambda: [f"ins-{i}" for i in range(n)])
    monkeypatch.setattr(events, "RECONCILE_GAP_SEC", 0)         # 测试里不真 sleep
    done = []
    monkeypatch.setattr(events, "process_instance", lambda ic: done.append(ic))

    events.reconcile_pending_once()

    assert len(done) == events.RECONCILE_MAX, "必须按上限截断"
    out = capsys.readouterr().out
    assert "[reconcile][warn]" in out, "被跳过必须告警,不能静默"
    assert "7" in out, "告警要说清还剩多少条"


def test_reconcile_no_warn_when_within_cap(monkeypatch, capsys):
    """未超上限时不该发告警(避免制造噪音 —— 噪音久了就没人看了)。"""
    monkeypatch.setattr(store, "list_pending", lambda: ["a", "b"])
    monkeypatch.setattr(events, "RECONCILE_GAP_SEC", 0)
    monkeypatch.setattr(events, "process_instance", lambda ic: None)

    events.reconcile_pending_once()
    assert "[reconcile][warn]" not in capsys.readouterr().out
