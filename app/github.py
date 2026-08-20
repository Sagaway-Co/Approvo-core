"""触发 GitHub Actions 部署(workflow_dispatch)。

审批通过后,不由本服务直接动集群,而是调 GitHub API 触发目标 repo 的部署 workflow,
由 Actions 用 GitHub Secrets 里的 ACK 凭证去发已构建好的镜像。本服务只需一个能
workflow_dispatch 的 token(env GITHUB_TOKEN),不持有任何 kubeconfig。

被触发的 workflow 必须带 `on: workflow_dispatch`,并接收 inputs(至少 tag、env)。
"""
import os
import re
import time

import requests

from app import deploycred, k8s

API = os.environ.get("GITHUB_API", "https://api.github.com")  # 自建/企业版改这里

# 安装 token 缓存(GitHub App 方式,token ~1h 过期,提前续)
# 按 target_repo 分开缓存 - 每个受限发版仓拿自己 scope 的 token,
# 支持同一 approvo 实例 dispatch 到多个受限仓 (my-deploy-repo, another-deploy-repo, ...)。
_inst_tokens: dict = {}


def _app_jwt() -> str:
    """用 App 私钥签一个短期 JWT(RS256),用于换安装 token。"""
    import jwt  # PyJWT[crypto]
    app_id = os.environ["GITHUB_APP_ID"]
    key = os.environ["GITHUB_APP_PRIVATE_KEY"]   # PEM 全文
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, key, algorithm="RS256")


def _installation_token(target_repo: str) -> str:
    """按目标受限发版仓拿一个 scope 缩到该仓的 token。

    每个受限仓独立缓存 - 保证同一 approvo 实例 dispatch 到多个受限仓 (my-deploy-repo /
    another-deploy-repo / ...) 时 scope 互不污染。原本这里读 env GITHUB_APP_REPO 全局硬编码,
    导致一个 approvo 只能 dispatch 到一个仓,新方案由调用方 (spec.github.repo) 传入。
    """
    now = time.time()
    tk = _inst_tokens.get(target_repo)
    if tk and now < tk["exp"] - 120:
        return tk["value"]
    jh = {"Authorization": f"Bearer {_app_jwt()}", "Accept": "application/vnd.github+json"}
    owner = os.environ["GITHUB_APP_OWNER"]
    # 自动发现该仓库对应的 installation id
    r = requests.get(f"{API}/repos/{owner}/{target_repo}/installation", headers=jh, timeout=10)
    r.raise_for_status()
    inst_id = r.json()["id"]
    # 即使 App 装在全组织,也把 token 缩到只这一个仓库 + 只 actions:write
    body = {"repositories": [target_repo], "permissions": {"actions": "write"}}
    r2 = requests.post(f"{API}/app/installations/{inst_id}/access_tokens",
                       headers=jh, json=body, timeout=10)
    r2.raise_for_status()
    _inst_tokens[target_repo] = {"value": r2.json()["token"], "exp": now + 3000}
    return _inst_tokens[target_repo]["value"]


def _token(target_repo: str) -> str:
    # 优先 GitHub App(自动轮换);没配 App 则用 PAT 兜底
    if os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_PRIVATE_KEY"):
        return _installation_token(target_repo)
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise RuntimeError("缺少 GITHUB_APP_ID/PRIVATE_KEY(App)或 GITHUB_TOKEN(PAT)")
    return tok


def _headers(target_repo: str) -> dict:
    return {
        "Authorization": f"Bearer {_token(target_repo)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ---------- 变更清单(审批卡列出本次发版包含的 PR)----------
# 与 dispatch token 权限分离:这里换的是缩到 [源码仓库] + contents:read + pull_requests:read
# 的只读 token,按仓库各自缓存。App 必须已安装到对应源码仓库,否则 /installation 404。
_read_tokens: dict = {}


def _split_owner_repo(repo_spec: str, default_owner: str) -> tuple[str, str]:
    """容错处理:配置里 source_repo 允许两种写法:
      - 'my-app'            (仓库名,owner 用 default_owner=GITHUB_APP_OWNER)
      - 'my-org/my-app' (完整 owner/repo,split 出来避免和 default_owner 拼成 owner/owner/repo)
    历史上 QA 配置一直是 'owner/repo' 形式,导致 URL 双前缀 404,拉不到变更清单.
    """
    if "/" in repo_spec:
        parts = repo_spec.split("/", 1)
        return parts[0], parts[1]
    return default_owner, repo_spec


class AppInstallLookupError(RuntimeError):
    """拿不到某个仓的 App 安装信息(`/repos/{owner}/{repo}/installation` 返回 404)。

    单独立一个类型,是为了让调用方能把它和"GitHub 挂了/网络不通"区分开:
    前者是**配置错误**(该报 409,人去 App 安装设置里勾一下就好),
    后者才是**校验服务不可用**(fail-close 报 502)。

    ⚠️ **404 不等于"App 没装"**。实测过一种情况:App 确实装在该 org 上、
    Repository access 也是 all,而 `/repos/{owner}/{repo}/installation` 仍然 404
    (典型成因是 `GITHUB_APP_ID` 与私钥不属于同一个/仍存在的 App,JWT 认到了
    另一个没装的 App)。所以错误信息必须把三种可能都列出来,
    否则看到的人会去做一件已经做过的事。

    为什么这类故障可以坏很久才暴露(这条比 404 本身更值得记):
    **派发部署用的是 PAT / 安装 token,只有"读仓库"走 App 读路径。**
    而读路径(拉变更清单)是被 try/except 吞掉的 —— 失败只表现为
    "审批卡片上没有变更清单",没人会因此报警。直到
    `check_release_promotes_pre`(fail-close,不吞异常)第一次执行,
    它才以 502 的形式冒出来。
    ⇒ 一条被 try/except 吞掉的依赖,可以坏很久而无人知晓。
    """


def _repo_read_token(repo: str) -> str:
    now = time.time()
    tk = _read_tokens.get(repo)
    if tk and now < tk["exp"] - 120:
        return tk["value"]
    if not (os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_PRIVATE_KEY")):
        return _token()  # PAT 兜底(PAT 自身得有这些仓库的读权限)
    jh = {"Authorization": f"Bearer {_app_jwt()}", "Accept": "application/vnd.github+json"}
    owner, repo_only = _split_owner_repo(repo, os.environ["GITHUB_APP_OWNER"])
    r = requests.get(f"{API}/repos/{owner}/{repo_only}/installation", headers=jh, timeout=10)
    if r.status_code == 404:
        # 404 在这里的含义是"这个仓上读不到 App 安装信息"。给出可直接照做的指引,
        # 而不是把 HTTPError 冒泡成"服务不可用"。
        raise AppInstallLookupError(
            f"读不到 {owner}/{repo_only} 的 App 安装信息(GitHub 返回 404)。"
            f"按顺序查三件事:"
            f"① App 是否装在 owner『{owner}』上、且 Repository access 覆盖 {repo_only};"
            f"② GITHUB_APP_ID(当前 {os.environ.get('GITHUB_APP_ID', '未设')})"
            f"与 GITHUB_APP_PRIVATE_KEY 是否属于**同一个且仍存在的** App"
            f"(App 重建过、或私钥换过而 id 没换,都会让 JWT 认到一个没装的 App);"
            f"③ GITHUB_APP_OWNER 是否正确 —— 当前解析出的 owner 是『{owner}』。"
        )
    r.raise_for_status()
    body = {"repositories": [repo_only],
            "permissions": {"contents": "read", "pull_requests": "read"}}
    r2 = requests.post(f"{API}/app/installations/{r.json()['id']}/access_tokens",
                       headers=jh, json=body, timeout=10)
    r2.raise_for_status()
    _read_tokens[repo] = {"value": r2.json()["token"], "exp": now + 3000}
    return _read_tokens[repo]["value"]


def _read_headers(repo: str) -> dict:
    return {
        "Authorization": f"Bearer {_repo_read_token(repo)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# squash 默认首行 "标题 (#123)";merge commit 首行 "Merge pull request #123 from ..."
_PR_NUM_RE = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)")


def release_changes(source_repo: str, base: str, head: str, max_api_lookups: int = 30) -> dict:
    """上次成功部署的 commit(base) → 本次 commit(head) 之间的变更清单。

    PR 提取两层:先按 commit message 正则(squash/merge 默认格式,零额外请求);
    没命中的 commit 再逐个调 commits/{sha}/pulls(rebase merge 也能找回),
    仍找不到的归入「未关联 PR 的直接提交」——那部分正是审批人最该看的。
    异常直接抛,由调用方降级(变更清单失败不阻塞审批)。
    """
    owner, repo_only = _split_owner_repo(source_repo, os.environ["GITHUB_APP_OWNER"])
    h = _read_headers(source_repo)
    r = requests.get(f"{API}/repos/{owner}/{repo_only}/compare/{base}...{head}",
                     headers=h, timeout=15)
    r.raise_for_status()
    data = r.json()
    commits = data.get("commits", [])
    out = {"status": "ok", "prs": [], "direct_commits": [],
           "total_commits": data.get("total_commits", len(commits)),
           "compare_url": data.get("html_url")}
    if not commits:
        out["status"] = "nodiff"   # 无新增提交:重发同版本,或发了更早的版本(回退)
        return out

    prs: dict = {}
    unmatched = []
    for cm in commits:
        msg = cm.get("commit", {}).get("message") or ""
        first = msg.split("\n", 1)[0]
        login = ((cm.get("author") or {}).get("login")
                 or cm.get("commit", {}).get("author", {}).get("name") or "?")
        m = _PR_NUM_RE.search(first)
        if not m:
            unmatched.append((cm, first, login))
            continue
        num = int(m.group(1) or m.group(2))
        # squash:标题=首行去掉 (#num);merge commit:标题在 message 末行,作者是合并者不可信
        title = first if m.group(1) else (msg.strip().split("\n")[-1].strip() or first)
        title = _PR_NUM_RE.sub("", title).strip()
        ent = prs.setdefault(num, {"number": num, "title": title, "author": login})
        if m.group(2):
            ent["title"] = title

    for cm, first, login in unmatched[:max_api_lookups]:
        found = []
        try:
            pr_r = requests.get(f"{API}/repos/{owner}/{repo_only}/commits/{cm['sha']}/pulls",
                                headers=h, timeout=10)
            if pr_r.ok:
                found = pr_r.json()
        except requests.RequestException:
            pass
        if found:
            for p in found:
                prs.setdefault(p["number"], {
                    "number": p["number"], "title": p.get("title") or "",
                    "author": (p.get("user") or {}).get("login") or "?"})
        else:
            out["direct_commits"].append({"sha": cm["sha"][:7], "message": first[:80], "author": login})
    # 超出 API 配额的没法确认归属,保守地也列为直接提交
    for cm, first, login in unmatched[max_api_lookups:]:
        out["direct_commits"].append({"sha": cm["sha"][:7], "message": first[:80], "author": login})

    for p in prs.values():
        p["url"] = f"https://github.com/{owner}/{repo_only}/pull/{p['number']}"
    out["prs"] = sorted(prs.values(), key=lambda x: x["number"])
    return out


def _deploy_ns(spec: dict, env: str) -> str:
    """部署目标 namespace。只认 release 条目里显式配的 deploy.namespace。

    🔴 不猜:配漏了就返回空串 → deploy_target_allowed 必然不匹配 → 不发券 →
       流水线明确失败。曾经这里有一条"按项目名猜 namespace"的兜底,后果是
       "配置没接上"这件事在某些项目上看不出来(它们恰好命中兜底)、
       在另一些项目上突然炸 —— 一条路能走通,掩盖了另一条路根本没接上。
    """
    d = (spec.get("deploy") or {})
    if d.get("namespace"):
        return str(d["namespace"])
    return ""


def env_cluster(spec: dict, env: str) -> str:
    """部署目标 cluster 名。优先 release 条目里的 deploy.cluster;
    env=qa 时回落到名为 "qa" 的集群;生产必须由条目显式指定(同样不猜)。"""
    d = (spec.get("deploy") or {})
    if d.get("cluster"):
        return str(d["cluster"])
    return "qa" if env == "qa" else ""


def dispatch_and_wait(spec: dict, timeout: int = 900):
    """触发 deploy.yml 并【等待该 run 跑完】,返回 (ok, log)。ok 反映 run 的真实结论,
    而不是"触发成功"——这样结果卡在部署真正结束时才弹。"""
    g = spec.get("github") or {}
    owner, repo, workflow = g.get("owner"), g.get("repo"), g.get("workflow")
    ref = g.get("ref", "main")
    if not (owner and repo and workflow):
        return False, "github 方式需要配置 github.owner / repo / workflow"

    # 🔴 刻意【不给默认值】。原实现是 g.get("env", "prod") —— 一旦某个 release 条目或
    #    某个 stage 漏写 env,就会【静默派发到生产】。实测过一份真实配置:所有条目的
    #    顶层 github.env 都是空的,全靠每个 stage 记得写 —— 这层安全建立在人工约定上。
    #    改成 fail-close:解析不出 env 就拒绝派发,不猜。
    env = g.get("env")
    if not env:
        return False, ("github.env 未配置,拒绝派发(刻意不默认 prod —— "
                       "缺省值指向生产是这类设计最常见的致命错误)")
    inputs = {"app": spec["repo"], "tag": spec["tag"], "env": env}

    # 部署凭据的一次性兑换券。流水线用它 + RELEASE_TOKEN 换 10 分钟绑定 token,
    # 部署完回调撤销 —— 取代常驻 runner 的长期 kubeconfig(见 app/deploycred.py)。
    #
    # 🔴 grant 会进 run 元数据(GitHub UI 可见),所以它【本身不是集群凭据】:
    #    必须再配 RELEASE_TOKEN 才能兑换,且一次性、10 分钟。
    # 🔴 只在【该目标已登记 deploy_targets】时才发券。没登记就不发,
    #    让流水线明确失败(强制动态凭据 = 没有凭据就不部署),
    #    而不是让它悄悄回落到 runner 上的文件。
    cred_target = k8s.deploy_target_allowed(env_cluster(spec, env), _deploy_ns(spec, env))
    if cred_target:
        grant, ttl = deploycred.issue_grant(spec.get("instance_code", "-"),
                                            cred_target["cluster"], cred_target["namespace"])
        inputs["cred_grant"] = grant
        print(f"[github] 已签发部署凭据兑换券 target={cred_target['cluster']}/"
              f"{cred_target['namespace']} ttl={ttl}m")
    else:
        print("[github] 目标未登记 deploy_targets,不发兑换券 "
              "(流水线将因缺少动态凭据而失败 —— 这是刻意的 fail-close)")

    inputs.update(g.get("inputs", {}))

    # 派发重试:只在【连接层】失败时重试(ConnectionError 含 SSLError/ConnectTimeout,
    # 说明连接就没建起来、请求未送达 → 重试安全)。
    # 🔴 刻意【不】重试 ReadTimeout:那意味着请求已发出、只是没读到响应,重试会【重复派发】。
    # 起因:2026-08-09 approvo 派发时 TLS 握手超时,线程直接死,发版静默丢失。
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches",
                              headers=_headers(repo), json={"ref": ref, "inputs": inputs}, timeout=15)
            break
        # ⚠️ 只捕 ConnectionError,【不要】加 OSError/TimeoutError:
        #    requests 的异常最终继承自 IOError(=OSError),把 OSError 写进来会把
        #    ReadTimeout 也纳入重试 → 重复派发。(本处初版就踩了,靠下面的自检脚本发现)
        #    ConnectTimeout / SSLError 都是 ConnectionError 的子类,已覆盖。
        except requests.exceptions.ConnectionError as e:
            if attempt == 2:
                return False, f"workflow_dispatch 连接失败(重试 3 次): {type(e).__name__}: {e}"
            wait = 2 ** attempt * 3   # 3s, 6s
            print(f"[github] 派发连接失败({type(e).__name__}),{wait}s 后重试 {attempt + 2}/3")
            time.sleep(wait)
    if r.status_code != 204:
        return False, f"workflow_dispatch 失败 HTTP {r.status_code}: {r.text}"

    # 按 deploy.yml 的 run-name 定位刚触发的 run(run-name = "deploy <app> <tag> (<env>)")
    title = f"deploy {spec['repo']} {spec['tag']} ({env})"
    page = f"https://github.com/{owner}/{repo}/actions/workflows/{workflow}"
    run_id, url = None, page
    for _ in range(20):
        time.sleep(3)
        rr = requests.get(f"{API}/repos/{owner}/{repo}/actions/workflows/{workflow}/runs",
                          headers=_headers(repo), params={"event": "workflow_dispatch", "per_page": 20}, timeout=10)
        for run in rr.json().get("workflow_runs", []):
            if run.get("display_title") == title or run.get("name") == title:
                run_id, url = run["id"], run["html_url"]
                break
        if run_id:
            break
    if not run_id:
        return True, f"已触发,但没定位到 run(去 Actions 查看):{page}"

    deadline = time.time() + timeout
    while time.time() < deadline:
        d = requests.get(f"{API}/repos/{owner}/{repo}/actions/runs/{run_id}",
                         headers=_headers(repo), timeout=10).json()
        if d.get("status") == "completed":
            concl = d.get("conclusion")
            if concl == "success":
                return True, f"GitHub run {url} -> success"
            # 失败:列出失败的 step + run 链接(点进去看完整日志:拉不到镜像/健康检查等)
            fails = []
            try:
                jr = requests.get(f"{API}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                                  headers=_headers(repo), timeout=10).json()
                for j in jr.get("jobs", []):
                    for st in j.get("steps", []) or []:
                        if st.get("conclusion") == "failure":
                            fails.append(f"{j.get('name')} / {st.get('name')}")
            except Exception:
                pass
            detail = ("失败步骤: " + "; ".join(fails) + "\n") if fails else ""
            return False, f"GitHub run -> {concl}\n{detail}完整日志: {url}"
        time.sleep(8)
    return False, f"GitHub run 超时未完成:{url}"


# ---------- check-prereq：release 必须是同号 pre 的 promote ----------
# 为什么放在 approvo 服务端而不是各仓 release.yml：
#   tag 触发的 workflow 跑的是【被 tag 那个 commit】上的 workflow 文件，而 promote 的本质
#   就是指向一个旧 commit —— 于是"给 main 加门禁"对 promote 天然失效，还能被"故意 tag
#   一个旧 commit"绕过。放服务端则：①与被 tag 的 commit 无关 ②一处生效覆盖所有仓
#   ③绕不过（CI 必须经 approvo 才能部署）。
#   （2026-08-09 实测：my-app#156 把门禁加进 release.yml，promote V0.0.148-release 时
#     确实没跑到——tag 指向的 3180519 早于门禁合入的 346eb3e。）

def tag_commit(source_repo: str, tag: str) -> str | None:
    """把 tag 解析成 commit sha；tag 不存在返回 None。

    用 /commits/{ref} 而不是 /git/ref/tags/{tag}：前者由服务端做 ref 解析，
    【附注 tag 会自动解到 commit】。否则要像 git 那样自己处理 tag 对象 → commit
    的二段解引用（等价于 `rev-parse <tag>^{commit}`），漏了就会把 tag 对象 sha
    当成 commit sha 比，永远不相等。
    """
    owner, repo_only = _split_owner_repo(source_repo, os.environ["GITHUB_APP_OWNER"])
    r = requests.get(f"{API}/repos/{owner}/{repo_only}/commits/{tag}",
                     headers=_read_headers(source_repo), timeout=10)
    # 🔴 「ref 不存在」在这个端点上是 **422**,不是 404(404 是仓库不存在/无权限)。
    # 实测:GET /repos/{owner}/{repo}/commits/<不存在的 tag> → 422 Unprocessable Entity。
    # 只处理 404 的话,422 会 raise_for_status 冒泡,被 /release 的宽捕获包成
    # 502「check-prereq 无法校验」—— 而它本该是一句指向明确的 409「tag 不存在」。
    # 那次排查绕了一大圈(App 安装 / 配置漂移 / 行号全被怀疑过一遍),
    # 最后靠打印被 curl 丢掉的响应体才定案。
    if r.status_code in (404, 422):
        return None
    r.raise_for_status()
    return r.json().get("sha")


def check_release_promotes_pre(source_repo: str, tag: str) -> tuple[bool, str]:
    """契约：pre 与 release 不是两个版本，是同一个 commit 走两道门，版本号只有一个。
    返回 (是否放行, 不放行的原因)。非 -release tag 直接放行。"""
    suffix = "-release"
    if not tag.endswith(suffix):
        return True, ""
    pre = tag[: -len(suffix)] + "-pre"
    rel_sha = tag_commit(source_repo, tag)
    if not rel_sha:
        return False, f"tag `{tag}` 在 {source_repo} 上不存在"
    pre_sha = tag_commit(source_repo, pre)
    if not pre_sha:
        return False, (f"同号 pre tag `{pre}` 不存在。release 必须 promote 一个已在 QA 验证过的 pre；"
                       f"若 `{pre}` 曾存在但被删除 —— 删 git tag 不会释放版本号，请换一个号。")
    if pre_sha != rel_sha:
        return False, (f"`{tag}`({rel_sha[:7]}) 与 `{pre}`({pre_sha[:7]}) 指向不同 commit，"
                       f"生产会拿到未经 QA 验证的代码。release 必须打在与 `{pre}` 相同的 commit 上。")
    return True, ""


# 破坏性 SQL 的识别规则。命中不代表一定有害,但审批人【必须看见】。
# 🔴 为什么要专门标出来:镜像可以回滚,schema 不能。一个 DROP COLUMN 上线后,
#    回滚镜像只会让旧代码去访问已经不存在的列 —— 回滚反而把故障扩大。
_DANGER_SQL = [
    (re.compile(r"\bDROP\s+(TABLE|COLUMN|INDEX|CONSTRAINT|SCHEMA)\b", re.IGNORECASE), "DROP"),
    (re.compile(r"\bALTER\s+COLUMN\b[^;]*\bTYPE\b", re.IGNORECASE), "改列类型"),
    (re.compile(r"\bSET\s+NOT\s+NULL\b", re.IGNORECASE), "加 NOT NULL"),
    (re.compile(r"\bRENAME\s+(TO|COLUMN)\b", re.IGNORECASE), "重命名"),
    (re.compile(r"\bTRUNCATE\b", re.IGNORECASE), "TRUNCATE"),
    (re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE), "DELETE"),
]


def db_migration_changes(deploy_repo: str, since_iso: str | None,
                         path: str = "db/migrations", max_commits: int = 10) -> dict:
    """自 since_iso 起,迁移目录(通常在部署仓)发生了什么。

    🔴 为什么需要它:审批卡此前只说"仅镜像变更 —— 未附带环境变量/配置变更",
       而迁移文件常常放在【部署仓】、不在业务仓,release_changes 天然看不见。
       于是一次带着 DROP COLUMN 的发版,审批人看到的仍然是"仅镜像变更"。

    ⚖️ 口径:以"本应用上次成功部署的时间"为基线,不是"数据库里未应用的迁移"——
       approvo 够不到数据库。所以卡片必须如实写明这是【时间口径】,
       真正会执行哪些以迁移门禁为准。宁可说清楚口径,也不假装知道 DB 状态。

    失败时返回 status='unknown' 并带上原因 —— 绝不返回"没有变更",
    因为"查不到"和"没有"对审批人是完全不同的两件事。
    """
    if not since_iso:
        return {"status": "unknown", "reason": "没有上次成功部署的时间基线(首次部署?)"}
    try:
        owner, repo_only = _split_owner_repo(deploy_repo, os.environ["GITHUB_APP_OWNER"])
        h = _read_headers(deploy_repo)
        r = requests.get(f"{API}/repos/{owner}/{repo_only}/commits",
                         headers=h, timeout=15,
                         params={"path": path, "since": since_iso, "per_page": max_commits})
        r.raise_for_status()
        commits = r.json()
        if not commits:
            return {"status": "none", "since": since_iso}
        files: dict = {}
        for cm in commits[:max_commits]:
            d = requests.get(f"{API}/repos/{owner}/{repo_only}/commits/{cm['sha']}",
                             headers=h, timeout=15).json()
            for fl in d.get("files", []):
                if not fl.get("filename", "").startswith(path):
                    continue
                name = fl["filename"].split("/")[-1]
                ent = files.setdefault(name, {"name": name, "status": fl.get("status"), "danger": []})
                patch = fl.get("patch") or ""
                for rx, label in _DANGER_SQL:
                    if rx.search(patch) and label not in ent["danger"]:
                        ent["danger"].append(label)
        return {"status": "ok", "since": since_iso,
                "files": sorted(files.values(), key=lambda x: x["name"]),
                "commits": len(commits)}
    except Exception as e:      # 查不到 ≠ 没有:必须让卡片显示"无法判定"
        return {"status": "unknown", "reason": f"{type(e).__name__}: {e}"}
