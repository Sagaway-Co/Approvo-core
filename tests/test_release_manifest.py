"""发布清单(release_manifest):审批卡必须【精确到服务】地说明这次发什么。

守的判据:
  ① 清单按 tag 解析出的同一个 commit 读取;服务 / 迁移 / ConfigMap 原样上卡
  ② 迁移文件按全文扫破坏性语句(不是时间口径)
  ③ fail-close:配了清单却读不到 / 格式不对 / 引用不存在 → 不发审批(409 / 502)
  ④ 路径一律校验,tag 与清单内容都不能拼出仓库外或奇怪的 API 路径
  ⑤ 没配清单 → 行为与从前完全一致
所有仓库名 / 服务名都是占位符。
"""
import json

import pytest
from fastapi import HTTPException

from app import github, manifest, server
from app.providers.slack import messaging

TAG = "V1.2.3-release"
SHA = "0123456789abcdef0123456789abcdef01234567"

MANIFEST = """\
services:
  - svc-a
  - svc-b
migrations:
  - {database: main_db, file: migrations/001_add_col.sql}
  - {database: main_db, file: migrations/002_drop_old.sql}
configmaps:
  - deploy/cm/svc-a.yaml
deployments:
  - deploy/app/svc-b.yaml
notes: "一句话说明 #123"
"""

REGISTRY = """\
services:
  svc-a:
    image: svc-a-img
    deploy:
      - {deployment: svc-a, container: app}
  svc-b:
    image: svc-b-img
    deploy:
      - {deployment: svc-b-server, container: server}
      - {deployment: svc-b-worker, container: worker}
  svc-c:
    image: svc-c-img
"""

FILES = {
    "releases/V1.2.3-release/release.yaml": MANIFEST,
    "releases/V1.2.3-release/migrations/001_add_col.sql": "ALTER TABLE t ADD COLUMN IF NOT EXISTS c int;",
    "releases/V1.2.3-release/migrations/002_drop_old.sql": "ALTER TABLE t DROP COLUMN old;",
    "deploy/services.yaml": REGISTRY,
}


@pytest.fixture()
def gh(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_OWNER", "my-org")
    seen = []

    def file_at(repo, path, ref, max_bytes):
        seen.append((repo, path, ref))
        return FILES.get(path)

    monkeypatch.setattr(github, "tag_commit", lambda repo, tag: SHA if tag == TAG else None)
    monkeypatch.setattr(github, "_file_at", file_at)
    return seen


def _read(registry="deploy/services.yaml"):
    return github.release_manifest("my-monorepo", "releases/{tag}/release.yaml", TAG, registry)


# ─────────────────────── ① ② 解析与上卡内容 ───────────────────────

def test_manifest_is_read_at_the_tag_commit_with_registry(gh):
    m = _read()
    assert m["status"] == "ok" and m["commit"] == SHA and m["path"] == "releases/V1.2.3-release/release.yaml"
    assert {ref for _, _, ref in gh} == {SHA}, "所有文件都必须钉在 tag 解析出的同一个 commit 上读"
    assert [s["name"] for s in m["services"]] == ["svc-a", "svc-b"], "顺序即发布顺序,不得重排"
    assert m["services"][1] == {"name": "svc-b", "image": "svc-b-img",
                                "deployments": ["svc-b-server", "svc-b-worker"]}
    assert m["configmaps"] == ["deploy/cm/svc-a.yaml"] and m["notes"] == "一句话说明 #123"
    assert m["deployments"] == ["deploy/app/svc-b.yaml"]


def test_card_links_point_at_the_tag_commit(gh, monkeypatch):
    """清单 / 迁移 / ConfigMap / commit 都可点,且链到 tag 对应的 commit(不是分支最新)。"""
    monkeypatch.setenv("GITHUB_APP_OWNER", "my-org")
    m = _read()
    blob = f"https://github.com/my-org/my-monorepo/blob/{SHA}"
    assert m["blob_base"] == blob and m["commit_url"] == f"https://github.com/my-org/my-monorepo/commit/{SHA}"
    spec = {"repo": "my-monorepo", "tag": TAG, "image": "x", "submit_time": "-", "manifest": m}
    dump = json.dumps(messaging.approval_message("slk-x", spec)[1], ensure_ascii=False)
    assert f"<{blob}/releases/V1.2.3-release/release.yaml|releases/V1.2.3-release/release.yaml>" in dump
    assert f"<{blob}/releases/V1.2.3-release/migrations/001_add_col.sql|migrations/001_add_col.sql>" in dump
    assert f"<{blob}/deploy/cm/svc-a.yaml|deploy/cm/svc-a.yaml>" in dump
    assert f"<https://github.com/my-org/my-monorepo/commit/{SHA}|{SHA[:12]}>" in dump
    from app import cards
    feishu = json.dumps(cards._manifest_block(spec), ensure_ascii=False)
    assert f"]({blob}/releases/V1.2.3-release/release.yaml)" in feishu


def test_card_without_links_falls_back_to_code():
    m = {"status": "ok", "path": "r/x.yaml", "commit": SHA, "services": [], "migrations": [], "configmaps": ["c.yaml"]}
    spec = {"repo": "r", "tag": TAG, "image": "x", "submit_time": "-", "manifest": m}
    dump = json.dumps(messaging.approval_message("slk-x", spec)[1], ensure_ascii=False)
    assert "`r/x.yaml`" in dump and "`c.yaml`" in dump and "<http" not in dump


def test_migration_danger_is_scanned_from_file_content(gh):
    m = _read()
    assert m["migrations"][0]["danger"] == []
    assert "DROP" in m["migrations"][1]["danger"]


def test_registry_is_optional(gh):
    m = _read(registry=None)
    assert m["services"][0] == {"name": "svc-a", "image": None, "deployments": []}


def test_card_lists_services_images_migrations_and_configmaps(gh):
    m = dict(_read(), image_repo="registry.example.com/ns")
    spec = {"repo": "my-monorepo", "tag": TAG, "image": "registry.example.com/ns:" + TAG,
            "env": "prod", "submit_time": "2026-01-01 10:00", "manifest": m}
    text, blocks = messaging.approval_message("slk-x", spec)
    dump = json.dumps(blocks, ensure_ascii=False)
    for want in ("svc-a", "svc-b-server", "svc-b-worker", "svc-a-img:V1.2.3-release", "deploy/app/svc-b.yaml", "工作负载环境变量变更(1",
                 "registry.example.com/ns", "001_add_col.sql", "main_db", "deploy/cm/svc-a.yaml",
                 "🔴 DROP", "schema 不可回滚"):
        assert want in dump, want
    assert "仅镜像变更" not in dump, "有清单时不能再说「仅镜像变更」"
    assert "registry.example.com/ns:V1.2.3-release" not in dump, "不得再显示那个并不存在的 image_repo:tag"
    fields = json.dumps(blocks[1] if blocks[0]["type"] == "header" else blocks[2], ensure_ascii=False)
    assert "svc-a、svc-b" in dump and "服务" in fields
    assert "svc-a、svc-b" in text, "通知文本(推送 / 无障碍)也要写出服务"
    confirm = next(b for b in blocks if b.get("type") == "actions")["elements"][0]["confirm"]["text"]["text"]
    assert "svc-a" in confirm, "批准确认框必须写明要部署哪些服务"


def test_card_without_services_says_so(gh, monkeypatch):
    FILES_NO_SVC = dict(FILES, **{"releases/V1.2.3-release/release.yaml": "services: []\nconfigmaps: [deploy/cm/svc-a.yaml]\n"})
    monkeypatch.setattr(github, "_file_at", lambda repo, path, ref, mb: FILES_NO_SVC.get(path))
    m = _read(registry=None)
    spec = {"repo": "my-monorepo", "tag": TAG, "image": "x", "submit_time": "-", "manifest": m}
    dump = json.dumps(messaging.approval_message("slk-x", spec)[1], ensure_ascii=False)
    assert "本次不发布任何服务" in dump and "数据库迁移*:无" in dump


def test_no_manifest_keeps_old_card(gh):
    spec = {"repo": "my-app", "tag": TAG, "image": "registry.example.com/my-app:" + TAG, "submit_time": "-"}
    dump = json.dumps(messaging.approval_message("slk-x", spec)[1], ensure_ascii=False)
    assert "registry.example.com/my-app:V1.2.3-release" in dump and "仅镜像变更" in dump
    assert "发布清单" not in dump


# ─────────────────────── ③ fail-close ───────────────────────

@pytest.mark.parametrize("files,why", [
    ({}, "不存在"),                                                              # 清单缺失
    ({"releases/V1.2.3-release/release.yaml": "services: svc-a\n"}, "列表"),
    ({"releases/V1.2.3-release/release.yaml": "migrations: []\n"}, "缺少 services"),
    ({"releases/V1.2.3-release/release.yaml": "services: [svc-a, svc-a]\n"}, "重复"),
    ({"releases/V1.2.3-release/release.yaml": "services: [svc-x]\n", "deploy/services.yaml": REGISTRY}, "没有在服务登记表"),
    ({"releases/V1.2.3-release/release.yaml":
      "services: []\nmigrations: [{database: d, file: migrations/404.sql}]\n"}, "迁移文件"),
    ({"releases/V1.2.3-release/release.yaml":
      "services: []\nmigrations: [{database: d, file: ../../etc/x.sql}]\n"}, "不是合法的仓库内路径"),
    ({"releases/V1.2.3-release/release.yaml":
      "services: []\nmigrations: [{database: d, file: m.sql, extra: 1}]\n"}, "只能有"),
    ({"releases/V1.2.3-release/release.yaml": "services: []\nconfigmaps: [/abs.yaml]\n"}, "不是合法的仓库内路径"),
    ({"releases/V1.2.3-release/release.yaml": "services: []\ndeployments: [../x.yaml]\n"}, "不是合法的仓库内路径"),
    ({"releases/V1.2.3-release/release.yaml": "a: [\n"}, "YAML"),
])
def test_bad_manifest_raises_manifest_error(gh, monkeypatch, files, why):
    monkeypatch.setattr(github, "_file_at", lambda repo, path, ref, mb: files.get(path))
    with pytest.raises(manifest.ManifestError) as e:
        _read(registry="deploy/services.yaml" if "deploy/services.yaml" in files else None)
    assert why in str(e.value)


def test_unknown_tag_is_manifest_error(gh):
    with pytest.raises(manifest.ManifestError, match="不存在"):
        github.release_manifest("my-monorepo", "releases/{tag}/release.yaml", "V9.9.9-release")


@pytest.mark.parametrize("tag", ["../x", "a/b", "V1.0.0-release?ref=main", "", "-x"])
def test_tag_cannot_escape_the_manifest_path(tag):
    with pytest.raises(manifest.ManifestError):
        manifest.manifest_path("releases/{tag}/release.yaml", tag)


def test_path_template_must_contain_tag():
    with pytest.raises(manifest.ManifestError, match="{tag}"):
        manifest.manifest_path("releases/release.yaml", TAG)


def _req():
    return server.ReleaseReq(repo="my-monorepo", tag=TAG)


def test_server_collect_none_when_not_configured():
    assert server._collect_manifest({}, _req()) is None


def test_server_collect_maps_content_errors_to_409(monkeypatch):
    def boom(*a, **k):
        raise manifest.ManifestError("清单坏了")
    monkeypatch.setattr(github, "release_manifest", boom)
    with pytest.raises(HTTPException) as e:
        server._collect_manifest({"release_manifest": {"path": "r/{tag}.yaml"}}, _req())
    assert e.value.status_code == 409 and "fail-close" in e.value.detail


def test_server_collect_maps_github_outage_to_502(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(github, "release_manifest", boom)
    with pytest.raises(HTTPException) as e:
        server._collect_manifest({"release_manifest": {"path": "r/{tag}.yaml"}}, _req())
    assert e.value.status_code == 502


def test_server_collect_uses_source_repo_and_git_tag(monkeypatch):
    got = {}

    def fake(repo, path, tag, registry):
        got.update(repo=repo, path=path, tag=tag, registry=registry)
        return {"status": "ok", "services": []}
    monkeypatch.setattr(github, "release_manifest", fake)
    rel = {"source_repo": "my-org/my-monorepo", "image_repo": "registry.example.com/ns",
           "release_manifest": {"path": "releases/{tag}/release.yaml", "registry": "deploy/services.yaml"}}
    req = server.ReleaseReq(repo="my-monorepo", tag="img-tag", git_tag=TAG)
    m = server._collect_manifest(rel, req)
    assert got == {"repo": "my-org/my-monorepo", "path": "releases/{tag}/release.yaml", "tag": TAG,
                   "registry": "deploy/services.yaml"}
    assert m["image_repo"] == "registry.example.com/ns"
