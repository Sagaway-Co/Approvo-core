"""动态部署凭据:取代常驻 runner 的长期 kubeconfig。

实测的事实(这些数字决定了设计,写进测试以免被人"优化"掉):
  · TokenRequest 的 TTL 下限是 10 分钟 —— 请求 1m/5m 会被 API server 直接拒绝;
  · 删掉 boundObjectRef 后 0 秒仍可用、15 秒起 Unauthorized(认证缓存约 10s);
  · 删 SA 则 3 秒内失效(紧急手段,会影响并发,不作常规)。
⇒ 所以"绑定对象 + 用完即删"不是可选优化,而是把暴露窗口压到部署时长的唯一办法。
"""
from app import deploycred, k8s, settings


def test_grant_is_single_use():
    g, ttl = deploycred.issue_grant("inst-1", "qa", "qa-namespace")
    assert ttl == deploycred.TTL_MIN
    ok, payload, _ = deploycred.redeem_grant(g)
    assert ok and payload["cluster"] == "qa" and payload["namespace"] == "qa-namespace"
    ok2, _, err = deploycred.redeem_grant(g)
    assert not ok2, "grant 必须是一次性的"
    assert "无效" in err or "过期" in err


def test_grant_expiry(monkeypatch):
    g, _ = deploycred.issue_grant("inst-2", "qa", "qa-namespace", ttl_min=0)
    ok, _, err = deploycred.redeem_grant(g)
    assert not ok and "过期" in err


def test_slot_pool_fails_closed():
    """槽位耗尽必须【拒绝】,绝不能退化成"不绑定就签"。"""
    taken = [deploycred.claim_slot() for _ in range(deploycred.SLOTS)]
    assert all(taken), "全部槽位应可用"
    assert len(set(taken)) == deploycred.SLOTS, "槽位不得重复分配"
    assert deploycred.claim_slot() is None, "槽满必须返回 None(由调用方拒绝签发)"
    for s in taken:
        deploycred.release_slot(s)
    assert deploycred.claim_slot() is not None
    deploycred.release_slot(deploycred.SLOT_FMT.format(1))


def test_slot_names_match_rbac():
    """槽位名必须与部署仓侧 RBAC 的 resourceNames 一一对应。

    RBAC 里 delete 用 resourceNames 限死这些名字 —— 名字对不上就删不掉绑定对象,
    "用完即毁"会静默失效(token 仍活到 TTL 结束)。
    """
    names = [deploycred.SLOT_FMT.format(i) for i in range(1, deploycred.SLOTS + 1)]
    assert names[0] == "approvo-deploy-cred-1"
    assert names[-1] == f"approvo-deploy-cred-{deploycred.SLOTS}"


def test_deploy_target_fail_close(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOY_TARGETS",
                        [{"cluster": "qa", "namespace": "qa-namespace", "sa": "approvo-deployer"}])
    assert k8s.deploy_target_allowed("qa", "qa-namespace")["sa"] == "approvo-deployer"
    assert k8s.deploy_target_allowed("qa", "other-ns") is None, "未登记必须拒绝"
    # 跨集群同名 namespace 也不得命中
    assert k8s.deploy_target_allowed("prod-cluster", "qa-namespace") is None


def test_issue_refuses_unregistered_target(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOY_TARGETS", [])
    ok, msg = k8s.issue_deploy_kubeconfig("qa", "qa-namespace", "approvo-deploy-cred-1")
    assert not ok and "未登记" in msg


def test_issue_refuses_target_without_sa(monkeypatch):
    """登记了但没写 sa → 抛错不猜(与 viewer_sa_for 同一教训)。"""
    monkeypatch.setattr(settings, "DEPLOY_TARGETS",
                        [{"cluster": "qa", "namespace": "qa-namespace"}])
    ok, msg = k8s.issue_deploy_kubeconfig("qa", "qa-namespace", "approvo-deploy-cred-1")
    assert not ok and "未指定 sa" in msg


def test_ttl_floor_is_documented():
    """TTL 不得被改到 10 分钟以下 —— API server 会直接拒绝,签发会整体失败。"""
    assert deploycred.TTL_MIN >= 10, "TokenRequest 下限 10 分钟(实测 1m/5m 被拒)"


def test_dispatch_skips_grant_when_target_unregistered(monkeypatch):
    """未登记目标不发券 —— 流水线因此明确失败,而不是回落到 runner 上的文件。

    🔴 本用例原来断言"可以按项目名兜底猜 namespace"。那条兜底正是一次真实事故的
       元凶:它让一部分应用端到端跑通、掩盖了"配置里的 deploy 根本没传到 dispatch",
       直到另一批应用发版时才炸。测试把错误行为固化成了契约 —— 改对反而 CI 红。
       现已改为断言"不猜"。
    """
    from app import github
    monkeypatch.setattr(settings, "DEPLOY_TARGETS", [])
    spec = {"repo": "my-app", "tag": "V1-pre", "project": "MyProject",
            "github": {"owner": "o", "repo": "r", "workflow": "w", "env": "qa"}}
    assert github._deploy_ns(spec, "qa") == "", "不得按项目名猜命名空间"
    # 显式配了 deploy 才认
    spec2 = {**spec, "deploy": {"cluster": "qa", "namespace": "qa-namespace"}}
    assert github._deploy_ns(spec2, "qa") == "qa-namespace"
    assert k8s.deploy_target_allowed(github.env_cluster(spec2, "qa"),
                                     github._deploy_ns(spec2, "qa")) is None  # 白名单为空


def test_deploy_ns_does_not_guess():
    from app import github
    assert github._deploy_ns({"project": "MyProject"}, "qa") == "", "未显式配置不得猜"
    assert github.env_cluster({}, "prod") == "", "生产 cluster 不得猜"


def test_deploy_target_must_reach_dispatch(monkeypatch):
    """配置里的 deploy 必须真的传到派发那一步。

    真实事故:spec 是【白名单式】逐字段组装的,给各 stage 配了 deploy 却没有把它
    复制进 spec,于是 dispatch 里 env_cluster()/_deploy_ns() 拿不到目标 → 不发券
    → 强制动态凭据的流水线在"取凭据"这步失败,挡住了一整批应用的发版。
    """
    from app import github

    spec = {"repo": "my-app", "tag": "V1", "project": "MyProject",
            "deploy": {"cluster": "qa", "namespace": "qa-namespace"},
            "github": {"env": "qa"}}
    assert github.env_cluster(spec, "qa") == "qa"
    assert github._deploy_ns(spec, "qa") == "qa-namespace"


def test_no_project_fallback_masks_missing_config():
    """没有 deploy 配置时必须解析为空 —— 不许按项目名猜。

    猜出来的目标会让"配置漏了"这件事在某些项目上看不出来,
    而在另一些项目上突然炸掉。宁可一律不发券、明确失败。
    """
    from app import github

    for proj in ("MyProject", "Another", "unknown", ""):
        spec = {"repo": "x", "project": proj, "github": {"env": "qa"}}
        assert github._deploy_ns(spec, "qa") == "", f"project={proj} 不应被猜出命名空间"
    assert github.env_cluster({"project": "MyProject"}, "prod") == ""


def test_server_spec_carries_deploy():
    """server.py 组装 spec 时必须复制 deploy 字段（白名单式组装的陷阱）。"""
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "server.py").read_text()
    body = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))
    assert re.search(r'"deploy":\s*rel\.get\("deploy"\)', body), \
        "spec 未复制 deploy —— 配置再对也传不到派发那一步"


def test_status_history_sql_includes_stage():
    """判重必须能按 stage 区分 —— 否则"同一 tag 发两个环境"会被静默跳过。

    真实事故:某条通道的 tag 是部署仓的 commit sha,两个环境完全相同。
    QA 应用成功后提交生产,approvo 返回 {"skipped":"already deployed"} 且 HTTP 200
    —— 生产【根本没执行】,但看起来像成功。这类"静默跳过"比报错危险得多。

    普通发版的 tag 天然带环境(-pre / -release),所以旧逻辑一直没暴露问题。
    """
    import inspect

    from app import store

    src = inspect.getsource(store.status_history)
    code = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))
    assert "stage" in inspect.signature(store.status_history).parameters, \
        "status_history 必须接受 stage 参数"
    assert "stage" in code and "spec_json" in code, \
        "判重 SQL 必须按 stage 过滤"


def test_server_passes_stage_to_dedup():
    """server 组装判重时必须把 req.stage 传进去（传了参数却不用等于没改）。"""
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "server.py").read_text()
    body = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))
    assert re.search(r"status_history\(\s*req\.repo,\s*req\.tag,\s*req\.stage\s*\)", body), \
        "判重未传 stage —— 同一 tag 发两个环境会被误判为重复"


def test_uuid_includes_stage():
    """IM 侧的幂等 uuid 必须含 stage —— 否则同一 tag 发两个环境会 uuid conflict。

    同根因的第二处:修好 status_history 的 stage 过滤后,QA 与生产两侧
    len(history) 都是 0,uuid 反而更容易完全相同 —— create_instance 判重冲突 → 500。
    ⇒ 修"某个键漏了 stage"时,必须把同族的所有键一起修,否则只是把症状换个位置。
    """
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "server.py").read_text()
    body = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))
    m = re.search(r"uuid\s*=\s*hashlib\.sha256\(\s*\n?\s*f\"([^\"]+)\"", body)
    assert m, "找不到 uuid 计算式"
    expr = m.group(1)
    for part in ("req.repo", "req.tag", "req.stage", "req.commit"):
        assert part in expr, f"uuid 计算式缺 {part}：{expr}"
