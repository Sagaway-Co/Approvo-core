"""回滚请求端到端（不连外部）：/release 收到 rollback 时，不做 tag 比较、不读清单、不做变更对比，仍建审批。

所有外部调用(飞书、存储、长连接)都替换掉；仓库名与服务名都是占位符。
"""
import pytest
from fastapi import HTTPException

from app import server

RB = "RB-svc-1"


@pytest.fixture
def stubbed(monkeypatch):
    calls = {"saved": None, "created": 0}
    monkeypatch.setattr(server.settings, "RELEASES",
                        {"my-app": {"method": "kubectl", "cluster": "c", "namespace": "n",
                                    "deployment": "d", "container": "x"}}, raising=False)
    monkeypatch.setattr(server.settings, "approval_bypass_on", lambda: False, raising=False)
    monkeypatch.setattr(server.settings, "approval_code_for", lambda stage: "code", raising=False)
    monkeypatch.setattr(server.settings, "APPROVAL_PROVIDER", "feishu", raising=False)
    monkeypatch.setattr(server.settings, "RELEASE_TOKEN", "", raising=False)
    monkeypatch.setattr(server.wshealth, "healthy", lambda: True)
    monkeypatch.setattr(server.feishu, "get_chat_members", lambda chat: ["u1"])
    monkeypatch.setattr(server.feishu, "send_card", lambda *a, **k: None)

    def create_instance(*a, **k):
        calls["created"] += 1
        return "inst-rb-1"
    monkeypatch.setattr(server.feishu, "create_instance", create_instance)
    monkeypatch.setattr(server.store, "status_history", lambda repo, tag, stage: [])
    monkeypatch.setattr(server.store, "save", lambda code, spec: calls.__setitem__("saved", spec))

    def forbidden(*a, **k):
        raise AssertionError("回滚请求不得做 tag 比较 / 变更对比 / 清单读取")
    monkeypatch.setattr(server, "_collect_changes", forbidden)
    monkeypatch.setattr(server, "_collect_db_changes", forbidden)
    monkeypatch.setattr(server, "_collect_manifest", forbidden)
    monkeypatch.setattr(server.github, "check_release_promotes_pre", forbidden, raising=False)
    return calls


def _req(**over):
    rb = {"id": RB, "services": {"my-app": "v1.2.1"}, "configmaps": [], "allow_schema_behind": False, "notes": ""}
    rb.update(over.pop("rb", {}))
    data = {"repo": "my-app", "tag": RB, "stage": "release", "operator_user_id": "u1", "rollback": rb}
    data.update(over)
    return server.ReleaseReq(**data)


def test_rollback_request_creates_approval_without_release_checks(stubbed):
    out = server.release(_req(), x_release_token="")
    assert out == {"instance_code": "inst-rb-1", "repo": "my-app", "tag": RB, "image": RB}
    assert stubbed["created"] == 1
    spec = stubbed["saved"]
    assert spec["rollback"]["services"] == {"my-app": "v1.2.1"}
    assert spec["changes"] is None and spec["db_changes"] is None and spec["manifest"] is None


def test_rollback_id_must_equal_tag(stubbed):
    with pytest.raises(HTTPException) as e:
        server.release(_req(tag="RB-other-1"), x_release_token="")
    assert e.value.status_code == 400
    assert stubbed["created"] == 0


def test_invalid_rollback_is_400_and_creates_nothing(stubbed):
    with pytest.raises(HTTPException) as e:
        server.release(_req(rb={"services": {}}), x_release_token="")
    assert e.value.status_code == 400
    assert stubbed["created"] == 0


def test_plain_release_still_uses_release_checks(stubbed, monkeypatch):
    """没有 rollback 字段时，发版路径原样：仍会走 check-prereq（这里用拒绝来证明它被调用）。"""
    def refuse(repo, tag):
        return False, "stub-refused"
    monkeypatch.setattr(server.github, "check_release_promotes_pre", refuse, raising=False)
    req = server.ReleaseReq(repo="my-app", tag="V1.2.3-release", stage="release", operator_user_id="u1")
    with pytest.raises(HTTPException) as e:
        server.release(req, x_release_token="")
    assert e.value.status_code == 409 and "stub-refused" in e.value.detail
