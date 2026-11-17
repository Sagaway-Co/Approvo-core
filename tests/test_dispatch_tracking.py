"""派发后定位 / 等待 run:找不到不判成功、只认新 run、排队超时取消、排队不吃执行时间、审批单号随部署走。

起因(2026-11-16):GitHub 侧故障,run 建出来 44 分钟后才开始执行。原实现只找 60 秒,找不到就返回成功,
审批卡显示"已部署";run 后来开始时部署凭据兑换券已过期,在任何变更之前就失败了,没人知道。
"""
from datetime import UTC

import pytest

from app import deploycred, events, github, k8s

TITLE = "deploy demo v1.0.0 (prod)"
SPEC = {"repo": "demo", "tag": "v1.0.0", "github": {"owner": "o", "repo": "r", "workflow": "w.yml", "env": "prod"}}


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        return self._data


class _Clock:
    """假时钟:sleep 只推进时间,不真睡。"""

    def __init__(self):
        self.t = 1_800_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture()
def gh(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(github.time, "time", clock.time)
    monkeypatch.setattr(github.time, "sleep", clock.sleep)
    monkeypatch.setattr(github, "_headers", lambda repo: {})
    monkeypatch.setattr(k8s, "deploy_target_allowed", lambda c, n: None)   # 不发券 → 排队上限 QUEUE_MAX_MIN
    state = {"dispatched": False, "runs_before": [], "runs_after": [], "run": {}, "cancelled": [], "clock": clock}

    def fake_post(url, **kw):
        if url.endswith("/dispatches"):
            state["dispatched"] = True
            return _Resp(204)
        if url.endswith("/cancel"):
            state["cancelled"].append(url)
            return _Resp(202)
        raise AssertionError(url)

    def fake_get(url, **kw):
        if url.endswith("/runs") and "workflows" in url:
            runs = state["runs_after"] if state["dispatched"] else state["runs_before"]
            return _Resp(200, {"workflow_runs": runs(clock.t) if callable(runs) else runs})
        for rid, fn in state["run"].items():
            if url.endswith(f"/actions/runs/{rid}"):
                return _Resp(200, fn(clock.t))
        if "/jobs" in url:
            return _Resp(200, {"jobs": []})
        raise AssertionError(url)

    monkeypatch.setattr(github.requests, "post", fake_post)
    monkeypatch.setattr(github.requests, "get", fake_get)
    return state


def _run(rid, title=TITLE, created_at="2027-01-15T08:00:00Z"):
    return {"id": rid, "html_url": f"https://github.com/o/r/actions/runs/{rid}", "display_title": title,
            "created_at": created_at}


def test_run_never_found_is_not_success(gh):
    ok, msg = github.dispatch_and_wait(SPEC)
    assert ok is False, "找不到 run 时绝不能判成功"
    assert "结果未知" in msg and "不按成功处理" in msg


def test_previous_run_with_same_title_is_ignored(gh):
    """同一 tag 重发:旧 run(上次失败 / 成功)同名,只能认派发后新出现的那个。"""
    old = _run(5)
    gh["runs_before"] = [old]
    t_dispatch = gh["clock"].t
    gh["runs_after"] = lambda t: ([_run(7)] if t - t_dispatch > 120 else []) + [old]
    gh["run"][5] = lambda t: {"status": "completed", "conclusion": "success"}
    gh["run"][7] = lambda t: {"status": "completed", "conclusion": "failure"}
    ok, msg = github.dispatch_and_wait(SPEC)
    assert ok is False and "runs/7" in msg, msg


def test_queued_past_limit_is_cancelled_and_failed(gh):
    gh["runs_after"] = [_run(7)]
    gh["run"][7] = lambda t: {"status": "queued"}
    ok, msg = github.dispatch_and_wait(SPEC)
    assert ok is False
    assert gh["cancelled"] and gh["cancelled"][0].endswith("/actions/runs/7/cancel")
    assert "已取消该 run" in msg and "重新发起" in msg


def test_queue_limit_follows_grant_ttl(gh, monkeypatch):
    """发了兑换券:排队上限 = 券的有效期(券过期后开始也拿不到凭据)。"""
    monkeypatch.setattr(k8s, "deploy_target_allowed", lambda c, n: {"cluster": "c", "namespace": "n"})
    monkeypatch.setattr(deploycred, "issue_grant", lambda *a, **k: ("g", 12))
    gh["runs_after"] = [_run(7)]
    t0 = gh["clock"].t
    gh["run"][7] = lambda t: {"status": "queued"}
    ok, msg = github.dispatch_and_wait(SPEC)
    assert ok is False and "排队 12 分钟" in msg
    assert 12 * 60 <= gh["clock"].t - t0 < 12 * 60 + 60


def test_queue_time_does_not_eat_run_timeout(gh):
    """排队 20 分钟后开始、再跑 10 分钟成功:不能因为总时长超过 timeout 判超时。"""
    gh["runs_after"] = [_run(7)]
    t0 = gh["clock"].t

    def run7(t):
        if t - t0 < 20 * 60:
            return {"status": "queued"}
        if t - t0 < 30 * 60:
            return {"status": "in_progress"}
        return {"status": "completed", "conclusion": "success"}

    gh["run"][7] = run7
    ok, msg = github.dispatch_and_wait(SPEC, timeout=900)
    assert ok is True and "success" in msg, msg
    assert not gh["cancelled"]


def test_run_timeout_counts_from_start(gh):
    gh["runs_after"] = [_run(7)]
    gh["run"][7] = lambda t: {"status": "in_progress"}
    ok, msg = github.dispatch_and_wait(SPEC, timeout=900)
    assert ok is False and "超时未完成" in msg


def test_falls_back_to_created_at_when_prelist_fails(gh, monkeypatch):
    """派发前查已有 run 失败:按创建时间认新 run,旧的同名 run 不认。"""
    real_get = github.requests.get

    def flaky_get(url, **kw):
        if url.endswith("/runs") and "workflows" in url and not gh["dispatched"]:
            raise github.requests.exceptions.ConnectionError("boom")
        return real_get(url, **kw)

    monkeypatch.setattr(github.requests, "get", flaky_get)
    from datetime import datetime

    def iso(ts):
        return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    t0 = gh["clock"].t
    gh["runs_after"] = lambda t: [_run(7, created_at=iso(t0 + 70)), _run(5, created_at=iso(t0 - 3600))]
    gh["run"][5] = lambda t: {"status": "completed", "conclusion": "success"}
    gh["run"][7] = lambda t: {"status": "completed", "conclusion": "failure"}
    ok, msg = github.dispatch_and_wait(SPEC)
    assert ok is False and "runs/7" in msg, msg


def test_instance_code_travels_with_deploy(monkeypatch):
    """审批单号要进部署 spec(→ 兑换券 → 流水线 → 迁移台账 approval_ref),原来只有 "-"。"""
    seen = {}

    class _Dep:
        def deploy(self, spec):
            seen.update(spec)
            return True, "ok"

    monkeypatch.setattr(events, "get_deployer", lambda method: _Dep())
    monkeypatch.setattr(events, "_notify_deploying", lambda *a, **k: None)
    monkeypatch.setattr(events, "_notify_result", lambda *a, **k: None)
    monkeypatch.setattr(events.store, "set_status", lambda *a, **k: None)
    spec = {"repo": "demo", "tag": "v1.0.0", "method": "github"}
    events._run_deploy("slk-abc", spec, "Bob")
    assert seen["instance_code"] == "slk-abc"
    assert "instance_code" not in spec, "不改调用方的 spec(审批卡还在用)"
