"""集群只读凭证签发 —— 让「查日志/看状态」不必 ssh 进服务器。

为什么需要它：人登服务器【多数时候是为了看，不是为了改】。若查日志还得 ssh，
ssh 权限就永远收不回来；收不回来，改的能力也就还在。所以要先给"看"一条不经 shell 的路。

安全取向：
- 签发的是【短期(默认 30 分钟)、只读、namespace 受限】的 ServiceAccount token。
- 🔴 只读【不含 secrets】——只读也不该看到密钥值。
- 🔴 只读【不含 exec/portforward】——那是拿 shell，正是本通道要消灭的东西，
  不能混进"只读"里。需要 exec 是另一件事，必须单独审批。
- 身份不靠共享 token：调用方必须提供飞书 user_id 且【确实在审批群里】。
  共享的 RELEASE_TOKEN 无法归因是谁签的，不构成身份。
- 每次签发都往群里发卡：可归因 + 全群可见，异常签发立刻被发现。
"""
import subprocess

from app import settings


def viewer_sa_for(cluster: str, namespace: str) -> str:
    """取该目标登记的只读 SA 名。

    🔴 fail-close:未登记的组合、以及登记了却没写 `sa`(且没配全局
       `viewer_sa_default`)的目标,一律抛错 —— 绝不猜一个名字。
       猜的后果有两种,都很糟:
         · 给别的项目/别的团队加 viewer 目标时忘写 sa → 拿着另一个环境的账号名
           去人家的命名空间(至少是一次无效签发,信息噪音);
         · 万一那个命名空间里恰好存在同名 SA,签出来的权限就【不是你以为的那个】。
       `target_allowed()` 会先拦住未登记组合,但"两道防线里有一道是坏的"本身
       就是必须修的缺陷 —— 防线不能互相假设对方管用。
    """
    for t in settings.VIEWER_TARGETS:
        if t.get("cluster") == cluster and t.get("namespace") == namespace:
            sa = t.get("sa") or settings.VIEWER_SA_DEFAULT
            if sa:
                return str(sa)
            raise ValueError(
                f"viewer_targets 里 {cluster}/{namespace} 未指定 sa —— 拒绝猜测。"
                f"请在该 target 上显式写 sa(该命名空间自己的只读账号),"
                f"或配置顶层 viewer_sa_default")
    raise ValueError(f"{cluster}/{namespace} 未登记为 viewer 目标 —— 拒绝签发")


def target_allowed(cluster: str, namespace: str) -> bool:
    """签发只读凭证的目标白名单 —— 只允许 config 的 viewer_targets 里显式列出的组合。

    🔴 为什么两条入口都要用它:卡片按钮与 /viewer-token 是【同一个能力的两个入口】。
    只给新入口加白名单等于没加 —— 绕过者会走没加的那条。故下沉到这里共用。

    🔴 为什么不能信调用方给的 cluster/namespace:
    - 卡片的 action.value 是外部输入,任何能往群里发卡片的人都能构造指向别处的按钮;
    - /viewer-token 的 body 更是任意可填,而它只校验"你是谁"(RELEASE_TOKEN + 群成员),
      从不校验"你能操作哪个目标"。
    同一个集群里往往还跑着别的团队的生产 namespace(红线是绝对不能碰),
    不能只靠下游 RBAC 兜底 —— RBAC 是运维状态、随时可能因别的需求被放开,
    白名单才是写在配置里的显式意图。
    """
    return any(str(t.get("cluster")) == cluster and str(t.get("namespace")) == namespace
               for t in settings.VIEWER_TARGETS)


def secret_target_allowed(cluster: str, namespace: str) -> dict | None:
    """Secret 变更的目标白名单 —— 命中则返回该 target(含 release_key),否则 None。

    🔴 与 target_allowed 同一条道理,但此前【完全缺失】:/secret/request 的 cluster
    与 namespace 是请求体里任意可填的,只靠 token + 群成员 + 审批把关。
    而改 Secret 比签只读凭证【破坏性更强】(旧值不可恢复),判据反而更松 —— 这是倒挂。
    空白名单 = 通道整体关闭(fail-close),而不是"没配就全放开"。
    """
    for t in settings.SECRET_TARGETS:
        if str(t.get("cluster")) == cluster and str(t.get("namespace")) == namespace:
            return t
    return None


def secret_release_keys() -> set[str]:
    """所有被登记为 Secret 变更入口的 release_key —— /secret/commit 用它校验授权归属。"""
    keys = {settings.SECRET_RELEASE_KEY}
    for t in settings.SECRET_TARGETS:
        if t.get("release_key"):
            keys.add(str(t["release_key"]))
    return keys


def _kubectl(cluster: str, args: list[str], timeout: int = 20) -> tuple[bool, str]:
    cl = settings.CLUSTERS.get(cluster) or {}
    kubeconfig = cl.get("kubeconfig")
    if not kubeconfig:
        return False, f"cluster '{cluster}' 未在 config.clusters 配置 kubeconfig"
    env = {"KUBECONFIG": kubeconfig, "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"}
    if cl.get("context"):
        args = ["--context", cl["context"], *args]
    p = subprocess.run(["kubectl", *args], env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def issue_viewer_kubeconfig(cluster: str, namespace: str, minutes: int = 30) -> tuple[bool, str]:
    """签发只读 kubeconfig 文本。失败返回 (False, 错误说明)。"""
    # viewer_sa_for 是 fail-close 的(未登记/未写 sa 会抛 ValueError)。
    # 这里必须转成 (False, 说明):本函数的契约是"失败返回二元组",
    # 让异常穿出去会变成 500 / 未捕获栈,用户只看到"内部错误"而不知道是配置漏了 sa。
    try:
        sa = viewer_sa_for(cluster, namespace)
    except ValueError as e:
        return False, str(e)
    ok, out = _kubectl(cluster, ["-n", namespace, "create", "token", sa,
                                 f"--duration={minutes}m"])
    if not ok:
        return False, f"签发 token 失败：{out.strip()[:300]}"
    token = out.strip()
    if not token:
        return False, "签发 token 失败：返回为空"

    ok, server = _kubectl(cluster, ["config", "view", "--raw", "--minify",
                                    "-o", "jsonpath={.clusters[0].cluster.server}"])
    ok2, ca = _kubectl(cluster, ["config", "view", "--raw", "--minify",
                                 "-o", "jsonpath={.clusters[0].cluster.certificate-authority-data}"])
    if not (ok and ok2 and server.strip()):
        return False, "读取集群地址/CA 失败"

    kubeconfig = f"""apiVersion: v1
kind: Config
clusters:
- name: {cluster}
  cluster:
    server: {server.strip()}
    certificate-authority-data: {ca.strip()}
contexts:
- name: viewer
  context: {{cluster: {cluster}, namespace: {namespace}, user: viewer}}
current-context: viewer
users:
- name: viewer
  user: {{token: {token}}}
"""
    return True, kubeconfig


def patch_secret(cluster: str, namespace: str, name: str, key: str,
                 value: str) -> tuple[bool, str]:
    """把 Secret 的某个 key 改成 value。

    🔴 值的生命周期：HTTP 请求 → 本进程内存 → kubectl stdin → k8s API。
    【绝不落库、绝不入 git、绝不进日志、绝不进命令行参数】。
    走 stdin 而不是 `kubectl create secret --from-literal=` 是刻意的 ——
    后者会把值写进进程 argv，同机任何人 `ps` 一下就看得见。
    """
    import base64
    import json

    b64 = base64.b64encode(value.encode()).decode()
    patch = json.dumps({"data": {key: b64}})
    cl = settings.CLUSTERS.get(cluster) or {}
    kubeconfig = cl.get("kubeconfig")
    if not kubeconfig:
        return False, f"cluster '{cluster}' 未配置 kubeconfig"
    env = {"KUBECONFIG": kubeconfig, "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"}
    args = ["kubectl", "-n", namespace, "patch", "secret", name,
            "--type=merge", "--patch-file=/dev/stdin"]
    if cl.get("context"):
        args[1:1] = ["--context", cl["context"]]
    p = subprocess.run(args, input=patch, env=env, capture_output=True, text=True, timeout=20)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out.strip()[:300]


def secret_key_exists(cluster: str, namespace: str, name: str, key: str) -> tuple[bool, bool, str]:
    """返回 (查询是否成功, key 是否已存在, 说明)。用于卡片上区分【新增 key】与【覆盖已有 key】——
    覆盖是破坏性的(旧值不可恢复)，审批人必须知道自己批的是哪一种。"""
    ok, out = _kubectl(cluster, ["-n", namespace, "get", "secret", name,
                                 "-o", "jsonpath={.data}"])
    if not ok:
        return False, False, out.strip()[:200]
    return True, (f'"{key}"' in out), ""


def deploy_target_allowed(cluster: str, namespace: str) -> dict | None:
    """部署凭据的目标白名单。fail-close:config 里没登记就一律拒绝。

    与 viewer / secret 两条通道同一原则:cluster/namespace 是外部可填的,
    绝不能因为"调用方说要 ns X"就往 ns X 签发。
    """
    for t in (settings.DEPLOY_TARGETS or []):
        if str(t.get("cluster")) == cluster and str(t.get("namespace")) == namespace:
            return t
    return None


def issue_deploy_kubeconfig(cluster: str, namespace: str, slot: str,
                            minutes: int = 10) -> tuple[bool, str]:
    """签发【部署用】短期 kubeconfig,并绑定到一次性对象 slot。

    🔴 绑定是必须的,不是可选优化:TTL 下限 10 分钟(API server 会拒绝 1m/5m 的请求),
       而部署通常 1~2 分钟就结束。没有绑定对象就没有"用完即毁",
       凭据会在部署结束后继续有效若干分钟。

    返回 (True, kubeconfig 文本) 或 (False, 错误说明)。
    失败时【必须】由调用方释放 slot 并清理 Secret,否则槽位会泄漏。
    """
    tgt = deploy_target_allowed(cluster, namespace)
    if not tgt:
        return False, f"{cluster}/{namespace} 未登记为 deploy_targets —— 拒绝签发"
    sa = tgt.get("sa")
    if not sa:
        return False, f"deploy_targets 里 {cluster}/{namespace} 未指定 sa —— 拒绝猜测"

    # ① 建一次性绑定对象。已存在说明上一次没清理干净,先删再建(同名槽位可复用)。
    _kubectl(cluster, ["-n", namespace, "delete", "secret", slot, "--ignore-not-found"])
    ok, out = _kubectl(cluster, ["-n", namespace, "create", "secret", "generic", slot,
                                 "--from-literal=purpose=approvo-deploy-credential"])
    if not ok:
        return False, f"建一次性绑定对象失败:{out.strip()[:200]}"

    # ② 签 token 并绑定
    ok, out = _kubectl(cluster, ["-n", namespace, "create", "token", sa,
                                 f"--duration={minutes}m",
                                 "--bound-object-kind", "Secret",
                                 "--bound-object-name", slot])
    if not ok:
        return False, f"签发 token 失败:{out.strip()[:200]}"
    token = out.strip()
    if not token:
        return False, "签发 token 失败:返回为空"

    ok, server = _kubectl(cluster, ["config", "view", "--raw", "--minify",
                                    "-o", "jsonpath={.clusters[0].cluster.server}"])
    _, ca = _kubectl(cluster, ["config", "view", "--raw", "--minify", "-o",
                                 "jsonpath={.clusters[0].cluster.certificate-authority-data}"])
    server = server.strip()
    ca = ca.strip()
    if not ok or not server:
        return False, "取不到 API server 地址"
    # 有些集群的 kubeconfig 用 insecure-skip-tls-verify、没有 CA data(轻量发行版常见)。
    # 这里如实照抄集群配置,不硬编码任何一种。
    tls = (f"    certificate-authority-data: {ca}" if ca
           else "    insecure-skip-tls-verify: true")
    kubeconfig = f"""apiVersion: v1
kind: Config
clusters:
- name: target
  cluster:
    server: {server}
{tls}
users:
- name: deployer
  user:
    token: {token}
contexts:
- name: ctx
  context:
    cluster: target
    user: deployer
    namespace: {namespace}
current-context: ctx
"""
    return True, kubeconfig


def revoke_deploy_credential(cluster: str, namespace: str, slot: str) -> tuple[bool, str]:
    """删掉绑定对象 → 该 token 当场失效(实测约 15 秒内变 Unauthorized)。"""
    ok, out = _kubectl(cluster, ["-n", namespace, "delete", "secret", slot,
                                 "--ignore-not-found"])
    return ok, out.strip()[:200]
