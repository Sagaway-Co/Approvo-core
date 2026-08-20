"""Secret 变更通道的目标白名单与多项目 release_key。

守的判据：
  · 改 Secret 的目标必须走配置白名单 —— 此前完全没有，cluster/namespace 是请求体任意填的。
    而 /viewer-token 一直有白名单：改 Secret 破坏性更强（旧值不可恢复）判据却更松，是倒挂。
  · 空白名单 = 通道整体关闭（fail-close），不是"没配就全放开"。
  · 不同项目的 Secret 变更走各自的 release 条目,卡片上能看出是谁的变更。
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import k8s, server


@pytest.fixture(autouse=True)
def _clear():
    server._rl_buckets.clear()
    yield
    server._rl_buckets.clear()


def _client():
    return TestClient(server.app)


def _body(cluster="qa", namespace="qa-namespace"):
    return {"user_id": "u", "name": "s", "key": "k", "cluster": cluster, "namespace": namespace}


# ---------- ① 白名单：未登记目标必须拒绝 ----------

def test_unlisted_target_rejected(monkeypatch):
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "t")
    monkeypatch.setattr(server.settings, "SECRET_TARGETS",
                        [{"cluster": "qa", "namespace": "qa-namespace",
                          "release_key": "another-secret"}])
    r = _client().post("/secret/request", json=_body(namespace="other-ns"),
                       headers={"X-Release-Token": "t"})
    assert r.status_code == 403, f"未登记目标却没被拒：{r.status_code} {r.text[:160]}"
    assert "secret_targets" in r.text


def test_empty_whitelist_closes_channel(monkeypatch):
    """没配白名单时必须【整体关闭】，而不是全放开。"""
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "t")
    monkeypatch.setattr(server.settings, "SECRET_TARGETS", [])
    r = _client().post("/secret/request", json=_body(), headers={"X-Release-Token": "t"})
    assert r.status_code == 403, f"空白名单却放行了：{r.status_code}"


def test_whitelist_checked_before_identity(monkeypatch):
    """目标检查必须在群成员校验【之前】——与 /viewer-token 顺序一致，且不必为无效目标去调 IM API。"""
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "t")
    monkeypatch.setattr(server.settings, "SECRET_TARGETS", [])
    called = {"n": 0}

    def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("不该为未登记目标去查群成员")

    monkeypatch.setattr(server.feishu, "get_chat_members", _boom)
    r = _client().post("/secret/request", json=_body(), headers={"X-Release-Token": "t"})
    assert r.status_code == 403
    assert called["n"] == 0, "白名单检查晚于身份校验"


# ---------- ② 目标解析与 release_key ----------

def test_secret_target_allowed_returns_target(monkeypatch):
    monkeypatch.setattr(server.settings, "SECRET_TARGETS", [
        {"cluster": "qa", "namespace": "shared-ns", "release_key": "another-secret"},
        {"cluster": "prod-cluster", "namespace": "prod-namespace"},
    ])
    t = k8s.secret_target_allowed("qa", "shared-ns")
    assert t and t["release_key"] == "another-secret"
    assert k8s.secret_target_allowed("prod-cluster", "prod-namespace") == {
        "cluster": "prod-cluster", "namespace": "prod-namespace"}
    assert k8s.secret_target_allowed("qa", "nope") is None
    # 🔴 跨集群同名 namespace 不得命中 —— QA 与生产的 namespace 常常同名,
    #    只比 namespace 就会把两个环境当成一个。
    assert k8s.secret_target_allowed("prod-cluster", "shared-ns") is None


def test_secret_release_keys_collects_all(monkeypatch):
    monkeypatch.setattr(server.settings, "SECRET_RELEASE_KEY", "my-secret")
    monkeypatch.setattr(server.settings, "SECRET_TARGETS", [
        {"cluster": "qa", "namespace": "qa-namespace", "release_key": "another-secret"},
        {"cluster": "prod-cluster", "namespace": "prod-namespace"},
    ])
    assert k8s.secret_release_keys() == {"my-secret", "another-secret"}


def test_commit_rejects_unrelated_request_id(monkeypatch):
    """/secret/commit 只认属于【已登记的 Secret 入口】的授权，别的一律当作不存在。"""
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "t")
    monkeypatch.setattr(server.settings, "SECRET_TARGETS", [])
    monkeypatch.setattr(server.settings, "SECRET_RELEASE_KEY", "my-secret")
    monkeypatch.setattr(server.store, "get", lambda _rid: {"repo": "my-app", "status": "success"})
    r = _client().post("/secret/commit", json={"request_id": "x", "value": "v"},
                       headers={"X-Release-Token": "t"})
    assert r.status_code == 404, f"把普通发版授权当成 Secret 授权了：{r.status_code}"


# ---------- ③ 不许把硬编码 key 加回来 ----------

def test_no_hardcoded_secret_release_key():
    code = "\n".join(
        ln for ln in (Path(__file__).resolve().parent.parent / "app" / "server.py")
        .read_text(encoding="utf-8").split("\n") if not ln.lstrip().startswith("#"))
    assert "_SECRET_RELEASE_KEY" not in code, "又出现了硬编码的 Secret release key"


# ---------- ④ 只读 SA 名:不猜 ----------

def test_viewer_sa_for_fail_close(monkeypatch):
    """未登记 / 登记了但没写 sa 的目标,必须抛错而不是回落到某个默认账号名。

    原实现对任意输入都返回同一个硬编码 SA 名,于是给别的项目加目标时忘写 sa,
    就会拿着另一个环境的账号名去人家的命名空间。
    """
    from app import k8s, settings

    monkeypatch.setattr(settings, "VIEWER_SA_DEFAULT", "")
    monkeypatch.setattr(settings, "VIEWER_TARGETS", [
        {"cluster": "prod-cluster", "namespace": "prod-namespace", "sa": "prod-viewer"},
        {"cluster": "qa", "namespace": "qa-namespace"},          # 登记了但漏写 sa
    ])
    # 显式写了 sa → 用它
    assert k8s.viewer_sa_for("prod-cluster", "prod-namespace") == "prod-viewer"
    # 漏写 sa 且没有全局兜底 → 抛错,不猜
    with pytest.raises(ValueError, match="未指定 sa"):
        k8s.viewer_sa_for("qa", "qa-namespace")
    # 配了全局兜底才回落(显式意图,不是猜)
    monkeypatch.setattr(settings, "VIEWER_SA_DEFAULT", "approvo-viewer")
    assert k8s.viewer_sa_for("qa", "qa-namespace") == "approvo-viewer"
    # 未登记组合 → 抛错(全局兜底也救不了它)
    with pytest.raises(ValueError, match="未登记"):
        k8s.viewer_sa_for("prod-cluster", "other-ns")


def test_issue_viewer_kubeconfig_converts_failclose_to_tuple(monkeypatch):
    """fail-close 的异常必须被转成 (False, 说明),不能穿成未捕获异常。"""
    from app import k8s, settings

    monkeypatch.setattr(settings, "VIEWER_TARGETS", [])
    ok, msg = k8s.issue_viewer_kubeconfig("prod-cluster", "prod-namespace")
    assert ok is False
    assert "未登记" in msg
