"""四处 fail-close 加固的回归守卫(为"给第二个团队/环境接入"而做)。

守的都是"不许静默走到生产"这一类判据：
  · 派发时 env 不得有默认值（原实现默认 "prod"）
  · 声明了 stages 却送来未登记的 stage，必须拒绝，不得回落顶层配置
  · 只读 SA 名必须能按目标配置（否则新 namespace 里会出现别的环境的账号名）
  · 菜单目标必须能由配置驱动（否则开通新环境要改代码）
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import github, k8s, server


@pytest.fixture(autouse=True)
def _clear_buckets():
    """每个用例前后清空限流桶 —— 否则用例之间会互相把额度吃掉（顺序依赖）。"""
    server._rl_buckets.clear()
    yield
    server._rl_buckets.clear()


# ---------- ① 派发时 env 不得默认到生产 ----------

def test_dispatch_refuses_when_env_missing():
    """github.env 缺失时必须拒绝派发，且【不发起任何网络请求】。

    原实现是 g.get("env", "prod")：漏写 env 就静默打到生产。
    """
    ok, msg = github.dispatch_and_wait({
        "repo": "demo", "tag": "v1.0.0",
        "github": {"owner": "o", "repo": "r", "workflow": "w.yml"},
    })
    assert ok is False, "缺 env 却允许派发"
    assert "github.env" in msg, f"错误信息应指明缺什么：{msg}"


def _code_only(path: str) -> str:
    """只保留代码行，剔掉整行注释。

    🔴 为什么必须这么做：文本断言若连注释一起看，那么"在注释里解释被禁止的旧写法"
    就会触发它自己 —— 守卫把自己的说明文字当成违规,是这类断言最常见的失效方式。
    ⇒ 守卫应当只检查【代码】；注释要能自由引用坏写法，那正是它的文档价值所在。
    """
    src = (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")
    return "\n".join(ln for ln in src.split("\n") if not ln.lstrip().startswith("#"))


def test_dispatch_env_is_never_defaulted_in_source():
    """文本断言：不允许有人把 prod 默认值加回代码里（注释里引用它是允许的）。"""
    code = _code_only("app/github.py")
    assert 'g.get("env", "prod")' not in code, "又出现了默认到 prod 的写法"


# ---------- ② 未登记的 stage 必须拒绝 ----------

def _fake_release():
    return {
        "method": "github", "project": "T",
        "github": {"owner": "o", "repo": "r", "workflow": "w.yml", "env": "qa"},
        "stages": {"pre": {"env": "qa", "github": {"env": "qa"}}},
    }


def test_unregistered_stage_is_rejected(monkeypatch):
    """只登记了 pre 的应用，收到 stage=release 必须 400 —— 不得按顶层配置兜底。"""
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "t")
    monkeypatch.setattr(server.settings, "RELEASES", {"only-pre": _fake_release()})
    r = TestClient(server.app).post(
        "/release",
        json={"repo": "only-pre", "tag": "V1.0.0.1-release", "stage": "release"},
        headers={"X-Release-Token": "t"})
    assert r.status_code == 400, f"未登记的 stage 却没被拒：{r.status_code} {r.text[:200]}"
    assert "stages" in r.text


def test_unknown_repo_still_404(monkeypatch):
    """回归：未登记的应用仍应 404，不因新增 stage 校验而改变。"""
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "t")
    monkeypatch.setattr(server.settings, "RELEASES", {})
    r = TestClient(server.app).post(
        "/release", json={"repo": "nope", "tag": "V1.0.0.1-pre", "stage": "pre"},
        headers={"X-Release-Token": "t"})
    assert r.status_code == 404


# ---------- ③ 只读 SA 名按目标可配 ----------

def test_viewer_sa_configurable(monkeypatch):
    monkeypatch.setattr(server.settings, "VIEWER_SA_DEFAULT", "approvo-viewer")
    monkeypatch.setattr(server.settings, "VIEWER_TARGETS", [
        {"cluster": "another-prod", "namespace": "another-ns", "sa": "another-viewer"},
        {"cluster": "prod-cluster", "namespace": "prod-namespace"},
    ])
    assert k8s.viewer_sa_for("another-prod", "another-ns") == "another-viewer", \
        "没有按目标取到配置的 SA"
    # 没写 sa 的目标 → 回落到【显式配置的】全局兜底(不是硬编码的猜测)
    assert k8s.viewer_sa_for("prod-cluster", "prod-namespace") == "approvo-viewer"
    # 🔴 未登记的组合必须抛错。曾经这里对任何输入都返回同一个 SA 名 ——
    #    而把那个行为写进断言,就等于把缺陷固化成契约。
    with pytest.raises(ValueError, match="未登记"):
        k8s.viewer_sa_for("unknown", "x")


# ---------- ④ 菜单目标可由配置驱动 ----------

def test_menu_targets_present_and_whitelisted():
    """无论来自配置还是内置表，都必须是非空白名单（缺省绝不兜底）。"""
    from app import events
    assert isinstance(events.MENU_TARGETS, dict) and events.MENU_TARGETS, "菜单白名单为空"
    for key, t in events.MENU_TARGETS.items():
        assert "kind" in t, f"{key} 缺 kind"
        if t["kind"] == "viewer":
            assert t.get("cluster") and t.get("namespace"), f"{key} 的 viewer 目标不完整"


def test_menu_targets_is_config_driven():
    """settings 里给了 menu_targets 就必须能覆盖内置表。"""
    code = _code_only("app/events.py")
    assert "settings.MENU_TARGETS or {" in code, "菜单目标没有走配置驱动"


# ---------- ③ 轮询抖动不得判部署失败;失败时带回真实原因 ----------

class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        return self._data


def test_poll_survives_read_timeouts_and_reports_job_errors(monkeypatch):
    """2026-11-11:轮询时一次读超时,审批卡把"GitHub 超时"当成失败原因;真实原因(部署侧清单校验)看不到。"""
    import requests as rq

    from app import k8s
    monkeypatch.setenv("GITHUB_APP_OWNER", "my-org")
    monkeypatch.setattr(github.time, "sleep", lambda s: None)
    monkeypatch.setattr(github, "_headers", lambda repo: {})
    monkeypatch.setattr(k8s, "deploy_target_allowed", lambda c, n: None)
    calls = {"run": 0}
    title = "deploy demo v1.0.0 (prod)"

    def fake_post(url, **kw):
        return _Resp(204)

    def fake_get(url, **kw):
        if url.endswith("/runs") and "workflows" in url:
            return _Resp(200, {"workflow_runs": [{"id": 7, "html_url": "https://github.com/x/r/actions/runs/7",
                                                  "display_title": title}]})
        if url.endswith("/actions/runs/7"):
            calls["run"] += 1
            if calls["run"] <= 2:
                raise rq.exceptions.ReadTimeout("read timed out")
            return _Resp(200, {"status": "completed", "conclusion": "failure"})
        if url.endswith("/actions/runs/7/jobs"):
            return _Resp(200, {"jobs": [{"id": 9, "name": "deploy", "conclusion": "failure",
                                         "steps": [{"name": "发布", "conclusion": "failure"}]}]})
        if url.endswith("/actions/jobs/9/logs"):
            return _Resp(200, text="2026-11-11T09:36:10.1Z ##[error]清单校验失败：\n"
                                   "2026-11-11T09:36:10.2Z ##[error]  - ConfigMap 'x' 不在部署账号可改的 13 个之内\n"
                                   "2026-11-11T09:36:10.3Z ##[error]Process completed with exit code 1.\n")
        raise AssertionError(url)

    monkeypatch.setattr(github.requests, "post", fake_post)
    monkeypatch.setattr(github.requests, "get", fake_get)
    ok, msg = github.dispatch_and_wait({"repo": "demo", "tag": "v1.0.0",
                                        "github": {"owner": "o", "repo": "r", "workflow": "w.yml", "env": "prod"}})
    assert ok is False
    assert "不在部署账号可改的 13 个之内" in msg, msg
    assert "ReadTimeout" not in msg and "exit code" not in msg
    assert calls["run"] >= 3, "读超时后应继续轮询,而不是直接判失败"
