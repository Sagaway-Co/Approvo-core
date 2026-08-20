"""审批直通(应急旁路)开关:审批平台不可用时的应急发版路径。

背景:IM 的 API 额度耗尽 → 建审批实例/拉群/发卡全部失败 → 审批发不出去 →
QA 与生产都卡在"提审批"这步。本开关(settings.approval_bypass_on)提供一条不碰
IM API 的应急路径,且【非对称】:qa 放行秒通过、生产只留痕不部署(fail-close)。

守两条最容易写错的红线:
  ① 生产【绝不】自动部署 —— 只落库置 failed,由人工执行。
  ② 开关只在显式打开时生效;env 缺失时按生产处理(不猜、不放行)。
"""
import datetime as _dt

from app import events, server, settings, store


def _freeze_cst(monkeypatch, year, month, day=15):
    """冻结 settings 里 datetime.now(CN_TZ) 的返回,用来验证"按当月生效/跨月失效"。"""
    fake = type("DT", (), {
        "now": staticmethod(lambda tz=None: _dt.datetime(year, month, day, tzinfo=tz))})
    monkeypatch.setattr(settings, "datetime", fake)


def test_switch_only_active_in_its_own_month(monkeypatch):
    """开关值是 YYYY-MM,只在该自然月(CST)生效;跨月自动失效。"""
    _freeze_cst(monkeypatch, 2026, 8)                 # 假装现在是 2026-08(CST)
    monkeypatch.setenv("APPROVAL_BYPASS", "2026-08")
    assert settings.approval_bypass_on() is True      # 当月:生效

    # 🔴 同一个值不变,时间走到 9 月 1 号 0 点 → 自动失效(生产也随之恢复需审批)
    _freeze_cst(monkeypatch, 2026, 9, 1)
    assert settings.approval_bypass_on() is False

    # 未来月 / 过期月 / 简单真值 / 单位数月 / 空 / 非法月 一律不生效(fail-close)
    _freeze_cst(monkeypatch, 2026, 8)
    for bad in ("2026-09", "2026-07", "1", "true", "on", "2026-8", "", "2026-13", "x"):
        monkeypatch.setenv("APPROVAL_BYPASS", bad)
        assert settings.approval_bypass_on() is False, bad
    monkeypatch.delenv("APPROVAL_BYPASS", raising=False)
    assert settings.approval_bypass_on() is False     # 不设 = 关(默认安全)


class _SyncThread:
    """让 daemon 线程在测试里同步执行,断言可确定。"""
    def __init__(self, target, args=(), daemon=None):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def _patch_store(monkeypatch):
    saved, claimed = [], []
    monkeypatch.setattr(store, "save",
                        lambda code, spec, status="pending": saved.append((code, status)))
    monkeypatch.setattr(store, "try_claim_for_deploy",
                        lambda code: (claimed.append(code) or True))
    return saved, claimed


_SPEC = {"repo": "my-app", "tag": "V1.2.3-pre", "image": "registry.example.com/my-app:V1.2.3-pre"}


def test_qa_bypass_auto_deploys_via_original_flow(monkeypatch):
    """qa:落 pending → 原子认领 → 走【原有部署流程】(_run_deploy),即"秒通过"。"""
    saved, claimed = _patch_store(monkeypatch)
    monkeypatch.setattr(server.threading, "Thread", _SyncThread)
    ran = []
    monkeypatch.setattr(events, "_run_deploy",
                        lambda code, spec, who: ran.append((code, spec, who)))

    out = server._bypass_release("bypass:abc", _SPEC, "qa")

    assert out["bypass"] == "qa-auto-approved"
    assert saved == [("bypass:abc", "pending")]        # 先落 pending
    assert claimed == ["bypass:abc"]                    # 原子认领
    assert len(ran) == 1 and ran[0][0] == "bypass:abc"  # 确实触发了原有部署


def test_qa_bypass_does_not_double_deploy_when_claim_lost(monkeypatch):
    """认领失败(别处已抢)→ 不重复部署。防重语义与审批通过路径一致。"""
    _patch_store(monkeypatch)
    monkeypatch.setattr(store, "try_claim_for_deploy", lambda code: False)
    monkeypatch.setattr(server.threading, "Thread", _SyncThread)
    ran = []
    monkeypatch.setattr(events, "_run_deploy", lambda *a: ran.append(a))

    server._bypass_release("bypass:abc", _SPEC, "qa")
    assert ran == [], "认领失败时绝不能再触发部署"


def test_prod_bypass_records_failed_and_never_deploys(monkeypatch):
    """🔴 生产:只落库 + 置 failed + 【不部署】。绝不静默上一个没人批过的版本。"""
    saved, claimed = _patch_store(monkeypatch)
    monkeypatch.setattr(server.threading, "Thread", _SyncThread)
    ran = []
    monkeypatch.setattr(events, "_run_deploy", lambda *a: ran.append(a))

    out = server._bypass_release("bypass:xyz", _SPEC, "prod")

    assert out["bypass"] == "prod-recorded-only"
    assert out["status"] == "failed"
    assert saved == [("bypass:xyz", "failed")]   # 直接 failed
    assert claimed == []                          # 不认领
    assert ran == []                              # 不部署


def test_missing_env_is_treated_as_prod_failclose(monkeypatch):
    """env 缺失 / 非 qa 一律按生产处理(不猜、不放行)——与"缺省值指向生产"这类
    致命默认相反,这里的缺省是【最安全】的那一侧:只留痕、不部署。"""
    saved, _ = _patch_store(monkeypatch)
    monkeypatch.setattr(server.threading, "Thread", _SyncThread)
    monkeypatch.setattr(events, "_run_deploy",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应部署")))

    for env in (None, "", "staging", "PROD"):
        saved.clear()
        out = server._bypass_release("bypass:x", _SPEC, env)
        assert out["bypass"] == "prod-recorded-only", env
        assert saved == [("bypass:x", "failed")], env
