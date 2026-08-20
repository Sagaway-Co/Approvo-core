"""审批卡片必须显示数据库变更。

在此之前:一次带着迁移的发版,卡片上写的是
"仅镜像变更 —— 本次申请未附带环境变量/配置变更"。
审批人以为批的是换镜像,实际发生的是改生产库结构 ——
因为迁移文件在部署仓,业务仓的 compare 天然看不见它。
"""
from app import cards, github


def _content(spec):
    return cards._db_change_block(spec)[0]["text"]["content"]


def test_unknown_never_renders_as_no_change():
    """🔴 最重要的一条:查不到必须显示"无法判定",绝不能显示成"无变更"。

    把无知包装成安全,比不显示更危险 —— 审批人会据此认为不涉及数据库。
    """
    for spec in ({"db_changes": {}},
                 {"db_changes": {"status": "unknown", "reason": "404"}}):
        c = _content(spec)
        assert "无法判定" in c, c
        assert "无 ——" not in c, "查不到时绝不能渲染成『无变更』"


def test_block_absent_when_feature_not_configured():
    """没配 db_migrations 的应用不渲染这一块 —— 不给没用到的人添噪音。

    ⚠️ 与上一条的边界:【没开这个功能】= 不显示;【开了但查不到】= 必须显示"无法判定"。
    这两者绝不能混:后者是安全信息,前者只是噪音。
    """
    assert cards._db_change_block({}) == []
    assert cards._db_change_block({"db_changes": None}) == []


def test_none_is_explicit_about_the_baseline():
    c = _content({"db_changes": {"status": "none"}})
    assert "无" in c and "上次成功部署" in c, "『无变更』必须说明是相对哪个基线"


def test_danger_is_flagged_and_explains_rollback_trap():
    c = _content({"db_changes": {"status": "ok", "files": [
        {"name": "0078_drop_old.sql", "status": "added", "danger": ["DROP"]}]}})
    assert "0078_drop_old.sql" in c
    assert "🔴" in c and "DROP" in c
    # 必须解释为什么危险:镜像可回滚、schema 不可回滚
    assert "回滚" in c, "破坏性变更必须说明回滚陷阱,否则红标只是装饰"


def test_ok_states_the_time_based_caveat():
    """口径必须写明:这是时间口径,不等于"数据库里未应用的迁移"。"""
    c = _content({"db_changes": {"status": "ok", "files": [
        {"name": "0079_idx.sql", "status": "added", "danger": []}]}})
    assert "口径" in c and "门禁" in c


def test_danger_patterns_cover_the_real_hazards():
    """规则本身要能命中真实的破坏性语句。"""
    samples = {
        "+ALTER TABLE t DROP COLUMN c;": "DROP",
        "+ALTER TABLE t ALTER COLUMN c TYPE bigint;": "改列类型",
        "+ALTER TABLE t ALTER COLUMN c SET NOT NULL;": "加 NOT NULL",
        "+ALTER TABLE t RENAME TO t2;": "重命名",
        "+TRUNCATE TABLE t;": "TRUNCATE",
        "+DELETE FROM t WHERE 1=1;": "DELETE",
    }
    for sql, want in samples.items():
        hit = [label for rx, label in github._DANGER_SQL if rx.search(sql)]
        assert want in hit, f"{sql!r} 应命中 {want}，实际 {hit}"
    # 阴性对照:安全语句不得被误标
    safe = "+CREATE INDEX CONCURRENTLY idx ON t(c);\n+ALTER TABLE t ADD COLUMN c text;"
    assert not [lb for rx, lb in github._DANGER_SQL if rx.search(safe)], "安全语句被误标"


def test_missing_baseline_returns_unknown_not_none():
    r = github.db_migration_changes("my-deploy-repo", None)
    assert r["status"] == "unknown", "没有时间基线时必须是 unknown,不能是 none"


def test_all_baselines_are_stage_aware():
    """🔴 所有"上次成功"基线都必须能按 stage 取 —— 同族共四处。

    同一个根因(键少了 stage)曾以四种面貌出现:
      ① status_history       → 生产提交被静默跳过(HTTP 200,实际没执行)
      ② 幂等 uuid            → IM 侧 uuid conflict → 500
      ③ last_success_commit  → 卡片谎报"与已部署版本相比无新增提交"
      ④ last_success_at      → 卡片谎报"迁移目录没有变更",而那次真的新增并应用了迁移

    ③④ 是【谎报安全】：审批人看到"无变更"就会放松审查,而实际正在改生产库。
    ⇒ 修这类问题必须一次修完同族所有键,否则症状只是换个位置出现。
    """
    import inspect
    import pathlib
    import re

    from app import store

    for fn in (store.status_history, store.last_success_commit, store.last_success_at):
        assert "stage" in inspect.signature(fn).parameters, f"{fn.__name__} 缺 stage 参数"
        src = inspect.getsource(fn)
        code = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))
        assert "spec_json" in code, f"{fn.__name__} 的 SQL 未按 stage 过滤"

    body = (pathlib.Path(__file__).resolve().parent.parent / "app" / "server.py").read_text()
    body = "\n".join(ln for ln in body.split("\n") if not ln.strip().startswith("#"))
    for call in (r"status_history\(\s*req\.repo,\s*req\.tag,\s*req\.stage",
                 r"last_success_commit\(\s*req\.repo,\s*req\.stage",
                 r"last_success_at\(\s*req\.repo,\s*req\.stage"):
        assert re.search(call, body), f"server 未把 stage 传给 {call}"
    assert re.search(r"req\.stage", body), "uuid/基线都应引用 req.stage"
