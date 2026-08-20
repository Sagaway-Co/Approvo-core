"""渗透测试打出来的问题的回归守卫。

守的是判据，不是实现：
  · 公网不得暴露自动生成的接口文档（泄露全部端点定义 + 内部默认值）
  · 限流必须【按端点分桶】—— 一个端点被刷不能连坐其它端点
  · 认证失败不得消耗正常业务额度（否则匿名攻击者可用错 token 阻断全部功能）
  · 请求模型不得有指向生产的默认值（缺字段就拒绝，不猜）
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import server


@pytest.fixture(autouse=True)
def _clear_buckets():
    server._rl_buckets.clear()
    yield
    server._rl_buckets.clear()


def _client():
    return TestClient(server.app)


# ---------- ① 接口文档不得对外暴露 ----------

def test_openapi_docs_disabled_by_default():
    """默认必须关闭：少配一个环境变量不应导致文档意外敞开（fail-close）。"""
    c = _client()
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert c.get(path).status_code == 404, f"{path} 仍然可访问"


def test_healthz_still_open():
    """关文档不能把健康检查一起关掉。"""
    assert _client().get("/healthz").status_code == 200


# ---------- ② 限流必须按端点分桶 ----------

def test_rate_buckets_are_independent():
    limit = server._RL_MAX
    for _ in range(limit):
        assert server._rate_ok("release")
    assert not server._rate_ok("release"), "同一桶应当被限流"
    # 另一个端点的桶不受影响 —— 这是本次修复的核心
    assert server._rate_ok("secret_commit"), "限流串桶了：一个端点被刷会连坐其它端点"


def test_auth_failure_does_not_consume_business_quota(monkeypatch):
    """错 token 反复请求，不得吃掉正常业务额度。"""
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "correct-token-for-test")
    c = _client()
    for _ in range(server._RL_MAX + 5):
        r = c.post("/release", json={"repo": "x", "tag": "v1"},
                   headers={"X-Release-Token": "wrong"})
        assert r.status_code == 401, f"预期 401，得到 {r.status_code}"
    # 业务桶必须仍然干净
    assert server._rate_ok("release"), "认证失败消耗了正常业务额度"
    assert "_auth_fail" in server._rl_buckets, "认证失败没有记进独立的拒绝桶"


def test_auth_failure_bucket_has_a_ceiling(monkeypatch):
    """拒绝桶本身要有天花板，否则暴力尝试无上限。"""
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "correct-token-for-test")
    monkeypatch.setattr(server, "_RL_REJECT_MAX", 3)
    c = _client()
    codes = [c.post("/release", json={"repo": "x", "tag": "v1"},
                    headers={"X-Release-Token": "wrong"}).status_code for _ in range(5)]
    assert 429 in codes, f"拒绝桶没有上限：{codes}"


def test_admin_basic_auth_is_rate_limited(monkeypatch):
    """/admin 的 Basic Auth 必须有暴力尝试上限。

    ADMIN_USER 默认是 admin(已知),且 /admin 可能在公网 host 上可达
    (ingress 按 host 路由,关掉 admin ingress 不会关掉这条路径),
    所以没有上限就等于把口令暴露在无限次尝试之下。
    """
    monkeypatch.setattr(server.settings, "ADMIN_PASSWORD", "correct-admin-pw")
    monkeypatch.setattr(server, "_RL_REJECT_MAX", 3)
    c = _client()
    codes = [c.get("/admin", auth=("admin", "wrong")).status_code for _ in range(5)]
    assert 401 in codes, f"预期先出现 401：{codes}"
    assert 429 in codes, f"Basic Auth 没有暴力尝试上限：{codes}"


def test_admin_failure_does_not_consume_business_quota(monkeypatch):
    monkeypatch.setattr(server.settings, "ADMIN_PASSWORD", "correct-admin-pw")
    c = _client()
    for _ in range(server._RL_MAX + 5):
        assert c.get("/admin", auth=("admin", "wrong")).status_code == 401
    assert server._rate_ok("release"), "admin 认证失败消耗了正常业务额度"


# ---------- ③ 不得有指向生产的默认值 ----------

def test_viewer_token_requires_explicit_target(monkeypatch):
    """少传 cluster/namespace 必须 422 拒绝，绝不默认打到生产。"""
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "t")
    r = _client().post("/viewer-token", json={"user_id": "u"},
                       headers={"X-Release-Token": "t"})
    assert r.status_code == 422, f"缺 cluster/namespace 却没被拒：{r.status_code}"


def test_secret_request_requires_explicit_target(monkeypatch):
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "t")
    r = _client().post("/secret/request",
                       json={"user_id": "u", "name": "s", "key": "k"},
                       headers={"X-Release-Token": "t"})
    assert r.status_code == 422, f"缺 cluster/namespace 却没被拒：{r.status_code}"


def test_no_production_cluster_default_in_source():
    """源码里不得再出现把某个集群名当默认值的写法。

    这条刻意做成文本断言：默认值这类问题最容易在重构中被"顺手加回去"。
    """
    # 用相对 __file__ 的绝对路径：不依赖 pytest 从哪个 cwd 启动（同 conftest.py 的做法）
    src = (Path(__file__).resolve().parent.parent / "app" / "server.py").read_text(encoding="utf-8")
    bad = re.findall(r'(?:cluster|namespace)\s*:\s*str\s*=\s*"[^"]+"', src)
    assert not bad, f"请求模型又出现了硬编码默认目标：{bad}"
    # 泛化：不盯某个具体集群名,而盯【用 or 给 cluster 兜底】这个形状 ——
    # 集群改名后,只盯字面量的守卫会静默失效(这正是"守卫本身会过期"的一种)。
    code = "\n".join(ln for ln in src.split("\n") if not ln.lstrip().startswith("#"))
    bad = re.findall(r'get\("cluster"\)\s*or\s*"', code)
    assert not bad, f'又出现了给 cluster 兜底默认值的写法：{bad}'
