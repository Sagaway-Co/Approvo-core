"""`stage=release` 的前置校验：两类失败必须**分得开**，且都要指得出方向。

真实踩过的一条链:

  1. approvo 去读某个应用的源码仓(它的 `source_repo` != release key)
  2. GitHub App 在那个仓上读不到安装信息 → `/repos/{owner}/{repo}/installation` 404
  3. `raise_for_status()` 抛 HTTPError → 被宽捕获包成 **502 check-prereq 无法校验**

对排查的人零指向性：看起来像 approvo 挂了，实际是一个勾选框没勾(或 App id/私钥不配对)。
而按 fail-close 的语义，「App 装不上/认错了」是**配置错误**(409)，不是**校验服务不可用**(502)。

第二条(本文件一并守):`tag` 是镜像 tag,可能与真实 git tag 不同
(典型是"一仓两制":服务端的 git tag 带前缀,而 CI 传过来的镜像 tag 剥掉了前缀),
promote 校验必须用 `git_tag`,否则永远找不到那个 tag。
"""
import pytest
import requests

from app import github


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")


@pytest.fixture(autouse=True)
def _app_env(monkeypatch):
    # 让 _repo_read_token 走 App 分支而不是 PAT 兜底
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "x")
    monkeypatch.setenv("GITHUB_APP_OWNER", "my-org")
    monkeypatch.setattr(github, "_app_jwt", lambda: "jwt")
    github._read_tokens.clear()


def test_读不到安装信息时报三种可能而不是裸HTTPError(monkeypatch):
    monkeypatch.setattr(github.requests, "get", lambda *a, **k: _Resp(404))

    with pytest.raises(github.AppInstallLookupError) as ei:
        github._repo_read_token("my-app")

    msg = str(ei.value)
    # 判据落在"看到这句话的人能不能直接照做"上：三种可能都要点名，
    # 只说"去装 App"会让人去做一件已经做过的事(实测:App 装了仍然 404)。
    assert "my-app" in msg, msg
    assert "GITHUB_APP_ID" in msg, msg
    assert "GITHUB_APP_OWNER" in msg, msg
    # 反向：不能只是把状态码丢出来
    assert msg.strip() not in {"404", "404 Client Error"}


def test_其它错误码仍然冒泡为HTTPError(monkeypatch):
    """阳性对照：只特判 404。500 之类仍是"服务不可用"，必须继续 fail-close。

    没有这条，上面那个特判很容易被写成"把所有异常都当成 App 没装"。
    """
    monkeypatch.setattr(github.requests, "get", lambda *a, **k: _Resp(500))

    with pytest.raises(requests.HTTPError):
        github._repo_read_token("my-app")


def test_ref不存在时GitHub返回422_必须当作None而不是炸掉(monkeypatch):
    """实测:/commits/{ref} 对不存在的 ref 返回 **422** 而非 404。

    只优雅处理 404 的话，422 冒泡成 HTTPError → /release 宽捕获 → 502
    「check-prereq 无法校验」——把一句本该指向明确的「tag 不存在」(409)
    变成了"校验服务坏了"，排查空转半天。
    """
    monkeypatch.setattr(github, "_read_headers", lambda repo: {})
    monkeypatch.setattr(github.requests, "get", lambda *a, **k: _Resp(422))

    assert github.tag_commit("my-app", "V0.0.8-release") is None


def test_promote校验对不存在的tag给出409级别的拒绝而不是异常(monkeypatch):
    """整条链的回归：镜像 tag(剥掉了前缀)在仓库里不存在 → 必须得到
    「不放行 + 说得清原因」，绝不能抛异常。"""
    monkeypatch.setattr(github, "_read_headers", lambda repo: {})
    monkeypatch.setattr(github.requests, "get", lambda *a, **k: _Resp(422))

    ok, why = github.check_release_promotes_pre("my-app", "V0.0.8-release")

    assert not ok
    assert "不存在" in why, why


def test_promote校验用的是真实git_tag(monkeypatch):
    """`server-V0.0.8-release` 必须去找 `server-V0.0.8-pre`，而不是 `V0.0.8-pre`。"""
    seen: list[str] = []

    def fake_tag_commit(repo: str, tag: str):
        seen.append(tag)
        return "sha-same"

    monkeypatch.setattr(github, "tag_commit", fake_tag_commit)

    ok, why = github.check_release_promotes_pre("my-app", "server-V0.0.8-release")

    assert ok, why
    assert seen == ["server-V0.0.8-release", "server-V0.0.8-pre"], seen


def test_pre与release不同commit时拒绝(monkeypatch):
    """变异对照：证明这套校验在该拒的时候真的会拒（不是恒真）。"""
    monkeypatch.setattr(
        github, "tag_commit",
        lambda repo, tag: "sha-rel" if tag.endswith("-release") else "sha-pre",
    )

    ok, why = github.check_release_promotes_pre("my-app", "server-V0.0.8-release")

    assert not ok
    assert "不同 commit" in why, why
